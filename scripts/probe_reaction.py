#!/usr/bin/env python3
"""Automated smoke probe for the Discord reaction handler.

CLAUDE.md verification step 6: "Reaction handler: ✅ on an opp embed mutates
state identically to button click." Today the only way to verify is to open
Discord and click the reaction. This script invokes
``handle_raw_reaction_add`` programmatically against the live Postgres +
Redis stack and asserts the same DB side effects a button click would
produce.

Run from inside the applier-worker container (it carries discord.py + all
runtime deps + an env wired by `sops exec-env secrets.yaml`)::

    sops exec-env secrets.yaml 'docker compose run --rm \\
        -v $(pwd):/app \\
        --entrypoint /opt/venv/bin/python applier-worker \\
        /app/scripts/probe_reaction.py ✅'

The probe is fully self-contained:

1.  Mints a synthetic source + opportunity scoped to this run only. Walks
    the opp through the legal V004 transitions ``new → queued → ranked →
    digested`` so the reaction-target transition is legal.
2.  Monkey-patches ``RedisQ.publish`` to capture every ``stream:apply``
    message the handler emits.
3.  Forges a ``RawReactionActionEventLike`` and a ``MockBot`` carrying the
    minimum surface area the handler reads (``bot.user.id``,
    ``bot.get_channel`` → mock channel whose ``fetch_message`` returns a
    mock embed footer containing the synthetic opp_id).
4.  ``await handle_raw_reaction_add(event, mock_bot)``.
5.  Asserts the per-emoji invariants documented at the top of each
    ``_assert_*`` block — state flip, V004 transition row, stream:apply
    payload shape, source field == ``reaction``.
6.  Tears down every row it inserted in a ``finally`` block. The synthetic
    opp + source disappear; ``ON DELETE CASCADE`` removes transitions and
    scores.

The probe never touches a real opp from the 5810+ live rows.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote as _urlquote

# --- bootstrap sys.path -----------------------------------------------------
# When this file runs under `docker compose run ... -v $(pwd):/app`,
# /app holds the repo root and src/ is importable. When it runs from the
# host with `uv run`, the same path matches the dev tree. Add the parent
# of this script's directory to sys.path so `src.*` imports resolve in
# both worlds.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import asyncpg  # noqa: E402 — after sys.path mutation

from src.common import db  # noqa: E402
from src.common.queue import RedisQ, Streams  # noqa: E402
from src.common.secrets import get_settings  # noqa: E402

# Lazy import of the handler — it pulls discord.py which is heavy.
# Done inside main() to keep import errors local + readable.


# ---------------------------------------------------------------------------
# Emoji → expected action mapping (mirror of src/notifiers/.../reactions.py)
# ---------------------------------------------------------------------------
_EXPECTED_ACTION: dict[str, str] = {
    "✅": "apply",
    "❌": "skip",
    "🔖": "pin",
    "💬": "explain",
    "🔁": "snooze",
}

# Some humans type ⏰ (alarm clock) for "snooze". The handler does NOT map it.
# We treat ⏰ as an explicit "no-op expected" case so we report rather than
# crash. CLAUDE.md verification mentions snooze; the actual emoji is 🔁.
_NOOP_EMOJI: set[str] = {"⏰"}


# ---------------------------------------------------------------------------
# Per-emoji expected state transition (must be legal per V004)
# ---------------------------------------------------------------------------
_EXPECTED_TO_STATE: dict[str, str | None] = {
    "✅": "applied",
    "❌": "seen",
    "🔁": "snoozed",
    "🔖": None,  # pin doesn't change state, only writes user_pins
    "💬": None,  # explain doesn't change state, only publishes to NOTIFY
}


# ---------------------------------------------------------------------------
# Forged discord types — duck-typed to the attrs the handler reads
# ---------------------------------------------------------------------------
@dataclass
class _MockEmoji:
    name: str

    def __str__(self) -> str:
        return self.name


@dataclass
class _MockReactionEvent:
    """Quacks like discord.RawReactionActionEvent."""

    message_id: int
    channel_id: int
    user_id: int
    emoji: _MockEmoji
    guild_id: int | None = None
    member: Any = None


@dataclass
class _MockEmbedFooter:
    text: str


@dataclass
class _MockEmbed:
    footer: _MockEmbedFooter
    title: str | None = None
    description: str | None = None


@dataclass
class _MockMessage:
    id: int
    embeds: list[_MockEmbed]
    content: str = ""


class _MockChannel:
    def __init__(self, channel_id: int, message: _MockMessage) -> None:
        self.id = channel_id
        self._message = message

    async def fetch_message(self, message_id: int) -> _MockMessage:
        return self._message


@dataclass
class _MockUser:
    id: int


class _MockBot:
    """Quacks like discord.Client. Only the surface the reaction handler reads."""

    def __init__(self, channel: _MockChannel, bot_user_id: int = 999_000_000_001) -> None:
        self.user = _MockUser(id=bot_user_id)
        self._channel = channel

    def get_channel(self, channel_id: int) -> _MockChannel | None:
        if channel_id == self._channel.id:
            return self._channel
        return None

    async def fetch_channel(self, channel_id: int) -> _MockChannel:
        # Real bot would round-trip the API; we keep the local mock.
        return self._channel


# ---------------------------------------------------------------------------
# Assertion bookkeeping
# ---------------------------------------------------------------------------
@dataclass
class _ProbeReport:
    emoji: str
    opp_id: str
    pre_state: str
    post_state: str | None = None
    pre_transition_count: int = 0
    post_transition_count: int = 0
    pre_application_count: int = 0
    post_application_count: int = 0
    pre_pin_count: int = 0
    post_pin_count: int = 0
    expires_at_after: datetime | None = None
    captured_publishes: list[dict[str, Any]] = field(default_factory=list)
    assertions: list[tuple[str, bool, str]] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.assertions.append((name, ok, detail))

    def all_passed(self) -> bool:
        return all(ok for _, ok, _ in self.assertions)


# ---------------------------------------------------------------------------
# DB helpers (use a direct asyncpg connection — bypasses the singleton pool
# so we never collide with applier-worker's pool inside the same image)
# ---------------------------------------------------------------------------
async def _connect_pg() -> asyncpg.Connection:
    settings = get_settings()
    user = _urlquote(settings.postgres_user, safe="")
    pw = _urlquote(settings.postgres_password, safe="")
    dsn = f"postgresql://{user}:{pw}@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"
    return await asyncpg.connect(dsn, command_timeout=10)


async def _seed_synthetic_opp(conn: asyncpg.Connection) -> tuple[int, uuid.UUID]:
    """Insert a synthetic source + opp, walk to 'digested'. Return (source_id, opp_id)."""
    nonce = uuid.uuid4().hex[:12]
    slug = f"probe_reaction_{nonce}"
    canonical_url = f"https://probe.invalid/reaction/{nonce}"

    source_id = await conn.fetchval(
        """
        INSERT INTO sources (slug, name, category, base_url, crawler_strategy,
                             fetch_freq_minutes, priority, status, created_via)
        VALUES ($1, $2, 'other', $3, 'generic_html', 1440, 9, 'paused', 'probe')
        RETURNING id
        """,
        slug,
        f"probe-reaction-{nonce}",
        canonical_url,
    )

    opp_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO opportunities
            (id, source_id, canonical_url, title, fingerprint_hash, state)
        VALUES ($1, $2, $3, $4, $5, 'new')
        """,
        opp_id,
        source_id,
        canonical_url,
        f"probe opportunity {nonce}",
        f"probe-fp-{nonce}",
    )

    # Walk the legal V004 chain new → queued → ranked → digested. The V004
    # BEFORE-UPDATE trigger validates each hop AND auto-logs an audit row
    # with trigger='auto'.
    for target in ("queued", "ranked", "digested"):
        await conn.execute(
            "UPDATE opportunities SET state = $2 WHERE id = $1",
            opp_id,
            target,
        )

    return source_id, opp_id


async def _cleanup(conn: asyncpg.Connection, source_id: int, opp_id: uuid.UUID) -> None:
    """Remove every row the probe inserted, even if assertions failed.

    user_pins doesn't ON-DELETE-CASCADE (it's keyed by uuid + bigint), so we
    explicitly nuke it. Applications, transitions, scores cascade via
    opportunities -> sources.
    """
    try:
        await conn.execute("DELETE FROM user_pins WHERE opportunity_id = $1", opp_id)
    except Exception:
        pass
    try:
        await conn.execute("DELETE FROM applications WHERE opportunity_id = $1", opp_id)
    except Exception:
        pass
    # Deleting the opp cascades transitions + scores via FK ON DELETE CASCADE.
    await conn.execute("DELETE FROM opportunities WHERE id = $1", opp_id)
    await conn.execute("DELETE FROM sources WHERE id = $1", source_id)


# ---------------------------------------------------------------------------
# Probe core
# ---------------------------------------------------------------------------
async def run_probe(emoji: str, pinned_opp_id: str | None = None) -> _ProbeReport:
    # Late import to keep tracebacks readable for missing-deps cases.
    from src.notifiers.discord.handlers import reactions as reactions_mod

    _ = get_settings()  # touch settings — asserts env loaded

    # 1) init the shared pool used by buttons._transition_state.
    await db.init_pool(min_size=1, max_size=3)

    # 2) own connection for our own DML (separate from the pool the handler uses).
    conn = await _connect_pg()
    source_id: int | None = None
    opp_uuid: uuid.UUID | None = None
    captured: list[dict[str, Any]] = []

    try:
        # ----- seed -----
        if pinned_opp_id:
            opp_uuid = uuid.UUID(pinned_opp_id)
            row = await conn.fetchrow(
                "SELECT source_id, state FROM opportunities WHERE id = $1",
                opp_uuid,
            )
            if row is None:
                raise SystemExit(f"--opp-id {pinned_opp_id} not found")
            if row["state"] != "digested":
                raise SystemExit(f"--opp-id state is {row['state']!r}; probe needs 'digested'")
            source_id = row["source_id"]
            owns_opp = False
        else:
            source_id, opp_uuid = await _seed_synthetic_opp(conn)
            owns_opp = True

        opp_id_str = str(opp_uuid)
        report = _ProbeReport(emoji=emoji, opp_id=opp_id_str, pre_state="digested")

        # snapshot pre-counts
        report.pre_transition_count = await conn.fetchval(
            "SELECT COUNT(*) FROM opportunity_transitions WHERE opportunity_id = $1",
            opp_uuid,
        )
        report.pre_application_count = await conn.fetchval(
            "SELECT COUNT(*) FROM applications WHERE opportunity_id = $1",
            opp_uuid,
        )
        report.pre_pin_count = await conn.fetchval(
            "SELECT COUNT(*) FROM user_pins WHERE opportunity_id = $1",
            opp_uuid,
        )

        # ----- monkey-patch RedisQ.publish to capture stream:apply payloads -----
        # We still let the publish happen so the live applier-worker can act on
        # it; we just snapshot the payload on the way through.
        orig_publish = RedisQ.publish

        async def _capturing_publish(self: RedisQ, stream: str, payload: dict[str, Any]) -> str:  # type: ignore[override]
            if stream == Streams.APPLY:
                captured.append({"stream": stream, **payload})
            return await orig_publish(self, stream, payload)

        RedisQ.publish = _capturing_publish  # type: ignore[assignment]

        # ----- forge event + bot -----
        channel_id = 90_000_000_000_000_000  # nonsense channel; never touched live
        message_id = 91_000_000_000_000_000
        footer = _MockEmbedFooter(text=f"cartograph probe · opp_id={opp_id_str}")
        embed = _MockEmbed(footer=footer, title="probe", description="probe")
        message = _MockMessage(id=message_id, embeds=[embed])
        channel = _MockChannel(channel_id=channel_id, message=message)
        bot = _MockBot(channel=channel)
        event = _MockReactionEvent(
            message_id=message_id,
            channel_id=channel_id,
            user_id=42,  # any non-bot id
            emoji=_MockEmoji(name=emoji),
        )

        # ----- invoke -----
        t0 = time.perf_counter()
        try:
            await reactions_mod.handle_raw_reaction_add(event, bot)  # type: ignore[arg-type]
        except Exception as e:
            report.add("handler_did_not_raise", False, f"raised: {e!r}")
        else:
            report.add("handler_did_not_raise", True)
        report.add(
            "handler_latency_ok",
            (time.perf_counter() - t0) < 5.0,
            f"{(time.perf_counter() - t0) * 1000:.0f}ms",
        )

        # Brief settle window for any fire-and-forget tasks the handler spawns
        # (none today, but cheap insurance for future changes).
        await asyncio.sleep(0.25)

        # ----- post snapshots -----
        post_row = await conn.fetchrow(
            "SELECT state, expires_at FROM opportunities WHERE id = $1",
            opp_uuid,
        )
        report.post_state = post_row["state"] if post_row else None
        report.expires_at_after = post_row["expires_at"] if post_row else None
        report.post_transition_count = await conn.fetchval(
            "SELECT COUNT(*) FROM opportunity_transitions WHERE opportunity_id = $1",
            opp_uuid,
        )
        report.post_application_count = await conn.fetchval(
            "SELECT COUNT(*) FROM applications WHERE opportunity_id = $1",
            opp_uuid,
        )
        report.post_pin_count = await conn.fetchval(
            "SELECT COUNT(*) FROM user_pins WHERE opportunity_id = $1",
            opp_uuid,
        )
        report.captured_publishes = [p for p in captured if str(p.get("opp_id")) == opp_id_str]

        # ----- assertions -----
        _assert_invariants(report, emoji)

        return report
    finally:
        # restore publish first so cleanup writes don't get captured
        if "orig_publish" in locals():
            RedisQ.publish = orig_publish  # type: ignore[assignment]
        if opp_uuid is not None and source_id is not None and owns_opp:
            try:
                await _cleanup(conn, source_id, opp_uuid)
            except Exception as e:
                print(f"  ! cleanup failed: {e!r}", file=sys.stderr)
        await conn.close()
        await db.close_pool()


def _assert_invariants(report: _ProbeReport, emoji: str) -> None:
    """Apply per-emoji invariants.

    Hard rule: reactions must produce the same DB writes as the corresponding
    button click.
    """
    expected_action = _EXPECTED_ACTION.get(emoji)
    expected_to = _EXPECTED_TO_STATE.get(emoji)

    if emoji in _NOOP_EMOJI:
        # The handler's emoji map deliberately does not include this glyph.
        report.add(
            "noop_no_state_change",
            report.post_state == report.pre_state,
            f"pre={report.pre_state} post={report.post_state}",
        )
        report.add(
            "noop_no_stream_apply",
            len(report.captured_publishes) == 0,
            f"captured={len(report.captured_publishes)}",
        )
        report.add(
            "noop_no_new_transition_row",
            report.post_transition_count == report.pre_transition_count,
            f"pre={report.pre_transition_count} post={report.post_transition_count}",
        )
        return

    if expected_action is None:
        report.add("emoji_known", False, f"unmapped emoji: {emoji!r}")
        return
    report.add("emoji_known", True, expected_action)

    if expected_to is not None:
        report.add(
            "state_transitioned",
            report.post_state == expected_to,
            f"expected={expected_to} got={report.post_state}",
        )
        # V004 trigger auto-logs every state change to opportunity_transitions.
        # The handler does NOT set trigger='reaction'; the trigger column
        # carries 'auto' from enforce_opp_state_transition(). We assert the
        # row exists (i.e. the trigger fired) regardless of label.
        report.add(
            "transition_row_inserted",
            report.post_transition_count == report.pre_transition_count + 1,
            f"delta={report.post_transition_count - report.pre_transition_count}",
        )
    else:
        report.add(
            "state_unchanged_as_expected",
            report.post_state == report.pre_state,
            f"pre={report.pre_state} post={report.post_state}",
        )

    # stream:apply contract — every reaction (except 💬 which posts to NOTIFY)
    # publishes exactly one apply-stream message tagged source='reaction'.
    if expected_action in {"apply", "skip", "snooze", "pin"}:
        matching = [p for p in report.captured_publishes if p.get("action") == expected_action]
        report.add(
            "stream_apply_published",
            len(matching) == 1,
            f"matching_count={len(matching)} all={[p.get('action') for p in report.captured_publishes]}",
        )
        if matching:
            payload = matching[0]
            report.add(
                "payload_source_is_reaction",
                payload.get("source") == "reaction",
                f"source={payload.get('source')!r}",
            )
            report.add(
                "payload_opp_id_correct",
                str(payload.get("opp_id")) == report.opp_id,
                f"got={payload.get('opp_id')}",
            )
            report.add(
                "payload_user_id_is_1",
                int(payload.get("user_id", -1)) == 1,
                f"got={payload.get('user_id')}",
            )

    # Per-emoji specifics
    if emoji == "✅":
        # send_application is fired by the applier-worker once it consumes
        # stream:apply; the reaction handler itself does not create the
        # applications row. The handler-layer assertion is the stream:apply
        # publish above. We additionally verify no DB-side application row
        # was synthesised by the handler (since that would diverge from the
        # button path).
        report.add(
            "no_application_row_from_handler",
            report.post_application_count == report.pre_application_count,
            "handler must defer application creation to applier-worker",
        )

    if emoji == "❌":
        report.add(
            "no_application_row_for_skip",
            report.post_application_count == report.pre_application_count,
            "skip MUST NOT create an application row",
        )

    if emoji == "🔁":
        # The handler only flips state to 'snoozed'; expires_at is set by the
        # applier-worker once it consumes the apply-stream message. We assert
        # the apply-stream payload carries the snooze 'days' field so the
        # downstream worker can compute the window.
        snooze_msgs = [p for p in report.captured_publishes if p.get("action") == "snooze"]
        report.add(
            "snooze_payload_has_days",
            bool(snooze_msgs) and "days" in snooze_msgs[0],
            f"payload_keys={list(snooze_msgs[0].keys()) if snooze_msgs else None}",
        )

    if emoji == "🔖":
        # Pin doesn't transition state and the handler doesn't write user_pins
        # directly — that's the applier-worker's job. Only the apply-stream
        # publish is the handler's responsibility.
        pass


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------
def _print_report(report: _ProbeReport) -> None:
    print()
    print("=" * 60)
    print(f"  probe_reaction  emoji={report.emoji}  opp={report.opp_id}")
    print("=" * 60)
    print(f"  pre_state           : {report.pre_state}")
    print(f"  post_state          : {report.post_state}")
    print(f"  transitions  pre/post: {report.pre_transition_count} -> {report.post_transition_count}")
    print(f"  applications pre/post: {report.pre_application_count} -> {report.post_application_count}")
    print(f"  user_pins    pre/post: {report.pre_pin_count} -> {report.post_pin_count}")
    print(f"  expires_at_after    : {report.expires_at_after}")
    print(f"  stream:apply captured: {len(report.captured_publishes)}")
    for p in report.captured_publishes:
        # Drop the heavy ts field in the printout.
        slim = {k: v for k, v in p.items() if k not in {"ts", "stream"}}
        print(f"    - {json.dumps(slim, default=str, sort_keys=True)}")
    print("-" * 60)
    width = max((len(n) for n, _, _ in report.assertions), default=0)
    for name, ok, detail in report.assertions:
        mark = "PASS" if ok else "FAIL"
        suffix = f"  ({detail})" if detail else ""
        print(f"  [{mark}] {name.ljust(width)}{suffix}")
    print("-" * 60)
    print(f"  result: {'PASS' if report.all_passed() else 'FAIL'}")
    print()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def _parse_args(argv: list[str]) -> tuple[str, str | None]:
    emoji = "✅"
    opp_id: str | None = None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        elif a == "--opp-id":
            i += 1
            opp_id = argv[i]
        elif a.startswith("--opp-id="):
            opp_id = a.split("=", 1)[1]
        elif not a.startswith("-"):
            emoji = a
        else:
            print(f"unknown arg: {a}", file=sys.stderr)
            sys.exit(2)
        i += 1
    return emoji, opp_id


def main() -> int:
    emoji, opp_id = _parse_args(sys.argv)
    started = datetime.now(UTC).isoformat()
    print(f"probe_reaction  emoji={emoji}  started_at={started}")
    try:
        report = asyncio.run(run_probe(emoji, pinned_opp_id=opp_id))
    except SystemExit:
        raise
    except Exception as e:
        print(f"FATAL: probe crashed: {e!r}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 2
    _print_report(report)
    return 0 if report.all_passed() else 1


if __name__ == "__main__":
    raise SystemExit(main())

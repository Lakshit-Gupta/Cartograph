"""Applier worker — consumes Streams.APPLY, dispatches user intents.

Discord button clicks, slash commands, and modal submissions publish onto
`stream:apply`. This worker is the single consumer in `g:appliers`. Each
payload carries an `action` discriminator; we route to a private handler.

Side effects:
- Mutates `opportunities.state` (skip / snooze) — V004 trigger logs transition.
- Inserts into `user_pins` / `user_prefs` (tables created on startup).
- Calls `src.application.sender.send_application` for apply / proposal_send.
- Publishes follow-up messages onto `Streams.NOTIFY` so the bot reflects
  the action in the tracker channels.
"""

from __future__ import annotations

import asyncio
import json
import signal
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from src.application.sender import send_application
from src.common.db import acquire, close_pool, fetch_one, init_pool
from src.common.logger import configure_logging, get_logger
from src.common.queue import Groups, RedisQ, Streams

configure_logging("applier")
_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Idempotent DDL — runs once at worker startup.
# ---------------------------------------------------------------------------
_DDL = """
CREATE TABLE IF NOT EXISTS user_pins (
    user_id        BIGINT NOT NULL,
    opportunity_id UUID   NOT NULL,
    pinned_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, opportunity_id)
);

CREATE TABLE IF NOT EXISTS user_prefs (
    user_id     BIGINT NOT NULL,
    key         TEXT   NOT NULL,
    value       JSONB  NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, key)
);
"""


async def _ensure_schema() -> None:
    async with acquire() as conn:
        await conn.execute(_DDL)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _opp_uuid(payload: dict[str, Any]) -> UUID:
    raw = payload.get("opp_id") or payload.get("opportunity_id")
    if not raw:
        raise ValueError("payload missing opp_id")
    return UUID(str(raw))


def _user_id(payload: dict[str, Any]) -> int:
    return int(payload.get("user_id", 1))


async def _tracker_update(q: RedisQ, *, user_id: int, opp_id: str, verb: str, extra: dict[str, Any] | None = None) -> None:
    msg = {
        "kind": "tracker_update",
        "user_id": user_id,
        "opp_id": opp_id,
        "verb": verb,
        "tracker": "applied",
        "message": f"opp `{opp_id}` {verb}",
    }
    if extra:
        msg.update(extra)
    await q.publish(Streams.NOTIFY, msg)


# ---------------------------------------------------------------------------
# Per-action handlers
# ---------------------------------------------------------------------------
async def _do_apply(q: RedisQ, payload: dict[str, Any]) -> None:
    opp_id = _opp_uuid(payload)
    # send_application handles tailoring + email/manual surfacing + its own
    # NOTIFY publish (`applied` or `manual_apply_ready`). Applier adds nothing.
    await send_application(opp_id)


async def _do_skip(q: RedisQ, payload: dict[str, Any]) -> None:
    opp_id = _opp_uuid(payload)
    async with acquire() as conn:
        await conn.execute(
            """
            UPDATE opportunities
               SET state = 'seen'
             WHERE id = $1
               AND state IN ('digested','ranked','queued','new','snoozed')
            """,
            opp_id,
        )
    await _tracker_update(q, user_id=_user_id(payload), opp_id=str(opp_id), verb="skipped")


async def _do_snooze(q: RedisQ, payload: dict[str, Any]) -> None:
    opp_id = _opp_uuid(payload)
    days = max(1, int(payload.get("days", 1)))
    async with acquire() as conn:
        await conn.execute(
            """
            UPDATE opportunities
               SET state = 'snoozed',
                   expires_at = NOW() + ($2 || ' days')::interval
             WHERE id = $1
            """,
            opp_id,
            str(days),
        )
    await _tracker_update(
        q,
        user_id=_user_id(payload),
        opp_id=str(opp_id),
        verb=f"snoozed {days}d",
    )


async def _do_pin(q: RedisQ, payload: dict[str, Any]) -> None:
    opp_id = _opp_uuid(payload)
    user_id = _user_id(payload)
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_pins (user_id, opportunity_id)
            VALUES ($1, $2)
            ON CONFLICT DO NOTHING
            """,
            user_id,
            opp_id,
        )
    await _tracker_update(q, user_id=user_id, opp_id=str(opp_id), verb="pinned")


async def _do_explain(q: RedisQ, payload: dict[str, Any]) -> None:
    opp_id = _opp_uuid(payload)
    user_id = _user_id(payload)
    row = await fetch_one(
        """
        SELECT score, score_components
          FROM opportunity_scores
         WHERE opportunity_id = $1
         ORDER BY scored_at DESC
         LIMIT 1
        """,
        opp_id,
    )
    if row is None:
        await q.publish(
            Streams.NOTIFY,
            {
                "kind": "explain_dm",
                "user_id": user_id,
                "opp_id": str(opp_id),
                "reason": "no score recorded yet",
                "components": {},
            },
        )
        return
    comps = row["score_components"]
    if isinstance(comps, str):
        try:
            comps = json.loads(comps)
        except Exception:
            comps = {}
    await q.publish(
        Streams.NOTIFY,
        {
            "kind": "explain_dm",
            "user_id": user_id,
            "opp_id": str(opp_id),
            "reason": f"score={float(row['score']):.2f}",
            "components": comps or {},
        },
    )


async def _do_budget_set(q: RedisQ, payload: dict[str, Any]) -> None:
    """Persist any subset of budget knobs into user_prefs.

    Accepts contract-style `min_score` AND modal-style `min_intern` /
    `min_ft` / `min_freelance_usd_hr`. Each lands under a stable key.
    """
    user_id = _user_id(payload)
    keys = {
        "min_score": "min_priority_score",
        "min_intern": "min_intern_inr_month",
        "min_ft": "min_ft_inr_month",
        "min_freelance_usd_hr": "min_freelance_usd_hr",
    }
    persisted: dict[str, Any] = {}
    async with acquire() as conn:
        for src_key, db_key in keys.items():
            if src_key not in payload:
                continue
            value = payload[src_key]
            await conn.execute(
                """
                INSERT INTO user_prefs (user_id, key, value)
                VALUES ($1, $2, $3::jsonb)
                ON CONFLICT (user_id, key) DO UPDATE
                   SET value = EXCLUDED.value,
                       updated_at = NOW()
                """,
                user_id,
                db_key,
                json.dumps(value),
            )
            persisted[db_key] = value
    _log.info("budget_set_persisted", user_id=user_id, persisted=persisted)


async def _do_source_add(q: RedisQ, payload: dict[str, Any]) -> None:
    url = str(payload.get("url") or "").strip()
    lane = str(payload.get("lane") or "other").strip().lower()
    if not url:
        raise ValueError("source_add missing url")

    host = (urlparse(url).hostname or url).lower()
    slug_base = host.replace(".", "_")[:48] or "manual"
    slug = f"manual_{slug_base}_{lane}"[:64]

    category_map = {
        "fulltime": "other",
        "internship": "other",
        "fellowship": "fellowship",
        "freelance": "freelance",
        "contract": "freelance",
    }
    category = category_map.get(lane, "other")

    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sources
                (slug, name, category, base_url, crawler_strategy,
                 fetch_freq_minutes, priority, status, created_via)
            VALUES ($1, $2, $3, $4, 'generic_html', 360, 5, 'paused', 'discord_modal')
            ON CONFLICT (slug) DO NOTHING
            """,
            slug,
            host,
            category,
            url,
        )

    await q.publish(
        Streams.ALERTS,
        {
            "kind": "alert",
            "alert": "source_add",
            "message": f"queued source `{slug}` (lane={lane}, status=paused) — `/source resume {slug}` to enable",
        },
    )


async def _do_proposal_send(q: RedisQ, payload: dict[str, Any]) -> None:
    """Freelance proposal: user-edited cover passed through sender.

    The modal publishes action=`freelance_send_proposal` with pitch/rate/cta;
    the contract also names a flat `body` field. Whichever shows up, we coerce
    into a single override markdown blob and feed sender.
    """
    opp_id = _opp_uuid(payload)
    body = payload.get("body")
    if not body:
        parts: list[str] = []
        pitch = (payload.get("pitch") or "").strip()
        rate = (payload.get("rate") or "").strip()
        cta = (payload.get("cta") or "").strip()
        if pitch:
            parts.append(pitch)
        if rate:
            parts.append(f"Rate: {rate}")
        if cta:
            parts.append(cta)
        body = "\n\n".join(parts).strip() or None

    await send_application(opp_id, override_cover_markdown=body)


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------
HandlerFn = Callable[[RedisQ, dict[str, Any]], Awaitable[None]]

_DISPATCH: dict[str, HandlerFn] = {
    "apply": _do_apply,
    "skip": _do_skip,
    "snooze": _do_snooze,
    "pin": _do_pin,
    "explain": _do_explain,
    "budget_set": _do_budget_set,
    "source_add": _do_source_add,
    "proposal_send": _do_proposal_send,
    "freelance_send_proposal": _do_proposal_send,
}


# ---------------------------------------------------------------------------
# Worker entrypoint
# ---------------------------------------------------------------------------
async def _process(q: RedisQ, payload: dict[str, Any]) -> None:
    action = str(payload.get("action") or "").lower()
    handler = _DISPATCH.get(action)
    if handler is None:
        _log.warning("applier_unknown_action", action=action, payload=payload)
        return
    _log.info("applier_action_start", action=action, opp_id=payload.get("opp_id"), user_id=payload.get("user_id"))
    await handler(q, payload)
    _log.info("applier_action_done", action=action, opp_id=payload.get("opp_id"))


async def _warm_fallback_pdf_if_enabled() -> None:
    """Pre-compile the untailored resume PDF at worker boot.

    Without this, `_send_with_latex` has no fallback to attach when a
    tailored compile fails (sanitizer reject, render bug, source drift,
    package fetch timeout). The compile takes ~5 s on cold cache and is
    cached on disk afterward; subsequent appliers reuse the cached PDF.

    Skipped silently when MP_RESUME_LATEX_ENABLED is false.
    """
    from src.application.sender import _manifest_path, _resume_root, is_latex_enabled

    if not is_latex_enabled():
        return
    try:
        from src.application.resume_latex.fallback import warm_fallback_pdf
        from src.application.resume_latex.parser.manifest import load as load_manifest

        manifest = load_manifest(_manifest_path())
        path = await warm_fallback_pdf(user_id=1, resume_root=_resume_root(), main_file=manifest.main_file)
    except Exception as e:
        _log.warning("fallback_warmup_failed", err=str(e))
        return
    if path is None:
        _log.warning("fallback_warmup_returned_none")
    else:
        _log.info("fallback_warmup_ok", path=str(path))


async def main() -> None:
    await init_pool()
    await _ensure_schema()
    q = await RedisQ.connect()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    await _warm_fallback_pdf_if_enabled()
    _log.info("applier_started")
    async for msg in q.consume(Streams.APPLY, Groups.APPLIERS):
        if stop.is_set():
            break
        try:
            await _process(q, msg.fields)
        except Exception as e:
            _log.exception("applier_process_failed", err=str(e), payload=msg.fields)
            await q.dlq(Streams.APPLY, msg.msg_id, msg.fields, str(e))
        await q.ack(Streams.APPLY, Groups.APPLIERS, msg.msg_id)

    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

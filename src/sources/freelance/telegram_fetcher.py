"""Freelance Telegram channel fetcher.

Long-running Telethon client. Reads a pre-authorised SQLite session (created
by `scripts/telegram_auth.py`), subscribes to a configured list of public
freelance channels, parses each new message into an `Opportunity`, and
publishes directly onto `stream:rank` via `persist_and_publish` — bypassing
the crawler / extractor tiers (Telegram is structured-enough source-side).

Hard constraints (see task brief + CLAUDE.md):
  * Import-clean even when the .session file is missing. Runtime errors at
    `client.connect()` are logged + retried with exponential backoff.
  * `fingerprint_hash = sha256(f"{chat.username}:{message.id}")` — stable
    across restarts. Never include timestamps.
  * FloodWaitError handled with exponential sleep capped at 5 min.
  * No cross-subsystem imports — only `src.common` + `src.extractors.persist`
    (the single canonical write path shared with the gmail Upwork lane).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.common.db import acquire, close_pool, init_pool
from src.common.logger import get_logger
from src.common.queue import RedisQ
from src.common.secrets import get_settings
from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType
from src.extractors.persist import persist_and_publish

_log = get_logger(__name__)

# ----- tuning constants ------------------------------------------------------

# Backoff schedule for FloodWaitError / connection bounces (seconds). Capped
# at `_MAX_BACKOFF`; chosen so worst-case pause matches Telegram's typical
# server-side cooldown (5 min) without compounding past it.
_BACKOFF_SHORT_SEC = 30
_BACKOFF_MEDIUM_SEC = 60
_BACKOFF_LONG_SEC = 120
_BACKOFF_EXTENDED_SEC = 240
_MAX_BACKOFF = 300
_BACKOFF_SCHEDULE: tuple[int, ...] = (
    _BACKOFF_SHORT_SEC,
    _BACKOFF_MEDIUM_SEC,
    _BACKOFF_LONG_SEC,
    _BACKOFF_EXTENDED_SEC,
    _MAX_BACKOFF,
)

# Telegram message size — Telegram caps at 4096 chars, we cap at 2000 in DB.
_DESC_MAX = 2000
_TITLE_MAX = 200

# SQLState for postgres UNIQUE violation — emitted by asyncpg on dupe
# canonical_url races. Treat as a silent dedupe skip, not an error.
_PG_UNIQUE_VIOLATION_SQLSTATE = "23505"

# Telethon appends `.session` to whatever stem it gets. Strip our suffix
# before handing it the path so it doesn't double up.
_SESSION_SUFFIX = ".session"

# Confidence score for tier-0 structured Telegram extraction. Higher than the
# generic regex floor because channel posts follow a predictable shape.
_TG_EXTRACTION_CONFIDENCE = 0.6

# ----- compensation regexes --------------------------------------------------

# Compensation hints. Order matters: hourly first (more specific), then range,
# then bare USD, then bare INR. Each returns (lo, hi, currency, period).
_RX_HOURLY_USD = re.compile(r"\$\s?(\d[\d,]*)(?:\s*[-–]\s*\$?(\d[\d,]*))?\s*/\s*hr", re.I)
_RX_HOURLY_INR = re.compile(r"(?:₹|INR)\s?(\d[\d,]*)(?:\s*[-–]\s*(?:₹|INR)?(\d[\d,]*))?\s*/\s*hr", re.I)
_RX_RANGE_USD = re.compile(r"\$\s?(\d[\d,]*)\s*[-–to]+\s*\$?\s?(\d[\d,]*)")
_RX_RANGE_INR = re.compile(r"(?:₹|INR)\s?(\d[\d,]*)\s*[-–to]+\s*(?:₹|INR)?\s?(\d[\d,]*)")
_RX_BARE_USD = re.compile(r"\$\s?(\d[\d,]*)")
_RX_BARE_INR = re.compile(r"(?:₹\s?(\d[\d,]*)|(\d[\d,]*)\s*INR)", re.I)

_EMPTY_COMP: dict[str, Any] = {
    "comp_min": None,
    "comp_max": None,
    "comp_currency": None,
    "comp_period": None,
}


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    """Structured view of a Telegram channel message used for `Opportunity`."""

    title: str
    description: str
    canonical_url: str
    fingerprint_hash: str
    comp_min: float | None
    comp_max: float | None
    comp_currency: str | None
    comp_period: str | None


# ----- pure helpers (unit-tested without Telethon) ---------------------------


def _normalise_channel(handle: str) -> str:
    """Accept `@foo`, `t.me/foo`, `https://t.me/foo`, or `foo` — emit `foo`."""
    h = handle.strip()
    if not h:
        return ""
    if h.startswith("http"):
        h = h.split("t.me/", 1)[-1]
    elif h.startswith("t.me/"):
        h = h[len("t.me/") :]
    return h.lstrip("@").rstrip("/")


def _fingerprint(channel: str, message_id: int) -> str:
    """sha256(channel:message_id) — deterministic, restart-safe."""
    return hashlib.sha256(f"{channel}:{message_id}".encode()).hexdigest()


def _strip_emoji_noise(text: str) -> str:
    """Drop zero-width joiners + variation selectors; collapse blank runs.

    Telegram-channel boilerplate is heavy on emoji and decorative dashes.
    Keep emoji characters (they often carry context) but normalise spacing.
    """
    cleaned = text.replace("‍", "").replace("️", "")
    # Collapse 3+ newlines into 2 (paragraph break).
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _first_nonempty_line(text: str, *, cap: int = _TITLE_MAX) -> str:
    for raw in text.splitlines():
        line = raw.strip(" \t•○●-—–*#>")
        if line:
            return line[:cap]
    return ""


def _to_float(s: str) -> float:
    """Strip thousands separators and cast to float."""
    return float(s.replace(",", ""))


def _hourly_match(text: str, rx: re.Pattern[str], currency: str) -> dict[str, Any] | None:
    m = rx.search(text)
    if not m:
        return None
    lo = _to_float(m.group(1))
    hi = _to_float(m.group(2)) if m.group(2) else lo
    return {"comp_min": lo, "comp_max": hi, "comp_currency": currency, "comp_period": "hour"}


def _range_match(text: str, rx: re.Pattern[str], currency: str) -> dict[str, Any] | None:
    m = rx.search(text)
    if not m:
        return None
    return {
        "comp_min": _to_float(m.group(1)),
        "comp_max": _to_float(m.group(2)),
        "comp_currency": currency,
        "comp_period": None,
    }


def _bare_usd_match(text: str) -> dict[str, Any] | None:
    m = _RX_BARE_USD.search(text)
    if not m:
        return None
    v = _to_float(m.group(1))
    return {"comp_min": v, "comp_max": v, "comp_currency": "USD", "comp_period": None}


def _bare_inr_match(text: str) -> dict[str, Any] | None:
    m = _RX_BARE_INR.search(text)
    if not m:
        return None
    v = _to_float(m.group(1) or m.group(2))
    return {"comp_min": v, "comp_max": v, "comp_currency": "INR", "comp_period": None}


def _parse_compensation(text: str) -> dict[str, Any]:
    """Best-effort rate extraction. Order: hourly USD/INR → range → bare."""
    extractors = (
        lambda t: _hourly_match(t, _RX_HOURLY_USD, "USD"),
        lambda t: _hourly_match(t, _RX_HOURLY_INR, "INR"),
        lambda t: _range_match(t, _RX_RANGE_USD, "USD"),
        lambda t: _range_match(t, _RX_RANGE_INR, "INR"),
        _bare_usd_match,
        _bare_inr_match,
    )
    for extractor in extractors:
        result = extractor(text)
        if result is not None:
            return result
    return dict(_EMPTY_COMP)


def parse_message(*, channel: str, message_id: int, text: str) -> ParsedMessage | None:
    """Pure parser. Returns None when the text is empty or has no title."""
    if not text or not text.strip():
        return None
    cleaned = _strip_emoji_noise(text)
    title = _first_nonempty_line(cleaned)
    if not title:
        return None
    description = cleaned[:_DESC_MAX]
    comp = _parse_compensation(cleaned)
    return ParsedMessage(
        title=title,
        description=description,
        canonical_url=f"https://t.me/{channel}/{message_id}",
        fingerprint_hash=_fingerprint(channel, message_id),
        **comp,
    )


def build_opportunity(parsed: ParsedMessage, *, source_id: int) -> Opportunity:
    """Wrap a `ParsedMessage` in the canonical `Opportunity` payload."""
    return Opportunity(
        source_id=source_id,
        canonical_url=parsed.canonical_url,
        title=parsed.title,
        company=None,  # not surfaced in channel posts
        description=parsed.description,
        comp_min=parsed.comp_min,
        comp_max=parsed.comp_max,
        comp_currency=parsed.comp_currency,
        comp_period=parsed.comp_period,
        location=None,
        remote_type=RemoteType.REMOTE,
        category=OppCategory.FREELANCE,
        posted_at=None,
        apply_url=parsed.canonical_url,
        apply_method=ApplyMethod.EXTERNAL,
        fingerprint_hash=parsed.fingerprint_hash,
        extraction_tier=0,
        extraction_confidence=_TG_EXTRACTION_CONFIDENCE,
    )


# ----- config + db lookups ---------------------------------------------------


def load_channels_from_prefs() -> list[str]:
    """Read freelance.telegram_channels from prefs.yaml. Empty list on miss."""
    settings = get_settings()
    prefs_path = Path(settings.config_root) / "profile" / "prefs.yaml"
    if not prefs_path.exists():
        return []
    try:
        data = yaml.safe_load(prefs_path.read_text()) or {}
    except yaml.YAMLError as e:
        _log.warning("tg_prefs_parse_failed", err=str(e))
        return []
    raw = (data.get("freelance") or {}).get("telegram_channels") or []
    if not isinstance(raw, list):
        return []
    return [c for c in (_normalise_channel(str(x)) for x in raw) if c]


async def resolve_source_id() -> int | None:
    async with acquire() as conn:
        rec = await conn.fetchrow("SELECT id FROM sources WHERE crawler_strategy = 'freelance_telegram' LIMIT 1")
    return int(rec["id"]) if rec else None


# ----- runtime ---------------------------------------------------------------


async def _publish_with_dedupe(q: RedisQ, opp: Opportunity, *, channel: str, message_id: int) -> None:
    """Persist + publish. Swallow unique-violation (dedupe) at debug level."""
    try:
        opp_id = await persist_and_publish(q, opp)
        if opp_id is None:
            _log.debug("tg_dedupe_skip", channel=channel, message_id=message_id)
            return
        _log.info(
            "tg_opportunity_published",
            channel=channel,
            message_id=message_id,
            opportunity_id=str(opp_id),
        )
    except Exception as e:
        # asyncpg.UniqueViolationError surfaces as exc with sqlstate == '23505'.
        # canonical_url has a UNIQUE constraint (V001), so a race on the same
        # message will land here. Swallow at debug; everything else escalates.
        sqlstate = getattr(e, "sqlstate", None)
        if sqlstate == _PG_UNIQUE_VIOLATION_SQLSTATE:
            _log.debug("tg_dedupe_skip", channel=channel, message_id=message_id, sqlstate=sqlstate)
            return
        _log.exception("tg_publish_failed", channel=channel, message_id=message_id, err=str(e))


def _resolve_session_path() -> Path:
    """Find the .session file. Prefer explicit env path, then var/telegram/<stem>."""
    settings = get_settings()
    if settings.telegram_session_path:
        return Path(settings.telegram_session_path)
    # Local-dev default: ./var/telegram/<stem>.session relative to repo root.
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "var" / "telegram" / f"{settings.telegram_session_name}.session"


def _session_stem(session_path: Path) -> str:
    """Telethon appends `.session` itself — strip ours if present."""
    s = str(session_path)
    if s.endswith(_SESSION_SUFFIX):
        return s[: -len(_SESSION_SUFFIX)]
    return s


def _import_telethon() -> tuple[Any, Any, Any] | None:
    """Lazy import. Returns (TelegramClient, events, FloodWaitError) or None.

    Telethon is imported here — not at module load — so unit tests can mock
    this fetcher without telethon's optional native deps being available.
    """
    try:
        from telethon import TelegramClient, events
        from telethon.errors import FloodWaitError
    except ImportError as e:
        _log.error("tg_telethon_missing", err=str(e))
        return None
    return TelegramClient, events, FloodWaitError


async def _handle_event(event: Any, q: RedisQ, source_id: int) -> None:
    """Parse one NewMessage event and publish. Errors logged, never raised."""
    try:
        chat = await event.get_chat()
        channel = getattr(chat, "username", None) or str(getattr(chat, "id", "unknown"))
        message_id = int(event.message.id)
        text = event.message.text or event.message.message or ""
        _log.info(
            "tg_message_received",
            channel=channel,
            message_id=message_id,
            length=len(text),
        )
        parsed = parse_message(channel=channel, message_id=message_id, text=text)
        if parsed is None:
            _log.debug("tg_skip_empty", channel=channel, message_id=message_id)
            return
        opp = build_opportunity(parsed, source_id=source_id)
        await _publish_with_dedupe(q, opp, channel=channel, message_id=message_id)
    except Exception as e:
        _log.exception("tg_handler_failed", err=str(e))


def _register_handler(client: Any, events: Any, *, channels: list[str], q: RedisQ, source_id: int) -> None:
    """Attach NewMessage handler to the client for the given channel set."""

    @client.on(events.NewMessage(chats=channels))
    async def _handler(event: Any) -> None:
        await _handle_event(event, q, source_id)


async def _ensure_authorised(client: Any) -> bool:
    """Connect + auth-check. Returns True on success, False otherwise."""
    await client.connect()
    if not await client.is_user_authorized():
        _log.error("tg_not_authorised", hint="re-run scripts/telegram_auth.py")
        return False
    me = await client.get_me()
    _log.info("tg_authorised", user_id=getattr(me, "id", None))
    return True


def _next_backoff(backoff_idx: int) -> tuple[int, int]:
    """Return (sleep_seconds, next_idx) from the backoff schedule."""
    sleep_s = _BACKOFF_SCHEDULE[min(backoff_idx, len(_BACKOFF_SCHEDULE) - 1)]
    next_idx = min(backoff_idx + 1, len(_BACKOFF_SCHEDULE) - 1)
    return sleep_s, next_idx


async def _safe_disconnect(client: Any) -> None:
    """Best-effort disconnect — never raise."""
    try:
        await client.disconnect()
    except Exception:
        pass


async def _run_session(
    *,
    client: Any,
    events: Any,
    channels: list[str],
    q: RedisQ,
    source_id: int | None,
) -> None:
    """One session lifecycle: connect, register, run until disconnected."""
    if not await _ensure_authorised(client):
        await asyncio.sleep(_MAX_BACKOFF)
        return
    if channels and source_id is not None:
        _register_handler(client, events, channels=channels, q=q, source_id=source_id)
    await client.run_until_disconnected()
    _log.warning("tg_disconnected_retry")


async def run() -> None:
    """Worker entrypoint. Idempotent + restart-safe."""
    settings = get_settings()
    _log.info("tg_fetcher_started", session_name=settings.telegram_session_name)

    channels = load_channels_from_prefs()
    if not channels:
        _log.info("tg_no_channels_configured")
    else:
        _log.info("tg_channels_configured", count=len(channels), channels=channels)

    # DB + Redis come up regardless of channel config so health checks pass.
    await init_pool()
    q = await RedisQ.connect()

    source_id = await resolve_source_id()
    if source_id is None:
        _log.warning("tg_source_id_missing", strategy="freelance_telegram")

    session_path = _resolve_session_path()
    if not session_path.exists():
        _log.warning("tg_session_missing", path=str(session_path))

    telethon = _import_telethon()
    if telethon is None:
        await close_pool()
        return
    TelegramClient, events, FloodWaitError = telethon

    backoff_idx = 0
    stem = _session_stem(session_path)
    while True:
        client = TelegramClient(stem, settings.telegram_api_id, settings.telegram_api_hash)
        try:
            await _run_session(
                client=client,
                events=events,
                channels=channels,
                q=q,
                source_id=source_id,
            )
            backoff_idx = 0  # successful run resets backoff
        except FloodWaitError as e:
            wait = min(getattr(e, "seconds", _MAX_BACKOFF) or _MAX_BACKOFF, _MAX_BACKOFF)
            _log.warning("tg_flood_wait", seconds=wait)
            await asyncio.sleep(wait)
        except (asyncio.CancelledError, KeyboardInterrupt):
            _log.info("tg_shutdown")
            await _safe_disconnect(client)
            break
        except Exception as e:
            sleep_s, backoff_idx = _next_backoff(backoff_idx)
            _log.exception("tg_loop_error", err=str(e), backoff_seconds=sleep_s)
            await _safe_disconnect(client)
            await asyncio.sleep(sleep_s)

    await close_pool()

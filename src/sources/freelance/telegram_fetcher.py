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

# Backoff schedule for FloodWaitError / connection bounces (seconds).
_BACKOFF_SCHEDULE: tuple[int, ...] = (30, 60, 120, 240, 300)
_MAX_BACKOFF = 300

# Telegram message size — Telegram caps at 4096 chars, we cap at 2000 in DB.
_DESC_MAX = 2000
_TITLE_MAX = 200

# Compensation hints. Order matters: hourly first (more specific), then range,
# then bare USD, then bare INR. Each returns (lo, hi, currency, period).
_RX_HOURLY_USD = re.compile(r"\$\s?(\d[\d,]*)(?:\s*[-–]\s*\$?(\d[\d,]*))?\s*/\s*hr", re.I)
_RX_HOURLY_INR = re.compile(r"(?:₹|INR)\s?(\d[\d,]*)(?:\s*[-–]\s*(?:₹|INR)?(\d[\d,]*))?\s*/\s*hr", re.I)
_RX_RANGE_USD = re.compile(r"\$\s?(\d[\d,]*)\s*[-–to]+\s*\$?\s?(\d[\d,]*)")
_RX_RANGE_INR = re.compile(r"(?:₹|INR)\s?(\d[\d,]*)\s*[-–to]+\s*(?:₹|INR)?\s?(\d[\d,]*)")
_RX_BARE_USD = re.compile(r"\$\s?(\d[\d,]*)")
_RX_BARE_INR = re.compile(r"(?:₹\s?(\d[\d,]*)|(\d[\d,]*)\s*INR)", re.I)


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


def _parse_compensation(text: str) -> dict[str, Any]:
    """Best-effort rate extraction. Order: hourly USD/INR → range → bare."""

    def _f(s: str) -> float:
        return float(s.replace(",", ""))

    if m := _RX_HOURLY_USD.search(text):
        lo = _f(m.group(1))
        hi = _f(m.group(2)) if m.group(2) else lo
        return {"comp_min": lo, "comp_max": hi, "comp_currency": "USD", "comp_period": "hour"}
    if m := _RX_HOURLY_INR.search(text):
        lo = _f(m.group(1))
        hi = _f(m.group(2)) if m.group(2) else lo
        return {"comp_min": lo, "comp_max": hi, "comp_currency": "INR", "comp_period": "hour"}
    if m := _RX_RANGE_USD.search(text):
        return {
            "comp_min": _f(m.group(1)),
            "comp_max": _f(m.group(2)),
            "comp_currency": "USD",
            "comp_period": None,
        }
    if m := _RX_RANGE_INR.search(text):
        return {
            "comp_min": _f(m.group(1)),
            "comp_max": _f(m.group(2)),
            "comp_currency": "INR",
            "comp_period": None,
        }
    if m := _RX_BARE_USD.search(text):
        v = _f(m.group(1))
        return {"comp_min": v, "comp_max": v, "comp_currency": "USD", "comp_period": None}
    if m := _RX_BARE_INR.search(text):
        v = _f(m.group(1) or m.group(2))
        return {"comp_min": v, "comp_max": v, "comp_currency": "INR", "comp_period": None}
    return {"comp_min": None, "comp_max": None, "comp_currency": None, "comp_period": None}


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
        extraction_confidence=0.6,
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
        if sqlstate == "23505":
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

    # Telethon imported lazily — never at module import — so unit tests can
    # mock the fetcher without telethon's optional native deps loading.
    try:
        from telethon import TelegramClient, events
        from telethon.errors import FloodWaitError
    except ImportError as e:
        _log.error("tg_telethon_missing", err=str(e))
        await close_pool()
        return

    backoff_idx = 0
    while True:
        # Strip `.session` suffix when handing to Telethon — it appends it.
        session_stem = str(session_path)
        if session_stem.endswith(".session"):
            session_stem = session_stem[: -len(".session")]
        client = TelegramClient(session_stem, settings.telegram_api_id, settings.telegram_api_hash)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                _log.error("tg_not_authorised", hint="re-run scripts/telegram_auth.py")
                await asyncio.sleep(_MAX_BACKOFF)
                continue
            me = await client.get_me()
            _log.info("tg_authorised", user_id=getattr(me, "id", None))

            if channels and source_id is not None:

                @client.on(events.NewMessage(chats=channels))
                async def _handler(event):
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

            backoff_idx = 0  # successful connect resets backoff
            await client.run_until_disconnected()
            _log.warning("tg_disconnected_retry")
        except FloodWaitError as e:
            wait = min(getattr(e, "seconds", _MAX_BACKOFF) or _MAX_BACKOFF, _MAX_BACKOFF)
            _log.warning("tg_flood_wait", seconds=wait)
            await asyncio.sleep(wait)
        except (asyncio.CancelledError, KeyboardInterrupt):
            _log.info("tg_shutdown")
            try:
                await client.disconnect()
            except Exception:
                pass
            break
        except Exception as e:
            sleep_s = _BACKOFF_SCHEDULE[min(backoff_idx, len(_BACKOFF_SCHEDULE) - 1)]
            backoff_idx = min(backoff_idx + 1, len(_BACKOFF_SCHEDULE) - 1)
            _log.exception("tg_loop_error", err=str(e), backoff_seconds=sleep_s)
            try:
                await client.disconnect()
            except Exception:
                pass
            await asyncio.sleep(sleep_s)

    await close_pool()

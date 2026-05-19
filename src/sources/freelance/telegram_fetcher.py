"""Freelance Telegram channel fetcher — public module.

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

This module is the public surface tests / workers import as
`telegram_fetcher`. The heavy lifting (parser, channels, handler, listener
loop) lives in the `telegram/` subpackage; this file hosts the worker
entrypoint + dedupe publisher so `monkeypatch.setattr(tf, ...)` in
`tests/sources/test_telegram_fetcher.py` keeps reaching the names that
`run()` / `_publish_with_dedupe` actually look up.
"""

from __future__ import annotations

from typing import Any

from src.common.db import close_pool, init_pool
from src.common.logger import get_logger
from src.common.queue import RedisQ
from src.common.secrets import get_settings
from src.common.types import Opportunity
from src.extractors.persist import persist_and_publish

# Re-exports — module-level rebinds so `monkeypatch.setattr(tf, name, ...)`
# patches the same binding that `run()` / `_publish_with_dedupe` resolve.
# Names below are surfaced on this module's namespace for tests + back-compat;
# ruff F401 fires because we don't *call* them inside this file.
from src.sources.freelance.telegram.channels import (
    load_channels_from_prefs,
    resolve_source_id,
)
from src.sources.freelance.telegram.loop import (
    _resolve_session_path,
    _run_listener_loop,
)
from src.sources.freelance.telegram.parser import (
    ParsedMessage,
    _fingerprint,  # noqa: F401 — re-export for tests
    _normalise_channel,  # noqa: F401 — re-export for tests
    build_opportunity,
    parse_message,
)

_log = get_logger(__name__)

# SQLState for postgres UNIQUE violation — emitted by asyncpg on dupe
# canonical_url races. Treat as a silent dedupe skip, not an error.
_PG_UNIQUE_VIOLATION_SQLSTATE = "23505"


# ---- publish path (kept in this module so persist_and_publish is patchable) -


async def _publish_with_dedupe(
    q: RedisQ,
    opp: Opportunity,
    *,
    channel: str,
    message_id: int,
) -> None:
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


async def _publish_adapter(q: RedisQ, opp: Opportunity, channel: str, message_id: int) -> None:
    """Positional-args adapter so the handler can call `_publish_with_dedupe`.

    Kept thin so monkeypatched `_publish_with_dedupe` (or
    `persist_and_publish`) on this module remains the live binding the
    handler ends up invoking — preserving the test surface intact.
    """
    await _publish_with_dedupe(q, opp, channel=channel, message_id=message_id)


# ---- telethon lazy import + run() ------------------------------------------


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


def _log_boot_channels(channels: list[str]) -> None:
    """Emit boot-time channel inventory logs (byte-identical to pre-refactor)."""
    if not channels:
        _log.info("tg_no_channels_configured")
    else:
        _log.info("tg_channels_configured", count=len(channels), channels=channels)


async def _resolve_source() -> int | None:
    """Resolve the telegram source row id, logging the missing case (warn)."""
    source_id = await resolve_source_id()
    if source_id is None:
        _log.warning("tg_source_id_missing", strategy="freelance_telegram")
    return source_id


async def run() -> None:
    """Worker entrypoint. Idempotent + restart-safe."""
    settings = get_settings()
    _log.info("tg_fetcher_started", session_name=settings.telegram_session_name)

    channels = load_channels_from_prefs()
    _log_boot_channels(channels)

    # DB + Redis come up regardless of channel config so health checks pass.
    await init_pool()
    q = await RedisQ.connect()

    source_id = await _resolve_source()

    session_path = _resolve_session_path()
    if not session_path.exists():
        _log.warning("tg_session_missing", path=str(session_path))

    telethon = _import_telethon()
    if telethon is None:
        await close_pool()
        return
    TelegramClient, events, FloodWaitError = telethon

    try:
        await _run_listener_loop(
            TelegramClient=TelegramClient,
            events=events,
            FloodWaitError=FloodWaitError,
            channels=channels,
            q=q,
            source_id=source_id,
            session_path=session_path,
            publish=_publish_adapter,
        )
    finally:
        await close_pool()


# Re-export for tests/back-compat. Tests touch `_publish_with_dedupe`,
# `parse_message`, `build_opportunity`, `_fingerprint`, `_normalise_channel`,
# `load_channels_from_prefs`, `persist_and_publish`, `run`.
__all__: tuple[str, ...] = (
    "ParsedMessage",
    "build_opportunity",
    "load_channels_from_prefs",
    "parse_message",
    "persist_and_publish",
    "resolve_source_id",
    "run",
)

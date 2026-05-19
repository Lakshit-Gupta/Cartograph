"""Listener loop, backoff curve, FloodWaitError handling.

`_run_listener_loop` is the worker's forever-loop after boot: connect
the Telethon client, register the channel handler, run until disconnect
or error, sleep on the exponential backoff schedule, retry. On
`FloodWaitError` it honours the server-supplied wait (capped at 5 min).

All logging keys (`tg_*`) are byte-identical to pre-refactor telegram_fetcher
because Grafana dashboards key on them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from src.common.logger import get_logger
from src.common.queue import RedisQ
from src.common.secrets import get_settings
from src.common.types import Opportunity

from .handler import _attach_handler

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

# Telethon appends `.session` to whatever stem it gets. Strip our suffix
# before handing it the path so it doesn't double up.
_SESSION_SUFFIX = ".session"


def _resolve_session_path() -> Path:
    """Find the .session file. Prefer explicit env path, then var/telegram/<stem>."""
    settings = get_settings()
    if settings.telegram_session_path:
        return Path(settings.telegram_session_path)
    # Local-dev default: ./var/telegram/<stem>.session relative to repo root.
    # Path: src/sources/freelance/telegram/loop.py -> parents[4] is repo root.
    repo_root = Path(__file__).resolve().parents[4]
    return repo_root / "var" / "telegram" / f"{settings.telegram_session_name}.session"


def _session_stem(session_path: Path) -> str:
    """Telethon appends `.session` itself — strip ours if present."""
    s = str(session_path)
    if s.endswith(_SESSION_SUFFIX):
        return s[: -len(_SESSION_SUFFIX)]
    return s


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


async def _ensure_authorised(client: Any) -> bool:
    """Connect + auth-check. Returns True on success, False otherwise."""
    await client.connect()
    if not await client.is_user_authorized():
        _log.error("tg_not_authorised", hint="re-run scripts/telegram_auth.py")
        return False
    me = await client.get_me()
    _log.info("tg_authorised", user_id=getattr(me, "id", None))
    return True


async def _run_session(
    *,
    client: Any,
    events: Any,
    channels: list[str],
    q: RedisQ,
    source_id: int | None,
    publish: Callable[[RedisQ, Opportunity, str, int], Awaitable[None]],
) -> None:
    """One session lifecycle: connect, register, run until disconnected."""
    if not await _ensure_authorised(client):
        await asyncio.sleep(_MAX_BACKOFF)
        return
    if channels and source_id is not None:
        _attach_handler(
            client,
            events,
            channels=channels,
            q=q,
            source_id=source_id,
            publish=publish,
        )
    await client.run_until_disconnected()
    _log.warning("tg_disconnected_retry")


async def _run_listener_loop(
    *,
    TelegramClient: Any,
    events: Any,
    FloodWaitError: type[BaseException],
    channels: list[str],
    q: RedisQ,
    source_id: int | None,
    session_path: Path,
    publish: Callable[[RedisQ, Opportunity, str, int], Awaitable[None]],
) -> None:
    """Forever listener loop. Connect → register → run → backoff → retry.

    Returns when the caller cancels (KeyboardInterrupt / CancelledError);
    every other exception class is logged + backed off + retried.
    """
    settings = get_settings()
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
                publish=publish,
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

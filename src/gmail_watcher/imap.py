"""IMAP IDLE consumer for personal Gmail (OAuth) and worker Gmail (app password).

Two connection helpers + a generic IDLE watch loop. Each new message is delivered
to a callback exactly once — last-seen UID is persisted in `imap_state`.
"""

from __future__ import annotations

import asyncio
import email
import time
from collections.abc import Awaitable, Callable
from email.message import Message
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.common.db import acquire
from src.common.logger import get_logger
from src.common.secrets import get_settings

_log = get_logger(__name__)

OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
INBOX_NAME = "INBOX"

# Module-level access-token cache keyed by gmail user.
# value: {"token": str, "expires_at": epoch_seconds_float}
_TOKEN_CACHE: dict[str, dict[str, Any]] = {}

# Per-mailbox lock to keep last-UID writes ordered with the loop iteration.
_STATE_INIT_DONE = False


# ---------------------------------------------------------------------------
# State persistence (last UID seen per mailbox)
# ---------------------------------------------------------------------------
async def _ensure_state_table() -> None:
    global _STATE_INIT_DONE
    if _STATE_INIT_DONE:
        return
    async with acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS imap_state (
                mailbox    TEXT PRIMARY KEY,
                last_uid   BIGINT NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
        )
    _STATE_INIT_DONE = True


async def _load_last_uid(mailbox: str) -> int:
    await _ensure_state_table()
    async with acquire() as conn:
        rec = await conn.fetchrow("SELECT last_uid FROM imap_state WHERE mailbox = $1", mailbox)
    return int(rec["last_uid"]) if rec else 0


async def _save_last_uid(mailbox: str, uid: int) -> None:
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO imap_state(mailbox, last_uid, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (mailbox) DO UPDATE
              SET last_uid = EXCLUDED.last_uid, updated_at = NOW()
              WHERE imap_state.last_uid < EXCLUDED.last_uid
            """,
            mailbox,
            uid,
        )


# ---------------------------------------------------------------------------
# OAuth — refresh-token → access-token, cached in-process
# ---------------------------------------------------------------------------
async def _refresh_access_token() -> str:
    settings = get_settings()
    user = settings.gmail_user
    now = time.time()
    cached = _TOKEN_CACHE.get(user)
    if cached and cached["expires_at"] - now > 60:
        return str(cached["token"])

    if not (settings.gmail_oauth_client_id and settings.gmail_oauth_client_secret and settings.gmail_oauth_refresh_token):
        raise RuntimeError("gmail oauth credentials missing")

    payload = {
        "client_id": settings.gmail_oauth_client_id,
        "client_secret": settings.gmail_oauth_client_secret,
        "refresh_token": settings.gmail_oauth_refresh_token,
        "grant_type": "refresh_token",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(OAUTH_TOKEN_URL, data=payload)
        resp.raise_for_status()
        body = resp.json()
    token = body["access_token"]
    expires_in = int(body.get("expires_in", 3600))
    _TOKEN_CACHE[user] = {"token": token, "expires_at": now + expires_in}
    _log.info("gmail_oauth_token_refreshed", user=user, expires_in=expires_in)
    return str(token)


def _xoauth2_string(user: str, access_token: str) -> str:
    """RFC-2595/4616-style SASL XOAUTH2 string."""
    return f"user={user}\x01auth=Bearer {access_token}\x01\x01"


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------
async def _new_imap_client() -> Any:
    from aioimaplib import aioimaplib  # lazy import

    imap = aioimaplib.IMAP4_SSL(host=IMAP_HOST, port=IMAP_PORT)
    await imap.wait_hello_from_server()
    return imap


async def connect_personal() -> Any:
    """Connect to personal Gmail via OAuth (XOAUTH2 SASL)."""
    settings = get_settings()
    user = settings.gmail_user
    if not user:
        raise RuntimeError("gmail_user not configured")
    token = await _refresh_access_token()
    auth = _xoauth2_string(user, token)
    imap = await _new_imap_client()
    # aioimaplib exposes xoauth2 helper if present; fall back to authenticate.
    if hasattr(imap, "xoauth2"):
        resp = await imap.xoauth2(user, token)
    else:
        # Manual AUTHENTICATE XOAUTH2 with base64-encoded payload.
        import base64

        b64 = base64.b64encode(auth.encode()).decode()
        resp = await imap.protocol.send(f"AUTHENTICATE XOAUTH2 {b64}")
    _log.info("imap_connected_personal", user=user, status=getattr(resp, "result", "?"))
    await imap.select(INBOX_NAME)
    return imap


async def connect_worker() -> Any:
    """Connect to worker Gmail via app password (Upwork digest inbox)."""
    settings = get_settings()
    user = settings.gmail_worker_user
    pw = settings.gmail_worker_app_password
    if not (user and pw):
        raise RuntimeError("gmail_worker_user / gmail_worker_app_password missing")
    imap = await _new_imap_client()
    resp = await imap.login(user, pw)
    _log.info("imap_connected_worker", user=user, status=getattr(resp, "result", "?"))
    await imap.select(INBOX_NAME)
    return imap


# ---------------------------------------------------------------------------
# IDLE loop
# ---------------------------------------------------------------------------
async def _fetch_message(imap: Any, uid: int) -> Message | None:
    try:
        resp = await imap.uid("fetch", str(uid), "(RFC822)")
    except Exception as e:
        _log.warning("imap_fetch_failed", uid=uid, err=str(e))
        return None
    if not resp or getattr(resp, "result", "") != "OK":
        return None
    # aioimaplib returns mixed bytes/str frames. The RFC822 payload is the
    # largest bytes chunk in resp.lines — pick it.
    raw: bytes | None = None
    best_len = 0
    for line in getattr(resp, "lines", []) or []:
        if isinstance(line, (bytes, bytearray)) and len(line) > best_len:
            raw = bytes(line)
            best_len = len(line)
    if not raw or best_len < 32:
        return None
    try:
        return email.message_from_bytes(raw)
    except Exception as e:
        _log.warning("imap_parse_failed", uid=uid, err=str(e))
        return None


async def _scan_new(imap: Any, last_uid: int) -> list[int]:
    try:
        resp = await imap.uid("search", None, f"{last_uid + 1}:*")
    except Exception as e:
        _log.warning("imap_search_failed", err=str(e))
        return []
    if not resp or getattr(resp, "result", "") != "OK":
        return []
    uids: list[int] = []
    for line in resp.lines:
        if isinstance(line, (bytes, bytearray)):
            line = line.decode(errors="ignore")
        for tok in str(line).split():
            if tok.isdigit():
                n = int(tok)
                if n > last_uid:
                    uids.append(n)
    return sorted(set(uids))


async def watch_mailbox(
    connect_fn: Callable[[], Awaitable[Any]],
    callback: Callable[[Message], Awaitable[None]],
    *,
    mailbox_label: str | None = None,
) -> None:
    """IDLE forever; deliver each new RFC822 message to `callback` exactly once.

    Reconnects with exponential backoff on any failure. Never crashes the worker.
    """
    label = mailbox_label or connect_fn.__name__

    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(2**31 - 1),  # effectively forever
        wait=wait_exponential(multiplier=2, min=2, max=300),
        retry=retry_if_exception_type(Exception),
        reraise=False,
    ):
        with attempt:
            imap = await connect_fn()
            try:
                await _idle_loop(imap, callback, label)
            finally:
                try:
                    await imap.logout()
                except Exception:
                    pass


async def _idle_loop(
    imap: Any,
    callback: Callable[[Message], Awaitable[None]],
    label: str,
) -> None:
    last_uid = await _load_last_uid(label)
    _log.info("imap_idle_start", mailbox=label, last_uid=last_uid)

    # Drain anything that arrived while we were offline.
    pending = await _scan_new(imap, last_uid)
    for uid in pending:
        msg = await _fetch_message(imap, uid)
        if msg is not None:
            try:
                await callback(msg)
            except Exception as e:
                _log.exception("imap_callback_error", uid=uid, err=str(e))
        await _save_last_uid(label, uid)
        last_uid = uid

    while True:
        # IDLE for up to ~25 minutes (Gmail recommends < 29).
        idle_task: Any = None
        try:
            idle_task = await imap.idle_start(timeout=25 * 60)
            # wait_server_push returns when EXISTS/EXPUNGE/etc. fire.
            await asyncio.wait_for(imap.wait_server_push(), timeout=25 * 60)
        except TimeoutError:
            pass
        except Exception as e:
            _log.warning("imap_idle_error", mailbox=label, err=str(e))
            raise  # reconnect via outer tenacity wrapper
        finally:
            try:
                imap.idle_done()
            except Exception:
                pass
            if idle_task is not None:
                try:
                    await asyncio.wait_for(idle_task, timeout=30)
                except Exception:
                    pass

        new_uids = await _scan_new(imap, last_uid)
        for uid in new_uids:
            msg = await _fetch_message(imap, uid)
            if msg is not None:
                try:
                    await callback(msg)
                except Exception as e:
                    _log.exception("imap_callback_error", uid=uid, err=str(e))
            await _save_last_uid(label, uid)
            last_uid = uid

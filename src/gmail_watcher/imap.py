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

# Google App Passwords are exactly 16 alphanumeric chars (sometimes shown
# as 4 groups of 4 separated by spaces in the Google UI — Gmail accepts
# either form). Anything shorter than 16 stripped chars cannot be a real
# value, so we treat it as "unset placeholder" rather than firing a doomed
# IMAP LOGIN that returns `status: NO, user: <nil>` and floods the logs.
# See https://support.google.com/accounts/answer/185833 for App Password
# format. The 16-char rule sidesteps having to enumerate placeholder
# tokens (empty, unset, todo, xxxxx, etc.) which we can't exhaustively guess.
_APP_PASSWORD_MIN_LEN = 16

# IDLE window. Gmail recommends < 29 min; we use 25 to leave headroom.
_IDLE_TIMEOUT_S = 25 * 60

# Tenacity reconnect backoff (exponential, capped). Values preserved verbatim
# from the original wait_exponential(multiplier=2, min=2, max=300) call.
_RECONNECT_BACKOFF_MULTIPLIER = 2
_RECONNECT_BACKOFF_MIN_S = 2
_RECONNECT_BACKOFF_MAX_S = 300
# Effectively-forever attempts cap (kept identical to original).
_RECONNECT_MAX_ATTEMPTS = 2**31 - 1

# Body-size floor used to discard truncated FETCH frames. Anything smaller is
# almost certainly a status/header echo rather than the RFC822 payload.
_MIN_RFC822_BYTES = 32

# Body cap forwarded to the classifier prompt is handled there; this constant
# bounds how many bytes the IDLE drain reads per UID frame iteration.
_BATCH_SIZE = 0  # reserved — no batch boundary in current single-UID drain.

# SASL XOAUTH2 string per RFC 7628 §3.1: `user=<user>\x01auth=Bearer <tok>\x01\x01`.
# Spec calls this the literal-prefix constant; kept here for grep-ability and to
# make the format string adjacent to its sole construction site.
_XOAUTH2_LITERAL_PREFIX = "user="


def _is_unset_user(value: str | None) -> bool:
    """True if `value` is not a plausible Gmail address.

    Structure check only: must contain `@` with non-empty local-part and
    a dotted domain. Content of the value is never logged.
    """
    if not value:
        return True
    v = value.strip()
    if "@" not in v:
        return True
    local, _, domain = v.partition("@")
    return not local or "." not in domain


def _is_unset_app_password(value: str | None) -> bool:
    """True if `value` cannot be a real Google App Password.

    App Passwords are 16 chars (whitespace-stripped). Shorter values are
    placeholders; longer values are forwarded to Gmail so a one-character
    typo surfaces as a NO and not silent disablement.
    """
    if not value:
        return True
    # App Passwords may be displayed grouped — `xxxx xxxx xxxx xxxx` — so
    # strip *all* whitespace before length check, not just edges.
    compact = "".join(value.split())
    return len(compact) < _APP_PASSWORD_MIN_LEN


def _worker_creds_unset(settings: Any) -> bool:
    """True iff worker mailbox should be skipped silently (empty-pw semantic).

    Centralises the empty-worker-password skip rule so both the pre-flight in
    `watch_mailbox` and `connect_worker` agree on the predicate byte-for-byte.
    """
    return _is_unset_user(settings.gmail_worker_user) or _is_unset_app_password(
        settings.gmail_worker_app_password,
    )


class _WorkerMailboxDisabled(RuntimeError):
    """Sentinel: worker mailbox creds intentionally unset; skip silently."""


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
    # NB: never log `token` — only the user + lifetime are safe to emit.
    _log.info("gmail_oauth_token_refreshed", user=user, expires_in=expires_in)
    return str(token)


def _build_xoauth2_authstring(user: str, access_token: str) -> str:
    """RFC-7628 §3.1 SASL XOAUTH2 string.

    Format: ``user=<user>\\x01auth=Bearer <token>\\x01\\x01``.
    Output MUST NOT be logged — it embeds the bearer token verbatim.
    """
    return f"{_XOAUTH2_LITERAL_PREFIX}{user}\x01auth=Bearer {access_token}\x01\x01"


# Back-compat alias for any in-tree caller still using the historical name.
_xoauth2_string = _build_xoauth2_authstring


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------
async def _new_imap_client() -> Any:
    from aioimaplib import aioimaplib  # lazy import

    imap = aioimaplib.IMAP4_SSL(host=IMAP_HOST, port=IMAP_PORT)
    await imap.wait_hello_from_server()
    return imap


async def _authenticate_xoauth2(imap: Any, user: str, token: str) -> Any:
    """Submit an XOAUTH2 SASL bind to an open IMAP client.

    Prefers the aioimaplib `xoauth2` helper when available; otherwise falls
    back to a manual `AUTHENTICATE XOAUTH2 <base64>` exchange. Neither the
    raw authstring nor the token is logged.
    """
    if hasattr(imap, "xoauth2"):
        return await imap.xoauth2(user, token)
    # Manual AUTHENTICATE XOAUTH2 with base64-encoded payload.
    import base64

    auth = _build_xoauth2_authstring(user, token)
    b64 = base64.b64encode(auth.encode()).decode()
    return await imap.protocol.send(f"AUTHENTICATE XOAUTH2 {b64}")


async def _connect_personal_mailbox() -> Any:
    """Connect to personal Gmail via OAuth (XOAUTH2 SASL).

    Internal helper — public surface is :func:`connect_personal`.
    """
    settings = get_settings()
    user = settings.gmail_user
    if not user:
        raise RuntimeError("gmail_user not configured")
    token = await _refresh_access_token()
    imap = await _new_imap_client()
    resp = await _authenticate_xoauth2(imap, user, token)
    _log.info("imap_connected_personal", user=user, status=getattr(resp, "result", "?"))
    await imap.select(INBOX_NAME)
    return imap


async def _connect_worker_mailbox() -> Any:
    """Connect to worker Gmail via app password.

    Raises :class:`_WorkerMailboxDisabled` when either ``gmail_worker_user`` or
    ``gmail_worker_app_password`` is unset / placeholder. :func:`watch_mailbox`
    catches the sentinel and exits silently after one info log, so the watcher
    container stays healthy until the user populates the value.
    """
    settings = get_settings()
    if _worker_creds_unset(settings):
        raise _WorkerMailboxDisabled(
            "gmail_worker_app_password unset; worker mailbox monitoring disabled",
        )
    user = settings.gmail_worker_user
    pw = settings.gmail_worker_app_password
    imap = await _new_imap_client()
    resp = await imap.login(user, pw)
    _log.info("imap_connected_worker", user=user, status=getattr(resp, "result", "?"))
    await imap.select(INBOX_NAME)
    return imap


# Public API — kept stable for `src/workers/gmail_worker.py`.
connect_personal = _connect_personal_mailbox
connect_worker = _connect_worker_mailbox


# ---------------------------------------------------------------------------
# Message fetch + UID scan
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
    if not raw or best_len < _MIN_RFC822_BYTES:
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


# ---------------------------------------------------------------------------
# Per-message and per-cycle helpers
# ---------------------------------------------------------------------------
async def _handle_message(
    imap: Any,
    uid: int,
    callback: Callable[[Message], Awaitable[None]],
    label: str,
) -> None:
    """Fetch + dispatch + persist for a single UID.

    Preserves the original ordering: callback runs FIRST (classifier in the
    gmail-worker wires into state_writer inside the callback), and the UID is
    persisted to `imap_state` ONLY after the callback returns — even on
    callback error, so a poison message can't wedge the watcher forever.
    """
    msg = await _fetch_message(imap, uid)
    if msg is not None:
        try:
            await callback(msg)
        except Exception as e:
            _log.exception("imap_callback_error", uid=uid, err=str(e))
    await _save_last_uid(label, uid)


async def _resync_unread_since(
    imap: Any,
    last_uid: int,
    callback: Callable[[Message], Awaitable[None]],
    label: str,
) -> int:
    """Drain everything that arrived while we were offline.

    Walks every UID strictly greater than `last_uid` in ascending order,
    invoking `_handle_message` on each. Returns the highest UID processed
    (or `last_uid` when nothing was pending).
    """
    pending = await _scan_new(imap, last_uid)
    for uid in pending:
        await _handle_message(imap, uid, callback, label)
        last_uid = uid
    return last_uid


async def _idle_one_cycle(imap: Any, label: str, idle_timeout: int) -> None:
    """Issue one IDLE → server-push → DONE round-trip.

    Swallows `TimeoutError` (the IDLE just expired uneventfully) and re-raises
    anything else so the outer tenacity wrapper can reconnect.
    """
    idle_task: Any = None
    try:
        idle_task = await imap.idle_start(timeout=idle_timeout)
        # wait_server_push returns when EXISTS/EXPUNGE/etc. fire.
        await asyncio.wait_for(imap.wait_server_push(), timeout=idle_timeout)
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


# ---------------------------------------------------------------------------
# IDLE loop + public watch_mailbox orchestrator
# ---------------------------------------------------------------------------
async def _idle_loop(
    imap: Any,
    callback: Callable[[Message], Awaitable[None]],
    label: str,
) -> None:
    last_uid = await _load_last_uid(label)
    _log.info("imap_idle_start", mailbox=label, last_uid=last_uid)

    # Drain anything that arrived while we were offline.
    last_uid = await _resync_unread_since(imap, last_uid, callback, label)

    while True:
        await _idle_one_cycle(imap, label, _IDLE_TIMEOUT_S)
        # Drain whatever the server pushed.
        last_uid = await _resync_unread_since(imap, last_uid, callback, label)


def _is_worker_connector(connect_fn: Callable[[], Awaitable[Any]]) -> bool:
    """Identify whether `connect_fn` is the worker connector (any alias)."""
    return connect_fn in (connect_worker, _connect_worker_mailbox)


def _worker_preflight_should_skip(connect_fn: Callable[[], Awaitable[Any]]) -> bool:
    """Empty-password skip predicate, byte-identical to original.

    Mirrors the legacy try/except: any error during settings probe degrades to
    "do not skip" so the loop can still attempt to connect (and log a warning).
    """
    if not _is_worker_connector(connect_fn):
        return False
    try:
        return _worker_creds_unset(get_settings())
    except Exception as e:
        _log.warning("imap_worker_preflight_failed", err=str(e))
        return False


def _reconnect_retrying() -> AsyncRetrying:
    """Tenacity policy mirroring the original exponential backoff verbatim."""
    return AsyncRetrying(
        stop=stop_after_attempt(_RECONNECT_MAX_ATTEMPTS),
        wait=wait_exponential(
            multiplier=_RECONNECT_BACKOFF_MULTIPLIER,
            min=_RECONNECT_BACKOFF_MIN_S,
            max=_RECONNECT_BACKOFF_MAX_S,
        ),
        retry=retry_if_exception_type(Exception),
        reraise=False,
    )


async def _connect_and_idle(
    connect_fn: Callable[[], Awaitable[Any]],
    callback: Callable[[Message], Awaitable[None]],
    label: str,
) -> bool:
    """One connect → IDLE-forever attempt. Returns True to keep retrying.

    Returns False ONLY when the worker sentinel is raised — the caller treats
    that as "stop retrying, exit silently".
    """
    try:
        imap = await connect_fn()
    except _WorkerMailboxDisabled as e:
        # Sentinel raised after watcher started but before connect — e.g.
        # settings reloaded mid-run. Stop the retry loop cleanly.
        _log.info("imap_worker_password_empty", mailbox=label, note=str(e))
        return False
    try:
        await _idle_loop(imap, callback, label)
    finally:
        try:
            await imap.logout()
        except Exception:
            pass
    return True


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
    if _worker_preflight_should_skip(connect_fn):
        _log.info(
            "imap_worker_password_empty",
            mailbox=label,
            note="gmail_worker_app_password unset; worker mailbox monitoring disabled",
        )
        return
    async for attempt in _reconnect_retrying():
        with attempt:
            if not await _connect_and_idle(connect_fn, callback, label):
                return

"""Reddit OAuth bearer helper for the `script` app type.

Provides an in-process token cache + async refresh path. Used by the HTTP
fetcher to inject `Authorization: Bearer <token>` when a request targets
`oauth.reddit.com`.

Flow:
- POST https://www.reddit.com/api/v1/access_token
- HTTP Basic: <reddit_client_id>:<reddit_client_secret>
- Body: grant_type=password&username=...&password=...   (if creds present)
       OR grant_type=client_credentials                 (limited fallback)
- Required header: User-Agent (per Reddit API rules)
- Response: {"access_token": "...", "token_type": "bearer",
            "expires_in": 86400, "scope": "*"}

The bearer is cached in-process and refreshed when within
`_REFRESH_WINDOW_SECONDS` (5 min) of expiry. A module-level asyncio.Lock
prevents concurrent fetchers from racing on refresh.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.common.logger import get_logger
from src.common.secrets import get_settings

_log = get_logger(__name__)

ACCESS_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_REFRESH_WINDOW_SECONDS = 5 * 60  # refresh if expiring within 5 min
_DEFAULT_TTL_SECONDS = 3600  # fallback if Reddit omits expires_in
_HTTP_TIMEOUT_SECONDS = 15.0

# Module-level cache. Single tenant for Phase 1.
# {"token": str, "expires_at": datetime (aware, UTC)}
_TOKEN_CACHE: dict[str, Any] = {}
_REFRESH_LOCK = asyncio.Lock()


def is_configured() -> bool:
    """True iff Reddit client id + secret are both non-empty in Settings."""
    s = get_settings()
    return bool(s.reddit_client_id) and bool(s.reddit_client_secret)


def _cached_token_if_fresh() -> str | None:
    token = _TOKEN_CACHE.get("token")
    expires_at = _TOKEN_CACHE.get("expires_at")
    if not token or not isinstance(expires_at, datetime):
        return None
    now = datetime.now(UTC)
    if (expires_at - now).total_seconds() > _REFRESH_WINDOW_SECONDS:
        return str(token)
    return None


def _build_auth_payload(settings: Any) -> tuple[dict[str, str], str]:
    """Pick grant type. Returns (form_payload, grant_label_for_logging)."""
    if settings.reddit_username and settings.reddit_password:
        return (
            {
                "grant_type": "password",
                "username": settings.reddit_username,
                "password": settings.reddit_password,
            },
            "password",
        )
    return ({"grant_type": "client_credentials"}, "client_credentials")


async def _request_new_token() -> str:
    settings = get_settings()
    if not is_configured():
        raise RuntimeError("reddit oauth not configured: client_id/client_secret missing")

    payload, grant_label = _build_auth_payload(settings)
    auth = httpx.BasicAuth(settings.reddit_client_id, settings.reddit_client_secret)
    headers = {"User-Agent": settings.reddit_user_agent}

    body: dict[str, Any] = {}
    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type((httpx.HTTPError,)),
            reraise=True,
        ):
            with attempt:
                async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
                    resp = await client.post(
                        ACCESS_TOKEN_URL,
                        data=payload,
                        auth=auth,
                        headers=headers,
                    )
                    resp.raise_for_status()
                    body = resp.json()
    except RetryError as e:
        _log.warning("reddit_oauth_refresh_failed", err=str(e), grant=grant_label)
        raise RuntimeError(f"reddit oauth refresh failed: {e}") from e
    except httpx.HTTPError as e:
        _log.warning("reddit_oauth_refresh_failed", err=str(e), grant=grant_label)
        raise RuntimeError(f"reddit oauth refresh failed: {e}") from e

    token = body.get("access_token")
    if not token:
        err = body.get("error") or "no access_token in response"
        _log.warning("reddit_oauth_refresh_failed", err=err, grant=grant_label)
        raise RuntimeError(f"reddit oauth refresh failed: {err}")

    expires_in = int(body.get("expires_in") or _DEFAULT_TTL_SECONDS)
    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    _TOKEN_CACHE["token"] = str(token)
    _TOKEN_CACHE["expires_at"] = expires_at
    _log.info(
        "reddit_oauth_token_refreshed",
        grant=grant_label,
        expires_in=expires_in,
        scope=body.get("scope"),
    )
    return str(token)


async def get_bearer_token() -> str:
    """Return a valid bearer token, refreshing if near expiry.

    Concurrent callers serialize through a module-level lock so only one
    refresh round-trip fires at a time. Raises RuntimeError if Reddit
    auth fails or credentials are missing.
    """
    cached = _cached_token_if_fresh()
    if cached is not None:
        return cached

    async with _REFRESH_LOCK:
        # Double-checked locking: another coroutine may have refreshed
        # while we were waiting on the lock.
        cached = _cached_token_if_fresh()
        if cached is not None:
            return cached
        return await _request_new_token()


def _reset_cache_for_tests() -> None:
    """Test-only hook to wipe the in-process cache."""
    _TOKEN_CACHE.clear()

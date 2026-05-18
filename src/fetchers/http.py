"""Tier-0 fetcher: curl_cffi chrome131 + cached cf_clearance cookies.

Reuses cf_clearance_cache rows before falling through to higher tiers via dispatcher.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from curl_cffi.requests import AsyncSession

from src.common.db import acquire
from src.common.logger import get_logger
from src.common.metrics import (
    cf_challenge_appeared_rate,
    fetch_errors_total,
    fetch_latency_seconds,
)
from src.common.secrets import get_settings
from src.fetchers.base import Fetcher, FetchRequest, FetchResponse
from src.sources import reddit_auth

_log = get_logger(__name__)

CF_MARKERS = ("Attention Required", "Just a moment", "Checking your browser")
REDDIT_OAUTH_HOST = "oauth.reddit.com"
REDDIT_HOSTS = ("oauth.reddit.com", "www.reddit.com", "reddit.com")


class HttpFetcher(Fetcher):
    tier = 0
    name = "curl_cffi_chrome131"

    def __init__(self, impersonate: str = "chrome131") -> None:
        self._impersonate = impersonate

    async def _load_clearance(self, source_id: int, host: str) -> tuple[str | None, str | None]:
        async with acquire() as conn:
            rec = await conn.fetchrow(
                """
                SELECT cookie_value, ua_string
                FROM cf_clearance_cache
                WHERE source_id = $1 AND domain = $2 AND expires_at > NOW()
                ORDER BY last_used_at DESC NULLS LAST
                LIMIT 1
                """,
                source_id, host,
            )
        if rec is None:
            return None, None
        return rec["cookie_value"], rec["ua_string"]

    async def _bump_clearance(self, source_id: int, host: str, ok: bool) -> None:
        col = "success_count" if ok else "failure_count"
        async with acquire() as conn:
            await conn.execute(
                f"""
                UPDATE cf_clearance_cache
                SET {col} = {col} + 1,
                    last_used_at = NOW()
                WHERE source_id = $1 AND domain = $2 AND expires_at > NOW()
                """,
                source_id, host,
            )

    async def fetch(self, req: FetchRequest) -> FetchResponse:
        host = _host(req.url)
        cookie_val, ua = await self._load_clearance(req.source_id, host)
        headers: dict[str, str] = dict(req.headers or {})
        if cookie_val:
            existing_cookie = headers.get("Cookie", "")
            headers["Cookie"] = (
                f"{existing_cookie}; {cookie_val}" if existing_cookie else cookie_val
            ).strip("; ")
        if ua:
            headers.setdefault("User-Agent", ua)

        # Reddit: set descriptive UA on every reddit.com request (Reddit 429s
        # anonymous traffic without a recognizable UA). Inject OAuth bearer
        # only when targeting oauth.reddit.com AND creds are configured.
        if host in REDDIT_HOSTS:
            reddit_ua = get_settings().reddit_user_agent
            if reddit_ua:
                headers["User-Agent"] = reddit_ua
            if host == REDDIT_OAUTH_HOST and reddit_auth.is_configured():
                try:
                    token = await reddit_auth.get_bearer_token()
                    headers["Authorization"] = f"Bearer {token}"
                except RuntimeError as e:
                    _log.warning("reddit_bearer_unavailable", url=req.url, err=str(e))

        t0 = time.perf_counter()
        cf_seen = False
        try:
            async with AsyncSession(impersonate=self._impersonate) as session:  # type: ignore[arg-type]
                resp = await session.request(
                    req.method, req.url,
                    headers=headers or None,
                    data=req.body,
                    timeout=req.timeout_s,
                    allow_redirects=True,
                )
                body = resp.text if hasattr(resp, "text") else (resp.content.decode("utf-8", errors="replace"))
                content_type = resp.headers.get("content-type")

            cf_seen = (resp.status_code in (403, 503)) and any(m in body for m in CF_MARKERS)
            if cf_seen:
                cf_challenge_appeared_rate.set(1.0)

            ok = 200 <= resp.status_code < 400 and not cf_seen
            await self._bump_clearance(req.source_id, host, ok=ok)

            return FetchResponse(
                status=resp.status_code,
                body=body,
                content_type=content_type,
                tier=self.tier,
                headers=dict(resp.headers),
                error=None if ok else f"status_{resp.status_code}_cf_{cf_seen}",
                cf_challenge_observed=cf_seen,
            )
        except Exception as e:
            # `class` is a Python keyword — pass the label positionally so
            # we are not forced into `**{"class": ...}` ceremony. The metric
            # is declared with labelnames=("class",) in common/metrics.py.
            fetch_errors_total.labels("http_exc").inc()
            _log.warning("http_fetch_failed", url=req.url, err=str(e))
            return FetchResponse(
                status=0, body="", content_type=None, tier=self.tier,
                headers={}, error=str(e), cf_challenge_observed=False,
            )
        finally:
            fetch_latency_seconds.labels(source=req.source_slug, tier=str(self.tier)).observe(
                time.perf_counter() - t0
            )


def _host(url: str) -> str:
    from urllib.parse import urlparse
    return (urlparse(url).hostname or "").lower()


async def save_clearance(
    *, source_id: int, identity_id: int | None,
    host: str, cookie_value: str, ua_string: str,
    ja4: str | None = None, ttl_minutes: int = 30,
) -> None:
    expires = datetime.now(UTC) + timedelta(minutes=ttl_minutes)
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO cf_clearance_cache(
                source_id, identity_id, domain, cookie_value, ua_string, ja4_profile,
                acquired_at, expires_at
            )
            VALUES ($1,$2,$3,$4,$5,$6,NOW(),$7)
            ON CONFLICT (source_id, COALESCE(identity_id,0), domain)
            DO UPDATE SET cookie_value = EXCLUDED.cookie_value,
                          ua_string    = EXCLUDED.ua_string,
                          ja4_profile  = EXCLUDED.ja4_profile,
                          acquired_at  = NOW(),
                          expires_at   = EXCLUDED.expires_at
            """,
            source_id, identity_id, host, cookie_value, ua_string, ja4, expires,
        )

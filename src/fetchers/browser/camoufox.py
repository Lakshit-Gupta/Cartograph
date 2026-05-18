"""Camoufox 0.4+ driver — Firefox-based headless / Xvfb-driven. ARM64."""

from __future__ import annotations

import time
from urllib.parse import urlparse

from camoufox.async_api import AsyncCamoufox

from src.common.logger import get_logger
from src.common.metrics import (
    cf_challenge_appeared_rate,
    fetch_errors_total,
    fetch_latency_seconds,
)
from src.fetchers.base import Fetcher, FetchRequest, FetchResponse
from src.fetchers.browser.behavioral import humanize_page
from src.fetchers.browser.pool import BrowserPool

_log = get_logger(__name__)

CF_MARKERS = ("Attention Required", "Just a moment", "Checking your browser")


def _cookies_to_playwright(cookies: dict[str, str], url: str) -> list[dict[str, str | bool]]:
    """Translate `IdentityLease.cookies` (flat name→value) into Playwright's
    SetCookieParam list. Cookies must carry either `url` or `domain`; we set
    `url` (Playwright then derives the right scope automatically) which means
    no leading-dot domain juggling here. Wildcard sharing (e.g. across
    subdomains) lives elsewhere — the vault stores per-host cookies already.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    base = f"{parsed.scheme}://{host}" if host else url
    return [
        {
            "name": name,
            "value": value,
            "url": base,
        }
        for name, value in cookies.items()
    ]


class CamoufoxFetcher(Fetcher):
    tier = 2
    name = "camoufox_xvfb"

    def __init__(self) -> None:
        self._pool = BrowserPool(max_size=3)

    async def fetch(self, req: FetchRequest) -> FetchResponse:
        t0 = time.perf_counter()
        try:
            async with self._pool.lease() as lease:
                browser: AsyncCamoufox = lease.browser

                # Build a fresh BrowserContext when identity context is
                # present so cookies + UA stay isolated per-fetch. The pool
                # itself shares the underlying browser process across leases;
                # the context (and its cookie jar) is per-fetch — recycled by
                # closing it at the end of this scope. Without a fresh
                # context, two identities leased back-to-back through the
                # same browser would cross-contaminate cookie jars.
                context_kwargs: dict[str, str] = {}
                if req.ua_string:
                    context_kwargs["user_agent"] = req.ua_string

                if context_kwargs:
                    context = await browser.new_context(**context_kwargs)
                else:
                    context = await browser.new_context()

                try:
                    if req.cookies:
                        await context.add_cookies(_cookies_to_playwright(req.cookies, req.url))

                    page = await context.new_page()
                    try:
                        await page.goto(req.url, timeout=int(req.timeout_s * 1000))
                        await humanize_page(page)
                        body = await page.content()
                        status = 200  # Camoufox doesn't surface HTTP status directly; check markers
                        cf_seen = any(m in body for m in CF_MARKERS)
                        if cf_seen:
                            cf_challenge_appeared_rate.set(1.0)
                        return FetchResponse(
                            status=0 if cf_seen else status,
                            body=body,
                            content_type="text/html",
                            tier=self.tier,
                            headers={},
                            error="cf_challenge" if cf_seen else None,
                            cf_challenge_observed=cf_seen,
                        )
                    finally:
                        await page.close()
                finally:
                    await context.close()
        except Exception as e:
            # Positional — `class` is a Python keyword; see http.py:123 for context.
            fetch_errors_total.labels("browser_exc").inc()
            _log.warning("camoufox_fetch_failed", url=req.url, err=str(e))
            return FetchResponse(
                status=0,
                body="",
                content_type=None,
                tier=self.tier,
                headers={},
                error=str(e),
                cf_challenge_observed=False,
            )
        finally:
            fetch_latency_seconds.labels(source=req.source_slug, tier=str(self.tier)).observe(time.perf_counter() - t0)

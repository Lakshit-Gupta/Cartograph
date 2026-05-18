"""FlareSolverr cookie broker client.

Tier-1 — used ONLY to obtain a fresh cf_clearance cookie + UA, NOT to fetch pages.
After clearance lands, fetcher falls back to tier-0 curl_cffi with the cookie set.
"""

from __future__ import annotations

import time
from urllib.parse import urlparse

import httpx

from src.common.logger import get_logger
from src.common.metrics import (
    cf_challenge_appeared_rate,
    cf_clearance_solve_rate,
    cf_js_challenge_solve_time_ms,
)
from src.common.secrets import get_settings
from src.fetchers.base import Fetcher, FetchRequest, FetchResponse
from src.fetchers.http import save_clearance

_log = get_logger(__name__)


class FlareSolverrFetcher(Fetcher):
    tier = 1
    name = "flaresolverr_cookie"

    def __init__(self) -> None:
        self._url = get_settings().flaresolverr_url

    async def fetch(self, req: FetchRequest) -> FetchResponse:
        t0 = time.perf_counter()
        payload = {
            "cmd": "request.get",
            "url": req.url,
            "maxTimeout": int(req.timeout_s * 1000),
        }
        try:
            async with httpx.AsyncClient(timeout=req.timeout_s + 5) as client:
                resp = await client.post(f"{self._url}/v1", json=payload)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            _log.warning("flaresolverr_failed", url=req.url, err=str(e))
            return FetchResponse(
                status=0,
                body="",
                content_type=None,
                tier=self.tier,
                headers={},
                error=str(e),
                cf_challenge_observed=True,
            )

        status_str = data.get("status")  # "ok" | "error"
        solution = data.get("solution") or {}
        body = solution.get("response", "")
        headers = solution.get("headers") or {}
        ua = solution.get("userAgent")
        cookies = {c["name"]: c["value"] for c in solution.get("cookies", [])}

        cf_clearance = cookies.get("cf_clearance")
        if cf_clearance and ua:
            host = (urlparse(req.url).hostname or "").lower()
            cookie_value = "; ".join(f"{k}={v}" for k, v in cookies.items())
            await save_clearance(
                source_id=req.source_id,
                identity_id=req.identity_id,
                host=host,
                cookie_value=cookie_value,
                ua_string=ua,
                ttl_minutes=30,
            )
            cf_clearance_solve_rate.set(1.0)
        else:
            cf_clearance_solve_rate.set(0.0)
            cf_challenge_appeared_rate.set(1.0)

        elapsed_ms = (time.perf_counter() - t0) * 1000
        cf_js_challenge_solve_time_ms.observe(elapsed_ms)

        return FetchResponse(
            status=int(solution.get("status", 0)) or (200 if status_str == "ok" else 0),
            body=body,
            content_type=headers.get("content-type") if isinstance(headers, dict) else None,
            tier=self.tier,
            headers=headers if isinstance(headers, dict) else {},
            error=None if status_str == "ok" else data.get("message"),
            cf_challenge_observed=True,
        )

"""Tier-routing dispatcher.

Routes a FetchRequest through `source.tier_chain` in order, escalating to the
next tier on failure / CF challenge. Tracks tier success in cf_clearance_cache
and metrics.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from src.common.logger import get_logger
from src.common.metrics import extract_tier_distribution
from src.fetchers.base import Fetcher, FetchRequest, FetchResponse
from src.fetchers.flaresolverr import FlareSolverrFetcher
from src.fetchers.http import HttpFetcher

_log = get_logger(__name__)


# Lazy registry — browser tier imported only when needed (heavy)
_FETCHERS: dict[int, Fetcher] = {}


def _get(tier: int) -> Fetcher:
    if tier in _FETCHERS:
        return _FETCHERS[tier]
    f: Fetcher
    if tier == 0:
        f = HttpFetcher()
    elif tier == 1:
        f = FlareSolverrFetcher()
    elif tier == 2:
        from src.fetchers.browser.camoufox import CamoufoxFetcher
        f = CamoufoxFetcher()
    else:
        raise NotImplementedError(f"tier {tier} not implemented")
    _FETCHERS[tier] = f
    return f


class TierDispatcher:
    """Run a fetch through a source's tier_chain. First success wins."""

    async def fetch(
        self,
        req: FetchRequest,
        tier_chain: list[int],
        *,
        on_promotion: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> FetchResponse:
        last: FetchResponse | None = None
        for tier in tier_chain:
            try:
                fetcher = _get(tier)
            except NotImplementedError as e:
                _log.warning("tier_unavailable", tier=tier, err=str(e))
                continue
            resp = await fetcher.fetch(req)
            last = resp

            if 200 <= resp.status < 400 and not resp.cf_challenge_observed:
                extract_tier_distribution.labels(source=req.source_slug, tier=str(tier)).inc()
                return resp

            _log.info(
                "tier_escalation",
                source=req.source_slug,
                from_tier=tier,
                status=resp.status,
                cf=resp.cf_challenge_observed,
            )
            if on_promotion is not None:
                await on_promotion(tier, resp.status)

        # All tiers failed
        return last or FetchResponse(
            status=0, body="", content_type=None, tier=-1, headers={},
            error="all_tiers_failed", cf_challenge_observed=True,
        )

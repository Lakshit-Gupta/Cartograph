"""r/forhire (and any reddit subreddit) via anonymous JSON endpoint.

Reddit's `www.reddit.com/r/<sub>/new.json` returns the same JSON shape as the
OAuth endpoint but requires no app credentials — useful when the developer
portal is gated (2026+) or when OAuth approval is pending.

Trade-off vs OAuth: anonymous traffic is rate-limited to ~10 req/min by Reddit,
which is sufficient for our crawl cadence (every 15-60 min per sub). The
HTTP fetcher must send a descriptive `User-Agent`; without it Reddit returns
429 aggressively. UA injection lives in `src/fetchers/http.py`.

Strategy names (`reddit_oauth`, `reddit_oauth_push`) are kept for migration
compatibility — V003 seed rows reference them. The names are historical; the
fetch path is anonymous.
"""
from __future__ import annotations

from src.sources.base import CrawlPlan
from src.sources.registry import register


class _RedditJSON:
    slug = "reddit_oauth"
    strategy = "reddit_oauth"

    async def plan(self, *, source_id: int, base_url: str, config: dict) -> CrawlPlan:
        # base_url now: https://www.reddit.com/r/forhire/new.json
        return CrawlPlan(
            source_id=source_id,
            source_slug=config.get("slug", self.slug),
            urls=[f"{base_url}?limit=50"],
            tier_chain=[0],
            requires_identity=False,    # anonymous endpoint, no identity needed
        )


class _RedditJSONPush:
    slug = "reddit_oauth_push"
    strategy = "reddit_oauth_push"

    async def plan(self, *, source_id: int, base_url: str, config: dict) -> CrawlPlan:
        return CrawlPlan(
            source_id=source_id,
            source_slug=config.get("slug", self.slug),
            urls=[f"{base_url}?limit=25"],
            tier_chain=[0],
            requires_identity=False,
        )


register(_RedditJSON())
register(_RedditJSONPush())

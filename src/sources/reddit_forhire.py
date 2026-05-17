"""r/forhire (and any reddit subreddit) via OAuth.

For Phase 1 we use the basic 'script' app — bearer is fetched + cached by
`src/sources/reddit_auth.py` and injected by the HTTP fetcher when the host
is `oauth.reddit.com`.
"""
from __future__ import annotations

from src.sources.base import CrawlPlan
from src.sources.registry import register


class _RedditOAuth:
    slug = "reddit_oauth"
    strategy = "reddit_oauth"

    async def plan(self, *, source_id: int, base_url: str, config: dict) -> CrawlPlan:
        # base_url e.g. https://oauth.reddit.com/r/forhire/new
        return CrawlPlan(
            source_id=source_id,
            source_slug=config.get("slug", self.slug),
            urls=[f"{base_url}?limit=50"],
            tier_chain=[0],
            requires_identity=True,    # needs a Reddit identity for OAuth bearer
        )


class _RedditOAuthPush:
    slug = "reddit_oauth_push"
    strategy = "reddit_oauth_push"

    async def plan(self, *, source_id: int, base_url: str, config: dict) -> CrawlPlan:
        return CrawlPlan(
            source_id=source_id,
            source_slug=config.get("slug", self.slug),
            urls=[f"{base_url}?limit=25"],   # smaller burst, fast lane
            tier_chain=[0],
            requires_identity=True,
        )


register(_RedditOAuth())
register(_RedditOAuthPush())

"""Replit Bounties listing source plugin.

Phase 3.3 — public HTML listing page; tier-0 fetch + tier-1 HTML
extractor. Replit serves the page server-rendered so curl_cffi with
Chrome impersonation is enough.
"""

from __future__ import annotations

from src.sources.base import CrawlPlan, SourcePlugin
from src.sources.registry import register


class _ReplitBounties:
    slug = "bounty_replit"
    strategy = "bounty_replit"

    async def plan(self, *, source_id: int, base_url: str, config: dict) -> CrawlPlan:
        return CrawlPlan(
            source_id=source_id,
            source_slug=self.slug,
            urls=[base_url],
            tier_chain=[0, 1],
            requires_identity=False,
        )


PLUGIN: SourcePlugin = _ReplitBounties()
register(PLUGIN)

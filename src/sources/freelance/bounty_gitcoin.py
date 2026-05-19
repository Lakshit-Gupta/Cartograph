"""Gitcoin bounty source plugin.

Phase 3.3 — Gitcoin's public REST API serves open bounties as JSON.
Tier-0 fetch via curl_cffi (no CF protection, no JS rendering).
"""

from __future__ import annotations

from src.sources.base import CrawlPlan, SourcePlugin
from src.sources.registry import register


class _Gitcoin:
    slug = "bounty_gitcoin"
    strategy = "bounty_gitcoin"

    async def plan(self, *, source_id: int, base_url: str, config: dict) -> CrawlPlan:
        return CrawlPlan(
            source_id=source_id,
            source_slug=self.slug,
            urls=[base_url],
            tier_chain=[0],
            requires_identity=False,
        )


PLUGIN: SourcePlugin = _Gitcoin()
register(PLUGIN)

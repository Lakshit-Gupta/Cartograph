"""Algora bounty feed source plugin.

Phase 3.3 — registers the bounty_algora strategy so the scheduler routes
Algora's public tRPC `bounty.list` endpoint to tier 0 (no CF, no JS).
The extractor lives at `src/extractors/tier1_selectors/bounty_algora.py`.

The original brief documented `/api/v1/bounties/feed.json` — that path
404s as of 2026-05-19 and the seed migration (V015) now points at the
tRPC route. The plugin itself stays endpoint-agnostic: it passes the
`sources.base_url` value through verbatim, so flipping the seed row to
a future REST endpoint requires zero code change here.
"""

from __future__ import annotations

from src.sources.base import CrawlPlan, SourcePlugin
from src.sources.registry import register


class _Algora:
    slug = "bounty_algora"
    strategy = "bounty_algora"

    async def plan(self, *, source_id: int, base_url: str, config: dict) -> CrawlPlan:
        return CrawlPlan(
            source_id=source_id,
            source_slug=self.slug,
            urls=[base_url],
            tier_chain=[0],
            requires_identity=False,
        )


PLUGIN: SourcePlugin = _Algora()
register(PLUGIN)

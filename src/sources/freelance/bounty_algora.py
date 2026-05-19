"""Algora bounty feed source plugin.

Phase 3.3 — registers the source so the dispatcher routes Algora's
public JSON feed to tier 0 (no CF, no JS). The extractor lives at
``src/extractors/tier1_selectors/bounty_algora.py``.
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

"""Inc42 funding news scrape — funded startups likely hiring."""
from __future__ import annotations

from src.sources.base import CrawlPlan, SourcePlugin
from src.sources.registry import register


class _Inc42:
    slug = "india_inc42"
    strategy = "india_inc42"

    async def plan(self, *, source_id: int, base_url: str, config: dict) -> CrawlPlan:
        return CrawlPlan(
            source_id=source_id, source_slug=self.slug,
            urls=[base_url],
            tier_chain=[0, 1, 2], requires_identity=False,
        )


PLUGIN: SourcePlugin = _Inc42()
register(PLUGIN)

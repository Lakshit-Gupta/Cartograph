"""YC India — list page → ATS slug harvest. Phase 1 fetches the listing only."""
from __future__ import annotations

from src.sources.base import CrawlPlan, SourcePlugin
from src.sources.registry import register


class _YCIndia:
    slug = "india_yc"
    strategy = "india_yc"

    async def plan(self, *, source_id: int, base_url: str, config: dict) -> CrawlPlan:
        return CrawlPlan(
            source_id=source_id, source_slug=self.slug,
            urls=[base_url],
            tier_chain=[0], requires_identity=False,
        )


PLUGIN: SourcePlugin = _YCIndia()
register(PLUGIN)

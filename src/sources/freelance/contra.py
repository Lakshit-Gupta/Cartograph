"""Contra hot opps — every 2 min via tier chain 0→1→2 if CF blocks."""
from __future__ import annotations

from src.sources.base import CrawlPlan, SourcePlugin
from src.sources.registry import register


class _Contra:
    slug = "freelance_contra"
    strategy = "freelance_contra"

    async def plan(self, *, source_id: int, base_url: str, config: dict) -> CrawlPlan:
        urls = [f"{base_url}?sortBy=createdAt&page=1&pageSize=30"]
        return CrawlPlan(
            source_id=source_id, source_slug=self.slug, urls=urls,
            tier_chain=[0, 1, 2], requires_identity=True,
        )


PLUGIN: SourcePlugin = _Contra()
register(PLUGIN)

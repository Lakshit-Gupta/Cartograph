"""Cuvette mobile API (iOS UA, no CF)."""

from __future__ import annotations

from src.sources.base import CrawlPlan, SourcePlugin
from src.sources.registry import register


class _Cuvette:
    slug = "india_cuvette"
    strategy = "india_cuvette"

    async def plan(self, *, source_id: int, base_url: str, config: dict) -> CrawlPlan:
        urls = [
            f"{base_url}/api/jobs?page=1&limit=50",
            f"{base_url}/api/internships?page=1&limit=50",
        ]
        return CrawlPlan(
            source_id=source_id,
            source_slug=self.slug,
            urls=urls,
            tier_chain=[0],
            requires_identity=False,
        )


PLUGIN: SourcePlugin = _Cuvette()
register(PLUGIN)

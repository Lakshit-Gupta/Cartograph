"""Unstop public JSON API."""

from __future__ import annotations

from src.sources.base import CrawlPlan, SourcePlugin
from src.sources.registry import register


class _Unstop:
    slug = "india_unstop"
    strategy = "india_unstop"

    async def plan(self, *, source_id: int, base_url: str, config: dict) -> CrawlPlan:
        urls = [
            f"{base_url}/opportunity-types/internships?per_page=50&page=1",
            f"{base_url}/opportunity-types/jobs?per_page=50&page=1",
        ]
        return CrawlPlan(
            source_id=source_id,
            source_slug=self.slug,
            urls=urls,
            tier_chain=[0],
            requires_identity=False,
        )


PLUGIN: SourcePlugin = _Unstop()
register(PLUGIN)

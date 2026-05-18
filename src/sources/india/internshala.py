"""Internshala HTML scrape (uses identity for logged-in pagination)."""

from __future__ import annotations

from src.sources.base import CrawlPlan, SourcePlugin
from src.sources.registry import register


class _Internshala:
    slug = "india_internshala"
    strategy = "india_internshala"

    async def plan(self, *, source_id: int, base_url: str, config: dict) -> CrawlPlan:
        pages = list(range(1, 4))  # first 3 pages per cycle
        urls = [f"{base_url}/page-{p}/" for p in pages]
        return CrawlPlan(
            source_id=source_id,
            source_slug=self.slug,
            urls=urls,
            tier_chain=[0, 1],
            requires_identity=True,
        )


PLUGIN: SourcePlugin = _Internshala()
register(PLUGIN)

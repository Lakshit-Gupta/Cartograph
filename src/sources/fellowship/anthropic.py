"""Fellowship HTML plugin. ONE generic strategy 'fellowship_html' for all fellowship sites."""
from __future__ import annotations

from src.sources.base import CrawlPlan, SourcePlugin
from src.sources.registry import register


class _FellowshipHtml:
    slug = "fellowship_html"
    strategy = "fellowship_html"

    async def plan(self, *, source_id: int, base_url: str, config: dict) -> CrawlPlan:
        return CrawlPlan(
            source_id=source_id,
            source_slug=config.get("slug", self.slug),
            urls=[base_url],
            tier_chain=config.get("tier_chain") or [0, 1, 2],
            requires_identity=False,
        )


PLUGIN: SourcePlugin = _FellowshipHtml()
register(PLUGIN)

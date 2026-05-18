"""Generic RSS plugin. Strategy 'rss_generic' handles ALL RSS-backed sources.

The fetcher just grabs base_url. A downstream extractor (rss_generic in
tier1_selectors — not implemented yet; tier-2 LLM is the fallback) parses items.
For Phase 1 we lean on tier-0 regex + tier-2 LLM.
"""

from __future__ import annotations

from src.sources.base import CrawlPlan, SourcePlugin
from src.sources.registry import register


class _RssGeneric:
    slug = "rss_generic"
    strategy = "rss_generic"

    async def plan(self, *, source_id: int, base_url: str, config: dict) -> CrawlPlan:
        return CrawlPlan(
            source_id=source_id,
            source_slug=config.get("slug", self.slug),
            urls=[base_url],
            tier_chain=[0],
            requires_identity=False,
        )


PLUGIN: SourcePlugin = _RssGeneric()
register(PLUGIN)

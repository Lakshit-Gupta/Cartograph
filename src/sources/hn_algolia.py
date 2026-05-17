"""HN 'Who is hiring' via Algolia search API.

Searches the past 30 days for the latest 'Ask HN: Who is hiring?' thread,
then pulls the comments page in JSON. Multiple comments per thread → many opps.
"""
from __future__ import annotations

from src.sources.base import CrawlPlan, SourcePlugin
from src.sources.registry import register


class _HNAlgolia:
    slug = "hn_algolia"
    strategy = "hn_algolia"

    async def plan(self, *, source_id: int, base_url: str, config: dict) -> CrawlPlan:
        # 1: latest 'Who is hiring?' story comments page
        story_search = (
            f"{base_url}?tags=story&query=Ask%20HN%20Who%20is%20hiring&hitsPerPage=1"
        )
        comments_search = (
            f"{base_url}?tags=comment,story_3148259&hitsPerPage=400"  # placeholder story id
        )
        return CrawlPlan(
            source_id=source_id,
            source_slug=self.slug,
            urls=[story_search, comments_search],
            tier_chain=[0],
            requires_identity=False,
        )


PLUGIN: SourcePlugin = _HNAlgolia()
register(PLUGIN)

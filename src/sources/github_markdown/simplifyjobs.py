"""GitHub markdown plugin. Strategy 'github_md' handles every awesome-list source."""
from __future__ import annotations

from src.sources.base import CrawlPlan, SourcePlugin
from src.sources.registry import register


class _GitHubMd:
    slug = "github_md"
    strategy = "github_md"

    async def plan(self, *, source_id: int, base_url: str, config: dict) -> CrawlPlan:
        return CrawlPlan(
            source_id=source_id,
            source_slug=config.get("slug", self.slug),
            urls=[base_url],
            tier_chain=[0],
            requires_identity=False,
        )


PLUGIN: SourcePlugin = _GitHubMd()
register(PLUGIN)

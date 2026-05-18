"""Upwork email digest pipeline.

We do NOT scrape Upwork directly. The user configures Upwork to email job
matches to `upwork-worker@yourdomain.tld` (Gmail with app password). The
gmail_watcher worker IDLEs that mailbox, parses each digest, and pushes
extracted opps onto stream:rank directly.

This plugin is therefore a NO-OP at the crawler tier — it exists so the
registry has a strategy for the sources row, and so the scheduler logs the
fact that we 'crawled' it.
"""

from __future__ import annotations

from src.sources.base import CrawlPlan, SourcePlugin
from src.sources.registry import register


class _UpworkEmail:
    slug = "freelance_upwork_im"
    strategy = "freelance_upwork_im"

    async def plan(self, *, source_id: int, base_url: str, config: dict) -> CrawlPlan:
        # No fetch — gmail_watcher feeds this lane.
        return CrawlPlan(
            source_id=source_id,
            source_slug=self.slug,
            urls=[],
            tier_chain=[],
            requires_identity=False,
        )


PLUGIN: SourcePlugin = _UpworkEmail()
register(PLUGIN)

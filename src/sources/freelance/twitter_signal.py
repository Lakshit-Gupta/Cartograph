"""Twitter/X founder-signal — input-only via Nitter mirrors.

Polls a configured list of Nitter instances (Twitter mirrors that serve
public timelines without auth) for a user-curated set of founder/recruiter
handles. The polling loop lives in a dedicated worker
(`src/workers/twitter_signal.py`) that drives
`src.sources.freelance.twitter_fetcher.run()` — same shape as the Telegram
freelance lane.

This plugin returns an empty URL list so the regular HTTP crawler pool
ignores the source. The only reason to keep a SourcePlugin row is to give
the dispatcher / registry a callable to look up when an opp is published
with this `source_id`.
"""

from __future__ import annotations

from src.sources.base import CrawlPlan, SourcePlugin
from src.sources.registry import register


class _TwitterSignal:
    slug = "fl_twitter_signal"
    strategy = "twitter_founder_signal"

    async def plan(self, *, source_id: int, base_url: str, config: dict) -> CrawlPlan:
        return CrawlPlan(
            source_id=source_id,
            source_slug=self.slug,
            urls=[],
            tier_chain=[],
            requires_identity=False,
        )


PLUGIN: SourcePlugin = _TwitterSignal()
register(PLUGIN)

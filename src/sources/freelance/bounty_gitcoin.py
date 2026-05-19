"""Gitcoin bounty source plugin.

Phase 3.3 — Gitcoin retired its bounties product (the REST API at
`/api/v1/bounty/` 404s as of 2026-05-19 and the homepage now markets
quadratic-funding grants exclusively). The seed migration (V015) seeds
this source with `status='disabled'` so the scheduler ignores it; the
plugin also returns an empty CrawlPlan as defence-in-depth.

The web3 bounty slot is now filled by `bounty_superteam`
(`src/sources/freelance/bounty_superteam.py`). The Gitcoin extractor
code is retained as a reference REST shape and exercised by tests, so a
future revival is a config-only flip plus a URL update.
"""

from __future__ import annotations

from src.sources.base import CrawlPlan, SourcePlugin
from src.sources.registry import register


class _Gitcoin:
    slug = "bounty_gitcoin"
    strategy = "bounty_gitcoin"

    async def plan(self, *, source_id: int, base_url: str, config: dict) -> CrawlPlan:
        return CrawlPlan(
            source_id=source_id,
            source_slug=self.slug,
            urls=[],
            tier_chain=[0],
            requires_identity=False,
        )


PLUGIN: SourcePlugin = _Gitcoin()
register(PLUGIN)

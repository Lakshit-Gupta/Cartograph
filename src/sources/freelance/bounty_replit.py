"""Replit Bounties listing source plugin.

Phase 3.3 — Replit Bounties shut down and the product was migrated to
Contra in 2024. `replit.com/bounties` 301s to
`contra.com/replit/?utm_source=replit&utm_medium=referral&utm_campaign=bounties`.
The seed migration (V015) seeds this source with `status='disabled'`
so the scheduler ignores it anyway, but if the row is ever flipped
to `active` the plugin returns an empty CrawlPlan as a defence-in-depth
no-op so we never hammer the redirect target.

We keep the plugin registered (and the extractor) because:

  1. The strategy slug is referenced in code paths (metrics labels,
     audit log, source_health worker) — unregistering it would break
     `strategy_unregistered` warning telemetry.
  2. If Replit revives a direct bounty board (or another platform ships
     with the same `data-cy="bounty-card"` shape) the extractor is a
     drop-in target.
"""

from __future__ import annotations

from src.sources.base import CrawlPlan, SourcePlugin
from src.sources.registry import register


class _ReplitBounties:
    slug = "bounty_replit"
    strategy = "bounty_replit"

    async def plan(self, *, source_id: int, base_url: str, config: dict) -> CrawlPlan:
        # Product retired upstream. Even when the source row is active,
        # emit zero URLs so the crawler does not fetch the redirect.
        return CrawlPlan(
            source_id=source_id,
            source_slug=self.slug,
            urls=[],
            tier_chain=[0, 1],
            requires_identity=False,
        )


PLUGIN: SourcePlugin = _ReplitBounties()
register(PLUGIN)

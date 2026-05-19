"""Superteam Earn bounty source plugin.

Phase 3.3 — fills the web3-bounty slot the Gitcoin row was originally
meant to occupy. Superteam Earn's public listings API
(`https://superteam.fun/api/listings`) returns a JSON array of OPEN
bounties + jobs in the Solana ecosystem with reward amounts denominated
in USDC/USDT/USDG/SOL/ETH.

Tier 0 fetch — no Cloudflare, no JS rendering, no auth.
"""

from __future__ import annotations

from src.sources.base import CrawlPlan, SourcePlugin
from src.sources.registry import register


class _Superteam:
    slug = "bounty_superteam"
    strategy = "bounty_superteam"

    async def plan(self, *, source_id: int, base_url: str, config: dict) -> CrawlPlan:
        return CrawlPlan(
            source_id=source_id,
            source_slug=self.slug,
            urls=[base_url],
            tier_chain=[0],
            requires_identity=False,
        )


PLUGIN: SourcePlugin = _Superteam()
register(PLUGIN)

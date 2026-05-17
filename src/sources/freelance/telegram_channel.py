"""Telegram channel scraping via Telethon — input-only.

Scrapes a configured list of public freelance channels. Uses MTProto auth
(api_id + api_hash from SOPS). The fetcher does NOT use the HTTP dispatcher;
instead, the scheduler runs `src.sources.freelance.telegram_fetcher` as a
dedicated worker that publishes Opportunity payloads directly onto stream:rank.

This plugin returns an empty URL list so the regular crawler pool ignores it.
"""
from __future__ import annotations

from src.sources.base import CrawlPlan, SourcePlugin
from src.sources.registry import register


class _Telegram:
    slug = "freelance_telegram"
    strategy = "freelance_telegram"

    async def plan(self, *, source_id: int, base_url: str, config: dict) -> CrawlPlan:
        return CrawlPlan(
            source_id=source_id, source_slug=self.slug, urls=[],
            tier_chain=[], requires_identity=False,
        )


PLUGIN: SourcePlugin = _Telegram()
register(PLUGIN)

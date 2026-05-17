"""Ashby plugin — reads `config/sources/ashby_slugs.yaml`."""
from __future__ import annotations

from pathlib import Path

import yaml

from src.common.secrets import get_settings
from src.sources.base import CrawlPlan, SourcePlugin
from src.sources.registry import register


class _AshbyPlugin:
    slug = "ats_ashby"
    strategy = "ats_ashby"

    async def plan(self, *, source_id: int, base_url: str, config: dict) -> CrawlPlan:
        cfg_path = Path(get_settings().config_root) / "sources" / "ashby_slugs.yaml"
        slugs = yaml.safe_load(cfg_path.read_text()).get("slugs") or []
        urls = [
            f"https://api.ashbyhq.com/posting-api/job-board/{s}?includeCompensation=true"
            for s in slugs
        ]
        return CrawlPlan(
            source_id=source_id, source_slug=self.slug,
            urls=urls, tier_chain=[0], requires_identity=False,
        )


PLUGIN: SourcePlugin = _AshbyPlugin()
register(PLUGIN)

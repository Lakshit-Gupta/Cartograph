"""Greenhouse plugin — reads `config/sources/greenhouse_slugs.yaml`."""
from __future__ import annotations

from pathlib import Path

import yaml

from src.common.secrets import get_settings
from src.sources.base import CrawlPlan, SourcePlugin
from src.sources.registry import register


class _GreenhousePlugin:
    slug = "ats_greenhouse"
    strategy = "ats_greenhouse"

    async def plan(self, *, source_id: int, base_url: str, config: dict) -> CrawlPlan:
        cfg_path = Path(get_settings().config_root) / "sources" / "greenhouse_slugs.yaml"
        slugs = yaml.safe_load(cfg_path.read_text()).get("slugs") or []
        urls = [
            f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs?content=true"
            for s in slugs
        ]
        return CrawlPlan(
            source_id=source_id,
            source_slug=self.slug,
            urls=urls,
            tier_chain=[0],
            requires_identity=False,
        )


PLUGIN: SourcePlugin = _GreenhousePlugin()
register(PLUGIN)

"""Internshala source plugin — generates filtered URLs from the
config/sources/internshala_filters.yaml dimensions.

Why filtered URLs:
  Internshala exposes its preferences engine via URL paths. Crawling the
  base listing (`/internships/page-N/`) buries the user under thousands
  of irrelevant opps that our policy gate then has to filter out one
  by one. Instead we generate URL combinations that pre-filter on
  stipend / work-mode / category / location / keyword on Internshala's
  side, ingesting only opps that already match the user's preferences.

URL shapes observed 2026-05-29:
  /internships/<category>-internships/stipend-<amt>
  /internships/<work_mode>-internships/stipend-<amt>
  /internships/<work_mode>-<category>-internships/stipend-<amt>
  /internships/<category>-internships-in-<city>/stipend-<amt>
  /internships/keyword-<word>/stipend-<amt>

Each URL gets crawled across `pages_per_url` pages by appending
`/page-N/`.

The plugin falls back to the legacy 3-page base-URL crawl if the
filters file is missing or empty — that way an accidentally deleted
config doesn't silently stop Internshala ingestion entirely.
"""

from __future__ import annotations

from itertools import product
from pathlib import Path
from typing import Any

import yaml

from src.common.logger import get_logger
from src.common.secrets import get_settings
from src.sources.base import CrawlPlan, SourcePlugin
from src.sources.registry import register

_log = get_logger(__name__)


def _load_filters() -> dict[str, Any]:
    """Read `config/sources/internshala_filters.yaml` -> dict.

    Returns an empty dict on missing file / malformed YAML so the
    plugin can degrade to the legacy unfiltered crawl rather than
    raising into the scheduler tick.
    """
    settings = get_settings()
    path = Path(settings.config_root) / "sources" / "internshala_filters.yaml"
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        _log.warning("internshala_filters_yaml_read_failed", err=str(e), path=str(path))
        return {}
    return loaded.get("internshala") or {}


def _stipend_segment(stipend_min: int | None) -> str:
    """Suffix path segment for the stipend filter. Empty when filter off."""
    if stipend_min and stipend_min > 0:
        return f"/stipend-{int(stipend_min)}"
    return ""


def _filter_segments(
    *,
    work_mode: str | None,
    category: str | None,
    location: str | None,
    keyword: str | None,
) -> str:
    """Build the path component between `/internships/` and the
    `/stipend-N` suffix. Returns the in-between path WITHOUT leading or
    trailing slashes; caller adds them.

    Order rules observed from Internshala URLs:
      - work_mode prefixes category: `work-from-home-web-development-internships`
      - location is a suffix:        `web-development-internships-in-mumbai`
      - keyword is its own root:     `keyword-<word>`
    """
    if keyword is not None:
        return f"keyword-{keyword}"

    parts: list[str] = []
    if work_mode is not None:
        parts.append(work_mode)
    if category is not None:
        parts.append(category)
    parts.append("internships")
    segment = "-".join(parts)
    if location is not None:
        segment = f"{segment}-in-{location}"
    return segment


def _generate_urls(filters: dict[str, Any], pages_per_url: int) -> list[str]:
    """Cartesian-bounded URL generator. Returns a deduplicated list of
    fully-qualified URLs from the `combinations` block.

    For each combination entry, the entries' `axes` declare which
    dimensions cross. Per-entry override lists (e.g. `categories: [...]`)
    narrow the dimension below the file-level list. Anything not
    overridden falls back to the file-level lists.
    """
    base = "https://internshala.com/internships"
    stipend_seg = _stipend_segment(filters.get("stipend_min_inr"))

    work_modes_global = list(filters.get("work_modes") or [])
    categories_global = list(filters.get("categories") or [])
    locations_global = list(filters.get("locations") or [])
    keywords_global = list(filters.get("keywords") or [])
    combos = filters.get("combinations") or []

    urls: set[str] = set()

    for combo in combos:
        axes = combo.get("axes") or []
        work_modes = list(combo.get("work_modes") or work_modes_global)
        categories = list(combo.get("categories") or categories_global)
        locations = list(combo.get("locations") or locations_global)
        keywords = list(combo.get("keywords") or keywords_global)

        # Resolve the per-axis option lists inline (no nested closure —
        # ruff's B023 rightly flags closure-over-loop-var in case the
        # function escapes the loop iteration).
        wm_opts = work_modes if "work_mode" in axes else [None]
        cat_opts = categories if "category" in axes else [None]
        loc_opts = locations if "location" in axes else [None]
        kw_opts = keywords if "keyword" in axes else [None]

        for wm, cat, loc, kw in product(wm_opts, cat_opts, loc_opts, kw_opts):
            segment = _filter_segments(work_mode=wm, category=cat, location=loc, keyword=kw)
            url_base = f"{base}/{segment}{stipend_seg}"
            for p in range(1, pages_per_url + 1):
                urls.add(f"{url_base}/page-{p}/")

    return sorted(urls)


class _Internshala:
    slug = "india_internshala"
    strategy = "india_internshala"

    async def plan(self, *, source_id: int, base_url: str, config: dict) -> CrawlPlan:
        filters = _load_filters()
        pages_per_url = int(filters.get("pages_per_url", 3))

        if not filters or not (filters.get("combinations")):
            # Legacy fallback when the filters config is missing or empty.
            _log.info("internshala_filters_empty_fallback")
            urls = [f"{base_url}/page-{p}/" for p in range(1, pages_per_url + 1)]
        else:
            urls = _generate_urls(filters, pages_per_url)
            _log.info(
                "internshala_filtered_plan",
                url_count=len(urls),
                stipend_min_inr=filters.get("stipend_min_inr"),
                combinations=len(filters.get("combinations") or []),
            )

        return CrawlPlan(
            source_id=source_id,
            source_slug=self.slug,
            urls=urls,
            tier_chain=[0, 1],
            requires_identity=True,
        )


PLUGIN: SourcePlugin = _Internshala()
register(PLUGIN)

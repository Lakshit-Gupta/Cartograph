"""Jobs scrape orchestration: `run_combo` + `run_cycle`.

Mirrors the internship cycle but the "combo" is a URL variant (general /
fresher) rather than a dropdown combo — jobs filter via URL path, so `run_combo`
navigates the built `variant.url` and scrapes; there is no dropdown driving. The
shared page primitives (`scrape_cards`, `click_load_more`, `detect_challenge`,
`dismiss_modal`, `capture_miss`, `sel`) are imported from the internship
`browser_ops`; the cycle-report dataclass + `passes_validity` + `dedup_key` come
from the internship `report`. Only the two jobs gates (`passes_salary_floor`
strict-min, `passes_experience`) and the jobs card parser are jobs-specific.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

from src.common.logger import get_logger
from src.common.metrics import (
    discovery_cards_published_total,
    discovery_cards_rejected_total,
    discovery_combo_duration_seconds,
    discovery_combo_timeouts_total,
    discovery_selector_miss_total,
)
from src.common.queue import RedisQ
from src.extractors.persist import persist_and_publish
from src.fetchers.browser.behavioral import humanize_page
from src.fetchers.browser.engine import BrowserEngine
from src.sources.india.internshala_jobs_card_parser import parse_card
from src.workers.internshala_discovery.browser_ops import (
    CARD_WAIT_MS,
    ChallengeDetected,
    capture_miss,
    click_load_more,
    detect_challenge,
    dismiss_modal,
    scrape_cards,
    sel,
)
from src.workers.internshala_discovery.report import (
    DiscoveryCycleReport,
    dedup_key,
    passes_validity,
)
from src.workers.internshala_jobs_discovery.config import (
    SOURCE_SLUG,
    JobsDiscoveryConfig,
    JobVariant,
)
from src.workers.internshala_jobs_discovery.filters import passes_experience, passes_salary_floor

_log = get_logger(__name__)

_COMBO_TIMEOUT_SEC = 30


async def run_combo(
    engine: BrowserEngine,
    cookies: list[dict],
    ua: str | None,
    q: RedisQ,
    cfg: JobsDiscoveryConfig,
    variant: JobVariant,
    report: DiscoveryCycleReport,
    source_id: int,
) -> None:
    """Navigate one URL variant end-to-end, mutating `report` counters in place."""
    t0 = time.monotonic()
    card_root = sel(cfg, "listing", "card_root") or "div.individual_internship"
    listing_selectors = cfg.listing_selectors

    async with engine.open_context(cookies=cookies, ua=ua) as ctx:
        page = await ctx.new_page()
        try:
            await page.goto(variant.url, wait_until="domcontentloaded")
            await humanize_page(page)
            await dismiss_modal(page, cfg)
            await detect_challenge(page, cfg)

            try:
                # Readiness = first card visible. NEVER networkidle.
                await page.wait_for_selector(card_root, timeout=CARD_WAIT_MS, state="visible")
            except Exception:
                await capture_miss(page, variant, "listing.card_root")
                report.selector_misses.append(f"{variant.name}:listing.card_root")
                discovery_selector_miss_total.labels(combo=variant.name, key="listing.card_root").inc()
                _log.warning("jobs_card_root_miss", variant=variant.name, url=variant.url)
                return

            await detect_challenge(page, cfg)

            load_more = sel(cfg, "paginate", "load_more_button")
            end_marker = sel(cfg, "paginate", "list_end_marker")

            for page_n in range(cfg.pages_per_url):
                html = await page.content()
                for card_html in scrape_cards(html, card_root=card_root):
                    await _ingest_card(
                        card_html,
                        q=q,
                        cfg=cfg,
                        report=report,
                        source_id=source_id,
                        listing_selectors=listing_selectors,
                    )
                if page_n < cfg.pages_per_url - 1:
                    if end_marker and await page.query_selector(end_marker) is not None:
                        break
                    if not load_more or not await click_load_more(page, load_more):
                        break
                    await humanize_page(page)
            report.combos_succeeded += 1
        finally:
            try:
                await page.close()
            except Exception:
                pass
    discovery_combo_duration_seconds.labels(combo=variant.name).observe(time.monotonic() - t0)


async def _ingest_card(
    card_html: str,
    *,
    q: RedisQ,
    cfg: JobsDiscoveryConfig,
    report: DiscoveryCycleReport,
    source_id: int,
    listing_selectors: dict[str, str],
) -> None:
    """Parse -> salary-floor -> experience -> validity -> dedup -> persist one card."""
    report.cards_scraped += 1
    now = datetime.now(UTC)
    opp = parse_card(card_html, source_id=source_id, selectors=listing_selectors, now=now)
    if opp is None:
        report.cards_rejected_parse += 1
        discovery_cards_rejected_total.labels(reason="parse").inc()
        return

    if not passes_salary_floor(opp, cfg.salary_floor_inr):
        report.cards_rejected_subfloor += 1
        discovery_cards_rejected_total.labels(reason="subfloor").inc()
        return

    if not passes_experience(opp, cfg.max_experience_years):
        report.cards_rejected_experience += 1
        discovery_cards_rejected_total.labels(reason="experience").inc()
        return

    if not passes_validity(opp, now, cfg.max_age_days):
        report.cards_rejected_expired += 1
        discovery_cards_rejected_total.labels(reason="expired").inc()
        return

    if cfg.dry_run:
        report.cards_published += 1
        print(
            f"[dry-run] {opp.title} @ {opp.company or '?'} :: "
            f"{opp.comp_min}-{opp.comp_max} {opp.comp_currency}/{opp.comp_period} :: "
            f"exp_min={opp.years_experience_min} :: {opp.canonical_url}"
        )
        return

    key = dedup_key(opp.canonical_url)
    fresh = await q.raw.set(key, "1", ex=86_400, nx=True)
    if fresh is None:
        report.cards_rejected_dedup += 1
        discovery_cards_rejected_total.labels(reason="dedup").inc()
        return

    opp_id = await persist_and_publish(q, opp)
    if opp_id is None:
        report.cards_rejected_dedup += 1
        discovery_cards_rejected_total.labels(reason="dedup").inc()
        return
    report.cards_published += 1
    discovery_cards_published_total.inc()


async def run_cycle(
    engine: BrowserEngine,
    cookies: list[dict],
    ua: str | None,
    q: RedisQ,
    cfg: JobsDiscoveryConfig,
    *,
    worker_id: str,
    cycle_id: str,
    started_at: str,
    source_id: int,
) -> DiscoveryCycleReport:
    """Run every active URL variant under a 30 s per-variant wall-clock timeout."""
    t0 = time.monotonic()
    report = DiscoveryCycleReport(
        cycle_id=cycle_id,
        worker_id=worker_id,
        source_slug=SOURCE_SLUG,
        started_at=started_at,
        duration_sec=0.0,
        selectors_version=cfg.selectors_version,
        matrix_version="",
    )
    for variant in cfg.active_variants():
        report.combos_attempted += 1
        try:
            await asyncio.wait_for(
                run_combo(engine, cookies, ua, q, cfg, variant, report, source_id),
                timeout=_COMBO_TIMEOUT_SEC,
            )
        except TimeoutError:
            report.combo_timeouts.append(variant.name)
            discovery_combo_timeouts_total.labels(combo=variant.name).inc()
            _log.warning("jobs_combo_timeout", variant=variant.name)
            continue
        except ChallengeDetected as chal:
            report.healthy = False
            _log.error("jobs_challenge_detected", variant=variant.name, kind=chal.kind)
            break
        except Exception as exc:
            report.selector_misses.append(f"{variant.name}:exception")
            _log.warning("jobs_combo_failed", variant=variant.name, err=str(exc))
            continue

    report.duration_sec = round(time.monotonic() - t0, 2)
    return report


__all__ = ["run_combo", "run_cycle"]

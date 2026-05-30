"""Browser-driven scrape orchestration: `run_combo` + `run_cycle`.

`run_cycle` walks the dropdown matrix, wrapping each combo in a 30 s wall-clock
`asyncio.wait_for`. `run_combo` drives one dropdown click sequence through the
`BrowserEngine`, scrapes the resulting listing cards across `pages_per_url`
Load-more pages, floor-filters + dedups each card, and persists survivors via
`persist_and_publish`.

The low-level page operations (dropdown clicks, challenge detection, miss
capture) live in `browser_ops.py`; this module is the control flow on top of
them. Selector misses screenshot the page + skip the combo; a login redirect or
captcha aborts the whole cycle `healthy=False` (the worker NEVER solves a
challenge). Readiness is gated on `wait_for_selector(card_root)`, never
`networkidle` — Internshala telemetry keeps the connection busy indefinitely.
"""

from __future__ import annotations

import asyncio
import time

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
from src.sources.india.internshala_card_parser import parse_card
from src.workers.internshala_discovery.browser_ops import (
    CARD_WAIT_MS,
    ChallengeDetected,
    SelectorMiss,
    capture_miss,
    click_load_more,
    detect_challenge,
    drive_dropdowns,
    scrape_cards,
    sel,
)
from src.workers.internshala_discovery.config import (
    INTERNSHALA_LISTING_URL,
    SOURCE_SLUG,
    Combo,
    DiscoveryConfig,
)
from src.workers.internshala_discovery.report import (
    DiscoveryCycleReport,
    dedup_key,
    passes_floor,
)

_log = get_logger(__name__)

_COMBO_TIMEOUT_SEC = 30


async def run_combo(
    engine: BrowserEngine,
    cookies: list[dict],
    ua: str | None,
    q: RedisQ,
    cfg: DiscoveryConfig,
    combo: Combo,
    report: DiscoveryCycleReport,
    source_id: int,
) -> None:
    """Drive one combo end-to-end, mutating `report` counters in place.

    Raises `ChallengeDetected` (propagated by `run_cycle` to abort the cycle). A
    `SelectorMiss` is caught here: the page is screenshotted, the miss is
    recorded on the report, and the combo is skipped (other combos continue).
    """
    t0 = time.monotonic()
    card_root = sel(cfg, "listing", "card_root") or "div.individual_internship"
    listing_selectors = cfg.listing_selectors

    async with engine.open_context(cookies=cookies, ua=ua) as ctx:
        page = await ctx.new_page()
        try:
            await page.goto(INTERNSHALA_LISTING_URL, wait_until="domcontentloaded")
            await humanize_page(page)
            await detect_challenge(page, cfg)

            try:
                await drive_dropdowns(page, combo, cfg)
                # Readiness = first card visible. NEVER networkidle.
                await page.wait_for_selector(card_root, timeout=CARD_WAIT_MS, state="visible")
            except SelectorMiss as miss:
                await capture_miss(page, combo, miss.key)
                report.selector_misses.append(f"{combo.name}:{miss.key}")
                discovery_selector_miss_total.labels(combo=combo.name, key=miss.key).inc()
                _log.warning("discovery_selector_miss", combo=combo.name, key=miss.key)
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
    discovery_combo_duration_seconds.labels(combo=combo.name).observe(time.monotonic() - t0)


async def _ingest_card(
    card_html: str,
    *,
    q: RedisQ,
    cfg: DiscoveryConfig,
    report: DiscoveryCycleReport,
    source_id: int,
    listing_selectors: dict[str, str],
) -> None:
    """Parse -> floor-filter -> dedup -> persist one card, updating counters.

    Dry-run mode stops before the Redis dedup write + persist so `--dry-run`
    never mutates Redis or Postgres; the card still counts toward `cards_scraped`
    and the floor/parse tallies.
    """
    report.cards_scraped += 1
    opp = parse_card(card_html, source_id=source_id, selectors=listing_selectors)
    if opp is None:
        report.cards_rejected_parse += 1
        discovery_cards_rejected_total.labels(reason="parse").inc()
        return

    if not passes_floor(opp, cfg.comp_floor_inr):
        report.cards_rejected_subfloor += 1
        discovery_cards_rejected_total.labels(reason="subfloor").inc()
        return

    if cfg.dry_run:
        # No Redis / Postgres side effects in dry-run; print to stdout so the CLI
        # smoke surfaces the survivors.
        report.cards_published += 1
        print(f"[dry-run] {opp.title} @ {opp.company or '?'} :: {opp.comp_min}-{opp.comp_max} {opp.comp_currency} :: {opp.canonical_url}")
        return

    key = dedup_key(opp.canonical_url)
    fresh = await q.raw.set(key, "1", ex=86_400, nx=True)
    if fresh is None:
        report.cards_rejected_dedup += 1
        discovery_cards_rejected_total.labels(reason="dedup").inc()
        return

    opp_id = await persist_and_publish(q, opp)
    if opp_id is None:
        # persist_and_publish dedup hit (canonical_url / fingerprint already known
        # to the extractor dedup layer) — count as a dedup rejection.
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
    cfg: DiscoveryConfig,
    *,
    worker_id: str,
    cycle_id: str,
    started_at: str,
    source_id: int,
) -> DiscoveryCycleReport:
    """Run every active combo under a per-combo 30 s wall-clock timeout.

    Returns the populated `DiscoveryCycleReport`. A `ChallengeDetected` from any
    combo aborts the remaining combos and marks the cycle `healthy=False`.
    """
    t0 = time.monotonic()
    report = DiscoveryCycleReport(
        cycle_id=cycle_id,
        worker_id=worker_id,
        source_slug=SOURCE_SLUG,
        started_at=started_at,
        duration_sec=0.0,
        selectors_version=cfg.selectors_version,
        matrix_version=cfg.matrix_version,
    )
    for combo in cfg.active_combos():
        report.combos_attempted += 1
        try:
            await asyncio.wait_for(
                run_combo(engine, cookies, ua, q, cfg, combo, report, source_id),
                timeout=_COMBO_TIMEOUT_SEC,
            )
        except TimeoutError:
            report.combo_timeouts.append(combo.name)
            discovery_combo_timeouts_total.labels(combo=combo.name).inc()
            _log.warning("discovery_combo_timeout", combo=combo.name)
            continue
        except ChallengeDetected as chal:
            report.healthy = False
            _log.error("discovery_challenge_detected", combo=combo.name, kind=chal.kind)
            break
        except Exception as exc:
            report.selector_misses.append(f"{combo.name}:exception")
            _log.warning("discovery_combo_failed", combo=combo.name, err=str(exc))
            continue

    report.duration_sec = round(time.monotonic() - t0, 2)
    return report


__all__ = ["run_combo", "run_cycle"]

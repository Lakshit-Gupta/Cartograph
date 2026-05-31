"""Browser-driven scrape orchestration: `run_combo` + `run_cycle`.

`run_cycle` walks the category matrix, wrapping each combo in a wall-clock
`asyncio.wait_for` scaled to the page budget. `run_combo` builds the combo's
filtered listing URL (`build_combo_url`), navigates it page-by-page via
Internshala's numbered `/page-N/` pagination through the `BrowserEngine`,
scrapes each page's cards, floor-filters + dedups each card, and persists
survivors via `persist_and_publish`.

Internshala retired the "Load more" button — the listing now serves ~25
cards/page behind numbered `/page-N/` URLs + a "Next" link (verified
2026-05-31). We navigate those URLs directly (deterministic, no scroll-timing
race) and stop early on the first empty / repeated page. There is no dropdown
driving: the category/stipend/work-mode filters live in the URL path.

The low-level page operations (challenge detection, modal dismiss, miss capture,
`/page-N/` URL building) live in `browser_ops.py`; this module is the control
flow on top of them. A login redirect or captcha aborts the whole cycle
`healthy=False` (the worker NEVER solves a challenge). Readiness is gated on
`wait_for_selector(card_root)`, never `networkidle` — Internshala telemetry
keeps the connection busy indefinitely.
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
from src.sources.india.internshala_card_parser import parse_card
from src.workers.internshala_discovery.browser_ops import (
    CARD_WAIT_MS,
    ChallengeDetected,
    capture_miss,
    detect_challenge,
    dismiss_modal,
    page_signature,
    page_url,
    scrape_cards,
    sel,
)
from src.workers.internshala_discovery.config import (
    SOURCE_SLUG,
    Combo,
    DiscoveryConfig,
    build_combo_url,
)
from src.workers.internshala_discovery.report import (
    DiscoveryCycleReport,
    dedup_key,
    passes_floor,
    passes_validity,
)

_log = get_logger(__name__)

_COMBO_TIMEOUT_SEC = 30  # floor
_COMBO_BASE_SEC = 30  # goto + humanize + first-card wait
_PER_PAGE_BUDGET_SEC = 15  # per /page-N/ navigation (goto + humanize + scrape)
_NAV_TIMEOUT_MS = 25_000  # explicit page.goto cap (logged-in page can stall domcontentloaded)
_OP_TIMEOUT_MS = 15_000  # default cap for every other page op


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
    """Navigate one combo's filtered listing page-by-page, mutating `report`.

    Raises `ChallengeDetected` (propagated by `run_cycle` to abort the cycle). A
    page-1 `card_root` miss (dead category slug / empty filtered set) is
    screenshotted, recorded on the report, and the combo is skipped (other
    combos continue). Pagination stops early on the first empty or repeated page.
    """
    t0 = time.monotonic()
    card_root = sel(cfg, "listing", "card_root") or "div.individual_internship"
    listing_selectors = cfg.listing_selectors
    base_url = build_combo_url(combo, comp_floor_inr=cfg.comp_floor_inr)

    async with engine.open_context(cookies=cookies, ua=ua) as ctx:
        page = await ctx.new_page()
        # Hard-cap every page op — Internshala's persistent telemetry can stall
        # `domcontentloaded` indefinitely; without an explicit nav timeout `goto`
        # hangs forever and the asyncio wall-clock can't cancel a stuck CDP await.
        page.set_default_navigation_timeout(_NAV_TIMEOUT_MS)
        page.set_default_timeout(_OP_TIMEOUT_MS)
        try:
            await page.goto(page_url(base_url, 1), wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
            await humanize_page(page)
            await dismiss_modal(page, cfg)
            await detect_challenge(page, cfg)

            try:
                # Readiness = first card visible. NEVER networkidle.
                await page.wait_for_selector(card_root, timeout=CARD_WAIT_MS, state="visible")
            except Exception:
                # No cards on page 1: a dead category slug or a genuinely empty
                # filtered set. Screenshot for recon, record the miss, skip combo.
                await capture_miss(page, combo, "listing.card_root")
                report.selector_misses.append(f"{combo.name}:listing.card_root")
                discovery_selector_miss_total.labels(combo=combo.name, key="listing.card_root").inc()
                _log.warning("discovery_card_root_miss", combo=combo.name, url=base_url)
                return

            prev_sig = ""
            for page_n in range(1, cfg.pages_per_url + 1):
                if page_n > 1:
                    # Numbered server-side pagination — navigate the next page.
                    await page.goto(page_url(base_url, page_n), wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
                    await humanize_page(page)
                    await detect_challenge(page, cfg)
                    try:
                        await page.wait_for_selector(card_root, timeout=CARD_WAIT_MS, state="visible")
                    except Exception:
                        break  # no card root on this page -> end of results
                html = await page.content()
                cards = scrape_cards(html, card_root=card_root)
                if not cards:
                    break
                sig = page_signature(cards)
                if page_n > 1 and sig == prev_sig:
                    break  # pagination didn't advance (out-of-range -> redirect to page 1)
                prev_sig = sig
                for card_html in cards:
                    await _ingest_card(
                        card_html,
                        q=q,
                        cfg=cfg,
                        report=report,
                        source_id=source_id,
                        listing_selectors=listing_selectors,
                    )
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
    now = datetime.now(UTC)
    opp = parse_card(card_html, source_id=source_id, selectors=listing_selectors, now=now)
    if opp is None:
        report.cards_rejected_parse += 1
        discovery_cards_rejected_total.labels(reason="parse").inc()
        return

    if not passes_floor(opp, cfg.comp_floor_inr):
        report.cards_rejected_subfloor += 1
        discovery_cards_rejected_total.labels(reason="subfloor").inc()
        return

    # Expired / stale guard: drop cards whose "Apply By" deadline has passed
    # (or, lacking a deadline, that are older than max_age_days). Sits right
    # after the floor so it shares one clock with parse_card and rejects before
    # any Redis / Postgres side effects.
    if not passes_validity(opp, now, cfg.max_age_days):
        report.cards_rejected_expired += 1
        discovery_cards_rejected_total.labels(reason="expired").inc()
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
    # Scale the per-combo wall-clock with the page budget: each /page-N/ adds a
    # goto + humanize pause. A flat 30 s starved multi-page runs once pagination
    # walks past page 1. Early-stop ends most combos in 1-4 pages regardless.
    combo_timeout = max(_COMBO_TIMEOUT_SEC, cfg.pages_per_url * _PER_PAGE_BUDGET_SEC + _COMBO_BASE_SEC)
    for combo in cfg.active_combos():
        report.combos_attempted += 1
        try:
            await asyncio.wait_for(
                run_combo(engine, cookies, ua, q, cfg, combo, report, source_id),
                timeout=combo_timeout,
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

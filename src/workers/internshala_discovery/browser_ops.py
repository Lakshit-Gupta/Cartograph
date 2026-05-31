"""Page-interaction primitives for the discovery cycle.

Split out of `cycle.py` to keep both files under the 300-line ceiling. Holds the
low-level Playwright `page` operations (challenge detection, modal dismiss,
selector-miss capture, card scraping, `/page-N/` URL pagination) plus the
`ChallengeDetected` control-flow exception. `cycle.py` owns the orchestration
(`run_cycle` / `run_combo`).

Pagination is by numbered server-side `/page-N/` URLs (Internshala retired the
"Load more" button; the listing now serves ~25 cards/page behind a "Next" link).
Readiness is always gated on an explicit selector wait, NEVER `networkidle` —
Internshala's background telemetry keeps the connection busy indefinitely.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from selectolax.parser import HTMLParser

from src.common.logger import get_logger
from src.workers.internshala_discovery.config import Combo, DiscoveryConfig

_log = get_logger(__name__)

# Selector-miss artefacts. The spec mandates /tmp/discovery/miss/; /tmp is tmpfs
# in compose.sidecar.yaml, so this never hits the spare's persistent disk.
_MISS_DIR = Path("/tmp/discovery/miss")  # noqa: S108 - tmpfs scratch on the sidecar, see compose.sidecar.yaml
CARD_WAIT_MS = 8_000
_DOM_CLIP_BYTES = 50_000
_SIG_CLIP_BYTES = 300  # repeat-page guard: first card's leading bytes


class ChallengeDetected(Exception):
    """Login redirect or captcha marker — aborts the whole cycle healthy=False."""

    def __init__(self, kind: str) -> None:
        super().__init__(f"challenge: {kind}")
        self.kind = kind


def sel(cfg: DiscoveryConfig, group: str, key: str) -> str | None:
    """Look up `selectors.<group>.<key>`; None when absent."""
    node = cfg.selectors.get(group)
    if isinstance(node, dict):
        return node.get(key)
    return None


def _top_sel(cfg: DiscoveryConfig, key: str) -> str | None:
    val = cfg.selectors.get(key)
    return val if isinstance(val, str) else None


async def dismiss_modal(page: Any, cfg: DiscoveryConfig) -> None:
    """Onboarding modal is best-effort — never fatal if absent."""
    selector = _top_sel(cfg, "modal_dismiss")
    if not selector:
        return
    try:
        await page.wait_for_selector(selector, timeout=2_000, state="visible")
        await page.click(selector)
    except Exception:
        # Modal usually not present; absence is the common, healthy case.
        return


async def detect_challenge(page: Any, cfg: DiscoveryConfig) -> None:
    """Raise `ChallengeDetected` on a login redirect or captcha marker."""
    try:
        url = page.url or ""
    except Exception:
        url = ""
    if "/login" in url:
        raise ChallengeDetected("login_redirect")
    for kind, key in (("login", "login_marker"), ("captcha", "captcha_marker")):
        selector = _top_sel(cfg, key)
        if not selector:
            continue
        try:
            node = await page.query_selector(selector)
        except Exception:
            node = None
        if node is not None:
            raise ChallengeDetected(kind)


async def capture_miss(page: Any, combo: Combo, key: str) -> None:
    """Screenshot + clipped DOM to /tmp/discovery/miss for operator recon."""
    ts = int(time.time())
    stem = _MISS_DIR / f"{combo.name}_{ts}"
    try:
        # Sync pathlib mkdir in an async fn: fires only on a selector miss (rare,
        # not a hot loop), creates the tmpfs dir once — same one-shot rationale
        # as the obsidian writer / internshala submitter ASYNC240 ignores.
        _MISS_DIR.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
        await page.screenshot(path=f"{stem}.png", full_page=False)
        html = await page.content()
        (stem.with_suffix(".html")).write_text(html[:_DOM_CLIP_BYTES], encoding="utf-8")
    except Exception as exc:  # screenshotting must never crash the cycle
        _log.warning("discovery_miss_capture_failed", combo=combo.name, key=key, err=str(exc))


def scrape_cards(html: str, *, card_root: str) -> list[str]:
    """Split the page HTML into per-card outerHTML strings."""
    return [node.html or "" for node in HTMLParser(html).css(card_root)]


def page_url(base_url: str, page_n: int) -> str:
    """Internshala `/page-N/` pagination URL. Page 1 is the bare base.

    `base_url` is the filtered listing URL (e.g. a combo / variant URL); page 1
    loads it as-is, pages >=2 append `/page-N/`. Trailing slashes on `base_url`
    are normalised so we never emit `//page-2/`.
    """
    base = base_url.rstrip("/")
    if page_n <= 1:
        return f"{base}/"
    return f"{base}/page-{page_n}/"


def page_signature(cards: list[str]) -> str:
    """Cheap repeat-page guard: the first card's leading bytes (or "").

    Internshala redirects an out-of-range `/page-N/` back to page 1, re-serving
    page 1's cards. Comparing this signature to the previous page lets the cycle
    stop the moment pagination fails to advance, instead of burning the whole
    page budget re-scraping (already-deduped) page-1 cards.
    """
    if not cards:
        return ""
    return cards[0][:_SIG_CLIP_BYTES]


__all__ = [
    "CARD_WAIT_MS",
    "ChallengeDetected",
    "capture_miss",
    "detect_challenge",
    "dismiss_modal",
    "page_signature",
    "page_url",
    "scrape_cards",
    "sel",
]

"""Page-interaction primitives for the discovery cycle.

Split out of `cycle.py` to keep both files under the 300-line ceiling. Holds the
low-level Playwright `page` operations (dropdown driving, challenge detection,
selector-miss capture, Load-more clicking) plus the two control-flow exceptions
they raise. `cycle.py` owns the orchestration (`run_cycle` / `run_combo`).

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
_ACTION_WAIT_MS = 6_000
_DOM_CLIP_BYTES = 50_000


class SelectorMiss(Exception):
    """A required selector did not appear — combo is screenshotted and skipped."""

    def __init__(self, key: str) -> None:
        super().__init__(f"selector_miss: {key}")
        self.key = key


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


async def _click_when_ready(page: Any, selector: str, key: str, *, timeout_ms: int = _ACTION_WAIT_MS) -> None:
    """Best-effort wait-then-click. Raises `SelectorMiss(key)` on absence."""
    try:
        await page.wait_for_selector(selector, timeout=timeout_ms, state="visible")
        await page.click(selector)
    except SelectorMiss:
        raise
    except Exception as exc:  # playwright TimeoutError + click errors
        raise SelectorMiss(key) from exc


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


async def drive_dropdowns(page: Any, combo: Combo, cfg: DiscoveryConfig) -> None:
    """Click stipend -> 'above 10000', category, and the WFH chip in order.

    Each step waits for its target selector before clicking; any miss raises
    `SelectorMiss` keyed by the missing control so the operator can recon it.
    """
    await dismiss_modal(page, cfg)

    stipend_btn = sel(cfg, "dropdown", "stipend_button")
    stipend_opt = sel(cfg, "dropdown", "stipend_option_above_10000")
    if stipend_btn and stipend_opt:
        await _click_when_ready(page, stipend_btn, "dropdown.stipend_button")
        await _click_when_ready(page, stipend_opt, "dropdown.stipend_option_above_10000")

    cat_btn = sel(cfg, "dropdown", "category_button")
    cat_opts = sel(cfg, "dropdown", "category_options")
    if cat_btn and cat_opts:
        await _click_when_ready(page, cat_btn, "dropdown.category_button")
        # Chosen renders option <li>s only after the trigger opens; pick the one
        # whose visible text matches the combo category.
        option = f"{cat_opts} >> text={combo.category}"
        await _click_when_ready(page, option, "dropdown.category_options")

    if combo.work_mode == "wfh":
        wfh = sel(cfg, "dropdown", "work_mode_wfh_chip")
        if wfh:
            await _click_when_ready(page, wfh, "dropdown.work_mode_wfh_chip")


def scrape_cards(html: str, *, card_root: str) -> list[str]:
    """Split the page HTML into per-card outerHTML strings."""
    return [node.html or "" for node in HTMLParser(html).css(card_root)]


async def click_load_more(page: Any, selector: str) -> bool:
    """Click the Load-more control; return False when it is absent (end of list).

    `humanize_page` is intentionally NOT called here — the caller drives stealth
    pauses around the page navigation; keeping this primitive pure-click avoids a
    fetcher-tier import in this low-level module.
    """
    try:
        node = await page.query_selector(selector)
        if node is None:
            return False
        await node.click()
        return True
    except Exception:
        return False


__all__ = [
    "CARD_WAIT_MS",
    "ChallengeDetected",
    "SelectorMiss",
    "capture_miss",
    "click_load_more",
    "detect_challenge",
    "dismiss_modal",
    "drive_dropdowns",
    "scrape_cards",
    "sel",
]

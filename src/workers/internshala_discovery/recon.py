"""One-shot live-DOM recon for the Internshala discovery selectors.

NOT part of the running worker. Run this ONCE on the ThinkPad against a live,
logged-in Internshala session to capture the real DOM, so the placeholder
selectors in ``config/sources/internshala_selectors.yaml`` (which ships with
``version: RECON_PENDING``) can be replaced with verified values.

It leases the same ``platform='internshala'`` identity the worker uses, opens
``https://internshala.com/internships/`` through the same ``CamoufoxEngine``,
and emits a high-signal text report to **stdout** (so it survives ``--rm`` with
no bind-mount or file-permission fuss). The report carries:

  * the resolved page URL + title + a login-redirect check (dead cookies → loud),
  * a keyword sweep of every element whose id/class smells like a filter control
    (``stipend`` / ``category`` / ``chosen`` / ``work_from_home`` / ``location``),
  * a MATCH/MISS probe of every candidate selector for each control we drive,
  * the outerHTML of the first listing card (to confirm the card-parser selectors),
  * a best-effort "open the category dropdown and dump the revealed options" pass.

A full-page screenshot + full HTML are *also* written to ``RECON_OUT`` (default
``/app/Screenshots/recon``) when that path is writable — bind-mount
``./Screenshots:/app/Screenshots`` to keep them past ``--rm``. They are a bonus;
the stdout report is the artefact to copy back.

Run (on the ThinkPad, inside the discovery image)::

    docker compose -f compose.sidecar.yaml run --rm \\
      -e INTERNSHALA_ALLOW_RECON_PENDING=1 \\
      -v "$(pwd)/Screenshots:/app/Screenshots" \\
      --entrypoint python internshala-discovery-worker \\
      -m src.workers.internshala_discovery.recon

Exit codes: 0 = report emitted, 2 = no healthy internshala identity to lease,
3 = the session is logged out (cookies dead — re-run identity warmup first).
"""

from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from src.common.db import close_pool, init_pool
from src.common.logger import configure_logging, get_logger

configure_logging("internshala_discovery_recon")
_log = get_logger(__name__)

_LISTING_URL = "https://internshala.com/internships/"
_PLATFORM = "internshala"

# Candidate selectors per control we need. Each list is tried in order; the
# recon reports which (if any) match on the live page. Order = current
# placeholder first, then the most-likely alternates from past Internshala DOMs.
_CANDIDATES: dict[str, list[str]] = {
    "stipend_button": [
        "#select_stipend",
        "#stipend_filter",
        "#stipend",
        "#stipend_chosen .chosen-single",
        "[name='stipend']",
        ".stipend_filter",
    ],
    "stipend_slider": [
        "#stipend_slider",
        ".noUi-handle",
        "input[type='range']",
        "#minimum_stipend",
    ],
    "stipend_option_above_10000": [
        "label[for='stipend_radio_4']",
        "#stipend_chosen .chosen-results li",
        "[data-stipend='10000']",
    ],
    "category_button": [
        "#select_category_chosen .chosen-single",
        "#category_chosen .chosen-single",
        "#select_category",
        "#category",
        "[name='category']",
        "#categories_chosen .chosen-single",
    ],
    "category_options": [
        "#select_category_chosen .chosen-results li",
        "#category_chosen .chosen-results li",
        "#categories_chosen .chosen-results li",
    ],
    "work_mode_wfh_chip": [
        "label[for='work_from_home']",
        "#work_from_home",
        "[name='work_from_home']",
        ".filter_radio_work_from_home",
    ],
    "location_button": [
        "#select_location_chosen .chosen-single",
        "#city_chosen .chosen-single",
        "#select_city",
        "#location_names_chosen .chosen-single",
    ],
    "location_options": [
        "#select_location_chosen .chosen-results li",
        "#city_chosen .chosen-results li",
    ],
    "card_root": [
        "div.individual_internship",
        ".individual_internship",
        "[internshipid]",
        ".internship_meta",
    ],
    "card_title": [
        ".heading_4_5.profile",
        ".job-internship-name",
        ".profile h3",
        "h3.heading_4_5",
    ],
    "card_company": [
        ".company_and_premium .company-name",
        "p.company a",
        ".company-name",
        ".company_name",
    ],
    "card_location": [
        ".locations span a",
        ".location_link",
        "#location_names a",
    ],
    "card_stipend": [
        ".stipend",
        ".stipend_container_table_cell",
        "span.stipend",
    ],
    "card_apply_link": [
        "a.view_detail_button",
        ".view_detail_button",
        "a[href*='/internship/detail']",
        ".individual_internship a[href*='/internship/']",
    ],
    "load_more_button": [
        "#load_more_internships_button",
        "a.click_source",
        ".load_more",
    ],
    "list_end_marker": [
        ".no-search-results",
        ".empty-listing",
        "#no_internships_found",
    ],
}

# JS sweep: enumerate every element whose id/class smells like a filter control.
_SWEEP_JS = """
() => {
  const kw = ['stipend','category','categories','work_from_home','work-from-home',
              'wfh','chosen','location','city','profile_filter','filter','keyword',
              'individual_internship','internship_meta'];
  const seen = new Set();
  const out = [];
  for (const el of document.querySelectorAll('*')) {
    const id = el.id || '';
    const cls = (typeof el.className === 'string' ? el.className : '') || '';
    const hay = (id + ' ' + cls).toLowerCase();
    if (!kw.some(k => hay.includes(k))) continue;
    const clsPart = cls.trim() ? '.' + cls.trim().split(/\\s+/).join('.') : '';
    const sig = el.tagName.toLowerCase() + (id ? '#' + id : '') + clsPart;
    if (seen.has(sig)) continue;
    seen.add(sig);
    const txt = (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 50);
    out.push(sig + ' :: ' + txt);
  }
  return out.slice(0, 250);
}
"""

_SIG_JS = (
    "e => e.tagName.toLowerCase()"
    " + (e.id ? '#' + e.id : '')"
    " + (e.className && typeof e.className === 'string'"
    " ? '.' + e.className.trim().split(/\\s+/).join('.') : '')"
)


def _cookies_to_pw(cookies: dict[str, str], base: str = "https://internshala.com") -> list[dict]:
    """Identity dict (name->value) → Playwright cookie-list shape."""
    parsed = urlparse(base)
    host = parsed.hostname or ""
    url = f"{parsed.scheme}://{host}" if host else base
    return [{"name": k, "value": v, "url": url} for k, v in cookies.items()]


def _hr(title: str) -> None:
    print(f"\n{'=' * 8} {title} {'=' * 8}")


def _dump_text(path: Path, content: str) -> None:
    """Sync file write — kept out of the async path so blocking IO is explicit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


async def _probe_one(page: Any, selector: str) -> str:
    try:
        els = await page.query_selector_all(selector)
    except Exception as exc:  # invalid selector / engine hiccup
        return f"  ERR   {selector!r}: {exc}"
    if not els:
        return f"  MISS  {selector!r}"
    try:
        sig = await els[0].evaluate(_SIG_JS)
    except Exception:
        sig = "<sig-failed>"
    return f"  MATCH {selector!r}  count={len(els)}  first={sig}"


async def _probe_candidates(page: Any) -> None:
    _hr("CANDIDATE SELECTOR PROBE")
    for key, selectors in _CANDIDATES.items():
        print(f"\n[{key}]")
        for selector in selectors:
            print(await _probe_one(page, selector))


async def _keyword_sweep(page: Any) -> None:
    _hr("KEYWORD SWEEP (filter-ish elements: tag#id.class :: text)")
    try:
        hits: list[str] = await page.evaluate(_SWEEP_JS)
    except Exception as exc:
        print(f"  sweep failed: {exc}")
        return
    if not hits:
        print("  (no filter-ish elements found — page may be logged out or restructured)")
        return
    for line in hits:
        print(f"  {line}")


async def _dump_first_card(page: Any) -> None:
    _hr("FIRST LISTING CARD outerHTML (verify card-parser selectors)")
    for selector in _CANDIDATES["card_root"]:
        try:
            node = await page.query_selector(selector)
        except Exception:
            node = None
        if node is None:
            continue
        try:
            html = await node.evaluate("e => e.outerHTML")
        except Exception as exc:
            print(f"  matched {selector!r} but outerHTML failed: {exc}")
            return
        print(f"  (matched card_root {selector!r})\n")
        print(html[:3000])
        return
    print("  no card_root candidate matched — listing cards not found on the page")


async def _open_category_dropdown(page: Any) -> None:
    """Best-effort: click the first matching category trigger, dump revealed options.

    jQuery-Chosen renders option <li>s into the DOM up front (display:none until
    open), so the static sweep usually catches them — but clicking confirms the
    trigger selector actually opens the list, which is the thing we must drive.
    """
    _hr("CATEGORY DROPDOWN OPEN ATTEMPT")
    trigger = None
    for selector in _CANDIDATES["category_button"]:
        try:
            node = await page.query_selector(selector)
        except Exception:
            node = None
        if node is not None:
            trigger = (selector, node)
            break
    if trigger is None:
        print("  no category_button candidate matched — cannot test open")
        return
    selector, node = trigger
    print(f"  clicking category trigger {selector!r} ...")
    try:
        await node.click()
        await page.wait_for_timeout(800)
    except Exception as exc:
        print(f"  click failed: {exc}")
        return
    for opt_sel in _CANDIDATES["category_options"]:
        try:
            opts = await page.query_selector_all(opt_sel)
        except Exception:
            opts = []
        if not opts:
            continue
        print(f"  options selector {opt_sel!r} matched count={len(opts)} — first 25 texts:")
        for opt in opts[:25]:
            try:
                txt = (await opt.evaluate("e => (e.textContent||'').trim()")) or ""
            except Exception:
                txt = "<text-failed>"
            print(f"    - {txt[:60]}")
        return
    print("  trigger clicked but no category_options candidate revealed any <li> — recon the option list manually")


async def _save_artifacts(page: Any, out_dir: Path) -> None:
    _hr("ARTIFACTS")
    try:
        # One-shot recon, not a hot loop — same blocking-mkdir rationale as the
        # worker's miss-capture (browser_ops.capture_miss ASYNC240 ignore).
        out_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
        shot = out_dir / "listing_full.png"
        await page.screenshot(path=str(shot), full_page=True)
        print(f"  screenshot -> {shot}")
    except Exception as exc:
        print(f"  screenshot skipped ({exc}) — stdout report is the primary artefact")
    try:
        html = await page.content()
        target = out_dir / "listing.html"
        _dump_text(target, html)
        print(f"  full HTML  -> {target} ({len(html)} bytes)")
    except Exception as exc:
        print(f"  full HTML skipped ({exc})")


async def _run(page: Any, out_dir: Path) -> int:
    await page.goto(_LISTING_URL, wait_until="domcontentloaded")
    # Behavioral nudge is optional here; import lazily so recon works even if the
    # ghost-cursor extras are absent.
    try:
        from src.fetchers.browser.behavioral import humanize_page

        await humanize_page(page)
    except Exception:
        pass
    await page.wait_for_timeout(2500)

    url = ""
    title = ""
    try:
        url = page.url or ""
        title = await page.title()
    except Exception:
        pass

    _hr("PAGE")
    print(f"  url:   {url}")
    print(f"  title: {title}")

    if "/login" in url or "login" in title.lower():
        print("\n  !! SESSION LOGGED OUT — redirected to login. Cookies are dead.")
        print("  !! Re-run identity warmup so a fresh internshala cookie is vaulted, then re-run recon.")
        await _save_artifacts(page, out_dir)
        return 3

    await _keyword_sweep(page)
    await _probe_candidates(page)
    await _dump_first_card(page)
    await _open_category_dropdown(page)
    await _save_artifacts(page, out_dir)

    _hr("DONE")
    print("  Copy the whole report above. It maps each control to its real selector.")
    return 0


async def main() -> int:
    out_dir = Path(os.environ.get("RECON_OUT", "/app/Screenshots/recon"))
    worker_id = f"recon-{socket.gethostname()}-{os.getpid()}"

    await init_pool()
    from src.common import identity_vault

    lease = await identity_vault.checkout(platform=_PLATFORM, worker_id=worker_id, lease_seconds=900)
    if lease is None:
        print("NO HEALTHY 'internshala' IDENTITY TO LEASE — seed/warmup an identity first.")
        await close_pool()
        return 2

    from src.fetchers.browser.camoufox_engine import CamoufoxEngine

    cookies = _cookies_to_pw(lease.cookies)
    engine = CamoufoxEngine(headless=True)
    code = 1
    try:
        async with engine.open_context(cookies=cookies, ua=lease.ua_string) as ctx:
            page = await ctx.new_page()
            try:
                code = await _run(page, out_dir)
            finally:
                try:
                    await page.close()
                except Exception:
                    pass
    except Exception as exc:
        _log.exception("recon_fatal", err=str(exc))
        print(f"\nRECON FATAL: {exc}")
        code = 1
    finally:
        try:
            await engine.shutdown()
        except Exception:
            pass
        try:
            await identity_vault.release(lease.lease_id)
        except Exception:
            pass
        await close_pool()
    return code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

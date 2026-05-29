"""ThinkPad-side Internshala Easy Apply driver.

Runs INSIDE the camoufox browser context provided by
`src/workers/apply_browser_worker.py`. Navigates to the opp URL,
clicks Easy Apply, fills the modal, uploads the PDF, and either:

  * dry_run=True   - screenshots the filled modal and returns WITHOUT
                     clicking Submit. The Pi-side card surfaces the
                     screenshot for human verification.
  * dry_run=False  - clicks Submit, waits for the confirmation banner,
                     screenshots the result, returns status='ok'.

================================================================
SELECTORS ARE PLACEHOLDERS — recon required before flipping live.
================================================================

The selectors below are the EXPECTED shape based on a 2026-02 manual
walkthrough of the Internshala Easy Apply modal. They WILL drift —
Internshala redesigns the modal roughly once per quarter. Verification
procedure lives in `docs/runbooks/sidecar_setup.md` §9.3.

When the selectors drift, the submitter detects the miss in
`_assert_present()`, screenshots the broken page, and returns
`status='failed'` with `error="selector_miss: <which one>"`. The
Pi-side apply-result-worker posts the screenshot to Discord so the user
can paste the new selector into INTERNSHALA_SELECTORS in this file.

Bump INTERNSHALA_SELECTORS_VERSION whenever the constants change so
the result rows in `applications.payload->>selectors_version` stay
forensically interpretable across DOM revisions.
"""

from __future__ import annotations

import asyncio
import base64
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.common.logger import get_logger
from src.fetchers.browser.behavioral import humanize_page

_log = get_logger(__name__)

INTERNSHALA_SELECTORS_VERSION = "2026.05.29-raju-recon-v6-div-apply-cta"

# Selector map — keys are stable internal names referenced by run_internshala_apply.
# Values are the live CSS/XPath selectors. Recon the actual DOM on the spare
# and patch these. Do NOT inline selectors in the run_*_apply body — the
# whole point of the map is that drift is a one-line fix.
INTERNSHALA_SELECTORS: dict[str, str] = {
    # CONFIRMED 2026-05-29 from raju's real listing + form DOM dumps.
    #
    # Internshala uses an in-place modal flow. Each internship card on the
    # listing page is `<div class="individual_internship easy_apply">` with
    # `data-source_cta="easy_apply"`. Clicking it opens `#easy_apply_modal`
    # via a global JS handler. The form HTML AJAX-loads into
    # `#application-form-container` shortly after. The form is a `<form
    # id="application-form">` with hidden CSRF + internshipId inputs
    # populated server-side.
    #
    # Form structure (confirmed):
    #   #application-form-container > #form-container > #application-form
    #     ├─ #assessment_questions_container
    #     │   ├─ hidden inputs (csrf, internshipId, etc.)
    #     │   ├─ #confirm_availability_container
    #     │   │   └─ input[name="confirm_availability"][value="yes"]#radio1  ← pre-checked
    #     │   │   └─ input[name="confirm_availability"][value="no"]#radio4
    #     │   ├─ .questions-container  (custom questions, may be empty)
    #     │   ├─ .custom-resume-container
    #     │   │   ├─ input[type=file]#custom_resume[name=custom_resume]  ← display:none, set_input_files works
    #     │   │   └─ input[type=hidden]#prefilled_custom_resume  ← server-side default
    #     │   └─ (optional) textarea[name=cover_letter] when is_cover_letter_visible
    #     └─ #submit  (input[type=submit])
    # CONFIRMED 2026-05-29 via raju's detail-page DOM dump:
    # The active apply CTA on the detail page is a STYLED DIV, not a
    # <button>: `<div class="apply btn btn-primary">Apply</div>`. That's
    # why every v5 selector targeting button/a elements missed.
    # First fallback: the .apply.btn-primary div. Then listing-page card
    # (when apply_url is a listing URL). Final fallback: anything with
    # data-source_cta="easy_apply".
    "easy_apply_button": ("div.apply.btn-primary, div.apply.btn, .individual_internship.easy_apply, [data-source_cta='easy_apply']"),
    # Closed-application detection — replaced with a page.inner_text()
    # substring check in run_internshala_apply because Playwright's
    # `text=` selector engine cannot be comma-chained with CSS
    # selectors, and the orange banner has no stable class anchor across
    # Internshala redesigns. The substring "Applications are closed"
    # appears verbatim in both screenshots (raju 2026-05-29). Kept here
    # for documentation only — NOT used by _assert_present.
    "_closed_banner_text_marker": "Applications are closed",
    # Outer modal — opens via global JS handler. Starts display:none.
    "modal": "#easy_apply_modal",
    # CONFIRMED: form fields land inside #application-form-container after
    # the AJAX fetch. The form itself is #application-form.
    "form_container": "#application-form-container",
    "form": "#application-form",
    # CONFIRMED: input[type=file]#custom_resume name="custom_resume",
    # style="display:none". Playwright's set_input_files() works on hidden
    # inputs — we don't need to click the label to expose the file picker.
    "resume_upload": "input#custom_resume[type='file'][name='custom_resume']",
    # CONFIRMED: confirm_availability radio defaults to "yes" (#radio1
    # checked="" in DOM). We re-affirm by explicit click to defend against
    # JS re-renders that drop the default. The "no" radio triggers a textarea
    # we don't want to fill.
    "availability_yes_radio": "input[name='confirm_availability'][value='yes']#radio1",
    # Cover letter — present only when JS var is_cover_letter_visible is
    # truthy. Our flow detects via the textarea selector + skips when
    # absent.
    "cover_letter": "textarea[name='cover_letter']",
    # Custom questions live inside .questions-container. Empty container
    # means this opp has no custom Qs. When present, each question is
    # typically a textarea / select / radio set keyed by question id.
    "custom_q_container": ".questions-container",
    # CONFIRMED 2026-05-29:
    # `<input type="submit" name="submit" id="submit" class="btn btn-large" value="Submit">`
    "submit_button": "input#submit[type='submit'][name='submit']",
    # Modal close button — we never click it; capture so dry-run flow can
    # detect the cancel-confirm dialog (#easy_apply_modal_close_confirm)
    # if a UI race causes Internshala to ask "Are you sure?".
    "_modal_close_button": "#easy_apply_modal_close",
    "_modal_close_confirm": "#easy_apply_modal_close_confirm",
    # Post-submit confirmation. UNVERIFIED — Internshala likely shows a
    # toast or transitions modal content. Patch on first real submit miss.
    "success_banner": (
        ".success_message, .toast-success, "
        ".application_success_message, "
        "h1:has-text('successfully applied'), "
        "h2:has-text('Application sent')"
    ),
    "error_banner": (".toast-error, .application_error_message, .form-error:visible, .help-block.form-error:visible"),
}


@dataclass
class BrowserApplyResult:
    """Published onto stream:apply_browser_result by the worker."""

    status: str  # 'ok' | 'failed' | 'dry_run_captured'
    submitted_at: str | None = None
    error: str | None = None
    screenshot_b64: str | None = None


async def _human_pause(min_ms: int = 200, max_ms: int = 800) -> None:
    """Behavioural jitter between actions. Internshala bot-detection looks
    at inter-event timing; uniform 0ms gaps are the easiest tell."""
    await asyncio.sleep(random.uniform(min_ms, max_ms) / 1000.0)


async def _assert_present(page: Any, selector_key: str) -> Any:
    """Wait for selector with a short timeout; raise on miss with a
    useful error so the caller can screenshot + bail."""
    selector = INTERNSHALA_SELECTORS[selector_key]
    try:
        return await page.wait_for_selector(selector, timeout=10_000)
    except Exception as e:
        raise RuntimeError(f"selector_miss: {selector_key} ({selector})") from e


async def _screenshot_b64(page: Any) -> str:
    """Full-page PNG, base64-encoded. Used for dry-run + failure capture."""
    png = await page.screenshot(full_page=True, type="png")
    return base64.b64encode(png).decode("ascii")


async def _fill_form(page: Any, task: dict[str, Any], pdf_path: Path) -> None:
    """Fill the AJAX-loaded Internshala application form.

    Real-DOM order (2026-05-29 recon):
      1. Confirm availability — `#radio1` (value="yes") is pre-checked
         by Internshala, but JS re-renders sometimes drop the default.
         Explicit click defends.
      2. Custom questions — `.questions-container` may be empty (this
         opp had zero). When non-empty, each <textarea> answered from
         `qa_defaults` in DOM order.
      3. Cover letter — present only when JS `is_cover_letter_visible`
         is truthy. Optional path; silently skipped if absent.
      4. Custom resume — `input#custom_resume[type=file]`. Required even
         though Internshala has a prefilled fallback — we want OUR
         tailored PDF, not the generic one.
    """
    # 1. Availability — pre-checked, re-affirm.
    try:
        avail = await page.wait_for_selector(INTERNSHALA_SELECTORS["availability_yes_radio"], timeout=3_000)
        if avail is not None:
            await avail.check()
            await _human_pause(200, 500)
    except Exception as e:
        _log.warning("internshala_availability_select_failed", err=str(e))

    # 2. Custom questions — best effort. Container often empty for short opps.
    qa_defaults = task.get("qa_defaults") or {}
    if qa_defaults:
        try:
            container = await page.query_selector(INTERNSHALA_SELECTORS["custom_q_container"])
        except Exception:
            container = None
        if container is not None:
            textareas = await container.query_selector_all("textarea")
            keys = sorted(qa_defaults.keys())
            for textarea, key in zip(textareas, keys, strict=False):
                try:
                    await textarea.fill(str(qa_defaults[key]))
                    await _human_pause(150, 400)
                except Exception as e:
                    _log.warning("internshala_custom_q_fill_failed", err=str(e), key=key)

    # 3. Cover letter — optional. _assert_present would raise; use a short
    # wait_for_selector with try/except so absent textarea is fine.
    cover_md = str(task.get("cover_letter_md") or "")
    if cover_md:
        try:
            cover = await page.wait_for_selector(INTERNSHALA_SELECTORS["cover_letter"], timeout=1_500)
        except Exception:
            cover = None
        if cover is not None:
            cover_plain = cover_md.replace("**", "").replace("__", "").replace("*", "").replace("_", "")
            await cover.fill(cover_plain)
            await _human_pause(300, 700)

    # 4. Upload custom resume — hidden file input, Playwright handles it.
    upload_input = await _assert_present(page, "resume_upload")
    await upload_input.set_input_files(str(pdf_path))
    await _human_pause(400, 900)


async def run_internshala_apply(
    page: Any,
    task: dict[str, Any],
    pdf_path: Path,
) -> BrowserApplyResult:
    """Drive one Easy Apply from a fresh page to either dry-run capture
    or genuine submit. Returns a BrowserApplyResult the worker publishes
    onto stream:apply_browser_result."""
    apply_url = task.get("apply_url")
    if not apply_url:
        return BrowserApplyResult(status="failed", error="task missing apply_url")
    dry_run = bool(task.get("dry_run", False))

    try:
        await page.goto(apply_url, wait_until="networkidle", timeout=30_000)
        # Behavioral humanization — mouse moves + scroll AFTER navigate +
        # BEFORE any click. Mimics a human reading the page before
        # deciding to apply. Camoufox handles fingerprint at C++ level
        # (canvas hash, WebGL, navigator props); this closes the gap on
        # the BEHAVIORAL signal Internshala bot detectors look at
        # (mouse movement velocity profile, scroll patterns, dwell time).
        await humanize_page(page)
        await _human_pause(500, 1200)

        # Early bail: applications-closed banner. Internshala removes the
        # apply CTA when applications are closed; without this check the
        # selector_miss further down throws on `easy_apply_button` and
        # the result worker bounces state back to `queued` — which the
        # cron would immediately re-fire on the next pass and burn the
        # daily cap on a dead opp. status='closed' instead so the result
        # worker can transition state to `expired` once and for all.
        # page.inner_text("body") substring check. The orange banner has
        # no stable class anchor across Internshala redesigns, and
        # Playwright's `text=` selector engine cannot be comma-chained
        # with CSS. Substring on the body text is the most stable
        # detection — Internshala used the exact phrase "Applications
        # are closed for this internship" in both 2026-05-29 screenshots.
        try:
            page_text = await page.inner_text("body")
        except Exception:
            page_text = ""
        marker = INTERNSHALA_SELECTORS["_closed_banner_text_marker"].lower()
        if marker in page_text.lower():
            shot = await _screenshot_b64(page)
            _log.info("internshala_application_closed", task_id=task.get("task_id"))
            return BrowserApplyResult(
                status="closed",
                error="applications closed for this internship",
                screenshot_b64=shot,
            )

        # Click the Easy Apply CTA. Internshala has a global JS handler
        # that opens #easy_apply_modal when this DIV is clicked. The
        # modal exists in DOM at page load (display:none) but the form
        # body is AJAX-injected only after the click.
        easy = await _assert_present(page, "easy_apply_button")
        # Scroll the CTA into view before clicking — mimics human eye-track
        # to the button. Playwright's element.click() does this internally
        # but adding explicit scroll + dwell makes the timing profile
        # more human.
        try:
            await easy.scroll_into_view_if_needed(timeout=2_000)
        except Exception:
            pass
        await _human_pause(300, 700)
        await easy.click()
        await _human_pause(600, 1200)
        await _assert_present(page, "modal")
        # Form fields are AJAX-injected AFTER the modal opens. Without
        # this wait, _fill_form runs against an empty modal body and
        # every input selector misses.
        await _assert_present(page, "form_container")
        await _human_pause(300, 700)

        await _fill_form(page, task, pdf_path)

        if dry_run:
            shot = await _screenshot_b64(page)
            _log.info("internshala_dry_run_captured", task_id=task.get("task_id"))
            return BrowserApplyResult(
                status="dry_run_captured",
                submitted_at=None,
                screenshot_b64=shot,
            )

        submit = await _assert_present(page, "submit_button")
        await submit.click()

        # Wait for success/error banner — whichever fires first wins.
        success_sel = INTERNSHALA_SELECTORS["success_banner"]
        error_sel = INTERNSHALA_SELECTORS["error_banner"]
        race_sel = f"{success_sel}, {error_sel}"
        try:
            elem = await page.wait_for_selector(race_sel, timeout=15_000)
        except Exception as e:
            shot = await _screenshot_b64(page)
            return BrowserApplyResult(
                status="failed",
                error=f"no submit confirmation: {e}",
                screenshot_b64=shot,
            )

        # Detect which one matched. If the element matches the error
        # banner selector specifically, surface its text in the error.
        text = (await elem.inner_text()).strip() if elem else ""
        shot = await _screenshot_b64(page)
        # The simplest discriminator: success banner texts contain
        # "successfully" or "applied"; error banners say "failed" or
        # "could not". Fall back on selector inspection.
        if "success" in text.lower() or "applied" in text.lower():
            return BrowserApplyResult(
                status="ok",
                submitted_at=datetime.now(UTC).isoformat(),
                screenshot_b64=shot,
            )
        return BrowserApplyResult(
            status="failed",
            error=f"submit banner says: {text or '(empty)'}",
            screenshot_b64=shot,
        )
    except Exception as e:
        # Worst case: screenshot the broken page so the user can see what
        # Internshala rendered, then surface the error.
        try:
            shot = await _screenshot_b64(page)
        except Exception:
            shot = None
        _log.exception("internshala_apply_unhandled", task_id=task.get("task_id"), err=str(e))
        return BrowserApplyResult(status="failed", error=str(e), screenshot_b64=shot)


__all__ = [
    "INTERNSHALA_SELECTORS",
    "INTERNSHALA_SELECTORS_VERSION",
    "BrowserApplyResult",
    "run_internshala_apply",
]

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

_log = get_logger(__name__)

INTERNSHALA_SELECTORS_VERSION = "2026.05.28-recon-pending"

# Selector map — keys are stable internal names referenced by run_internshala_apply.
# Values are the live CSS/XPath selectors. Recon the actual DOM on the spare
# and patch these. Do NOT inline selectors in the run_*_apply body — the
# whole point of the map is that drift is a one-line fix.
INTERNSHALA_SELECTORS: dict[str, str] = {
    # Easy Apply entry. Internshala renders this as a Bootstrap-style button
    # with `cta_apply_button` id on detail pages; some flows surface a
    # plain anchor with `data-action="apply"` instead. Try both.
    "easy_apply_button": "#cta_apply_button, [data-action='apply'], button:has-text('Easy Apply')",
    # Modal container — wait_for_selector before filling fields.
    "modal": "#apply_now_dialog, [role='dialog']:has-text('Apply')",
    # Resume upload — `input[type=file]` inside the modal. Internshala has
    # ALSO supported a "use existing resume" radio set; PDF upload path is
    # the auto-apply flow.
    "resume_upload": "input[type='file'][name='resume'], input[type='file'][accept*='pdf']",
    # Cover letter textarea — `name=cover_letter`.
    "cover_letter": "textarea[name='cover_letter'], textarea#cover_letter",
    # Custom-question textareas — Internshala calls these "additional
    # questions". The DOM uses `name="question_<id>"` per textarea. The
    # qa_defaults dict in the task carries a {question_id_or_text: answer}
    # map; we match by hashing the question label, NOT by index.
    "custom_q_container": ".additional_questions, .questions_container",
    # Submit button. Avoid generic "button:has-text('Submit')" — the modal
    # has a separate "Save Draft" button.
    "submit_button": "button#submit_application, button:has-text('Submit application')",
    # Post-submit confirmation. Internshala shows a toast with the
    # message "You have successfully applied" or a redirect to a "thanks"
    # page; we wait for whichever fires first.
    "success_banner": ".toast-success, .application_success_message, h1:has-text('successfully applied')",
    # Error banner — visible when the submit fails (e.g. account banned,
    # internship closed).
    "error_banner": ".toast-error, .application_error_message",
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
    """Upload PDF, fill cover letter, answer custom questions."""
    upload_input = await _assert_present(page, "resume_upload")
    await upload_input.set_input_files(str(pdf_path))
    await _human_pause(400, 900)

    cover = await _assert_present(page, "cover_letter")
    cover_md = str(task.get("cover_letter_md") or "")
    # Internshala's textarea accepts plain text; strip markdown emphasis.
    cover_plain = cover_md.replace("**", "").replace("__", "").replace("*", "").replace("_", "")
    await cover.fill(cover_plain)
    await _human_pause(300, 700)

    # Custom Q&A — best effort. Skip silently when no container is rendered
    # (some Internshala opps have zero custom questions, and the container
    # selector misses without raising).
    qa_defaults = task.get("qa_defaults") or {}
    if qa_defaults:
        try:
            container = await page.query_selector(INTERNSHALA_SELECTORS["custom_q_container"])
        except Exception:
            container = None
        if container is not None:
            # Inside the container, every <textarea> is a custom question.
            # Walk them in DOM order and pair with qa_defaults keys
            # alphabetically. NOT robust — recon should replace this with a
            # label-text-hash match. Phase 1 keeps it simple.
            textareas = await container.query_selector_all("textarea")
            keys = sorted(qa_defaults.keys())
            for textarea, key in zip(textareas, keys, strict=False):
                try:
                    await textarea.fill(str(qa_defaults[key]))
                    await _human_pause(150, 400)
                except Exception as e:
                    _log.warning("internshala_custom_q_fill_failed", err=str(e), key=key)


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
        await _human_pause(500, 1200)

        # Click Easy Apply. On some flows the button is a direct apply (no
        # modal) — in that case the modal selector miss is the signal and
        # the submitter bails so the user can patch the flow.
        easy = await _assert_present(page, "easy_apply_button")
        await easy.click()
        await _human_pause(400, 900)
        await _assert_present(page, "modal")

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

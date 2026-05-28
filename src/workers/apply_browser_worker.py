"""Sidecar worker — consumes Streams.APPLY_BROWSER, drives camoufox.

Runs on the spare Pop OS 24.04 desktop (NOT on the Pi). Connects to the
Pi's Redis + Postgres via the autossh tunnel terminating on the spare's
127.0.0.1 (see docs/runbooks/sidecar_setup.md §9).

Pipeline per task:
  1. Lease a healthy `identities` row for the task's platform.
  2. Decrypt cookies + UA locally with the libsodium master key (in env).
  3. Decode the base64 PDF to tmpfs `/tmp/apply/<task_id>.pdf`.
  4. Spin a fresh camoufox context with the identity's cookies + UA.
  5. Hand off to the platform-specific submitter
     (e.g. `src/application/submitters/internshala_browser.run`).
  6. Publish the `BrowserApplyResult` onto stream:apply_browser_result.
  7. Release the identity lease.
  8. Wipe `/tmp/apply/<task_id>.pdf`.

If any step before browser-launch fails (identity lease, PDF decode, etc.)
we publish a `status='failed'` result so the Pi-side apply-result-worker
can roll the application back to `queued` and post the failure to Discord.
The message is ACKed only AFTER the result is published so a sidecar
crash mid-task leaves the message claimable by XAUTOCLAIM (5 min idle).

Hard rules:
  - Single replica per spare. Internshala's cookie-rotation defence trips
    on concurrent submits against the same account; the compose file
    pins this service to one instance (no `deploy.replicas`).
  - PDFs land on tmpfs only — never the spare's persistent disk.
  - Dry-run mode (task.dry_run=True) STOPS before clicking Submit.
  - On selector miss or unexpected DOM we screenshot the page and
    publish status='failed' with the screenshot embedded so the user
    can diagnose without SSH'ing into the spare.
"""

from __future__ import annotations

import asyncio
import base64
import os
import signal
import socket
from pathlib import Path
from typing import Any

from camoufox.async_api import AsyncCamoufox

from src.application.submitters.internshala_browser import (
    INTERNSHALA_SELECTORS_VERSION,
    BrowserApplyResult,
    run_internshala_apply,
)
from src.common import identity_vault
from src.common.db import close_pool, init_pool
from src.common.logger import configure_logging, get_logger
from src.common.metrics import auto_apply_browser_results_total
from src.common.queue import Groups, RedisQ, Streams

configure_logging("apply_browser_worker")
_log = get_logger(__name__)

_TMPFS_ROOT = Path("/tmp/apply")


async def _decode_pdf(task: dict[str, Any]) -> Path:
    """Decode base64 PDF to tmpfs. Caller deletes when done."""
    _TMPFS_ROOT.mkdir(parents=True, exist_ok=True)
    pdf_b64 = task.get("pdf_b64")
    if not pdf_b64:
        raise ValueError("BrowserApplyTask missing pdf_b64")
    raw = base64.b64decode(pdf_b64)
    pdf_path = _TMPFS_ROOT / f"{task['task_id']}.pdf"
    pdf_path.write_bytes(raw)
    return pdf_path


async def _cookies_to_playwright(cookies: dict[str, str], url: str) -> list[dict[str, str | bool]]:
    """Minimal cookies adapter — reuses the same shape as
    `src/fetchers/browser/camoufox.py:_cookies_to_playwright`. Inlined to
    avoid pulling the whole fetcher tier into the apply image."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or ""
    base = f"{parsed.scheme}://{host}" if host else url
    return [{"name": k, "value": v, "url": base} for k, v in cookies.items()]


async def _publish_result(
    queue: RedisQ,
    task: dict[str, Any],
    result: BrowserApplyResult,
) -> None:
    """Push result onto Streams.APPLY_BROWSER_RESULT."""
    payload: dict[str, Any] = {
        "task_id": task["task_id"],
        "opportunity_id": task.get("opportunity_id"),
        "user_id": task.get("user_id"),
        "platform": task.get("platform"),
        "status": result.status,
        "submitted_at": result.submitted_at,
        "error": result.error,
        "screenshot_b64": result.screenshot_b64,
        "selectors_version": INTERNSHALA_SELECTORS_VERSION,
        "thread_title": task.get("thread_title"),
        "apply_url": task.get("apply_url"),
        "dry_run": task.get("dry_run", False),
    }
    await queue.publish(Streams.APPLY_BROWSER_RESULT, payload)
    auto_apply_browser_results_total.labels(
        platform=str(task.get("platform") or "unknown"),
        status=result.status,
    ).inc()


async def _process_task(queue: RedisQ, task: dict[str, Any], *, worker_id: str) -> None:
    """End-to-end task handler. Always publishes a result (success or fail)."""
    task_id = task.get("task_id") or "?"
    platform = task.get("platform") or "unknown"
    apply_url = task.get("apply_url") or ""

    _log.info(
        "apply_browser_task_received",
        task_id=task_id,
        platform=platform,
        dry_run=task.get("dry_run"),
    )

    pdf_path: Path | None = None
    lease = None
    try:
        try:
            pdf_path = await _decode_pdf(task)
        except Exception as e:
            _log.warning("apply_browser_pdf_decode_failed", task_id=task_id, err=str(e))
            await _publish_result(
                queue,
                task,
                BrowserApplyResult(status="failed", error=f"pdf decode: {e}"),
            )
            return

        lease = await identity_vault.checkout(platform=platform, worker_id=worker_id, lease_seconds=600)
        if lease is None:
            _log.warning("apply_browser_no_identity", task_id=task_id, platform=platform)
            await _publish_result(
                queue,
                task,
                BrowserApplyResult(status="failed", error=f"no healthy identity for platform={platform}"),
            )
            return

        # Spin camoufox, build a fresh context with the leased identity.
        async with AsyncCamoufox() as browser:
            context_kwargs: dict[str, str] = {}
            if lease.ua_string:
                context_kwargs["user_agent"] = lease.ua_string
            context = await browser.new_context(**context_kwargs)
            try:
                if lease.cookies:
                    await context.add_cookies(await _cookies_to_playwright(lease.cookies, apply_url))
                page = await context.new_page()
                try:
                    if platform == "internshala":
                        result = await run_internshala_apply(page, task, pdf_path)
                    else:
                        result = BrowserApplyResult(
                            status="failed",
                            error=f"no submitter wired for platform={platform}",
                        )
                finally:
                    await page.close()
            finally:
                await context.close()

        await _publish_result(queue, task, result)
    except Exception as e:
        _log.exception("apply_browser_task_unhandled", task_id=task_id, err=str(e))
        try:
            await _publish_result(
                queue,
                task,
                BrowserApplyResult(status="failed", error=f"unhandled: {e}"),
            )
        except Exception as inner:
            _log.exception("apply_browser_result_publish_failed", task_id=task_id, err=str(inner))
    finally:
        if lease is not None:
            try:
                await identity_vault.release(lease.lease_id)
            except Exception as e:
                _log.warning("apply_browser_identity_release_failed", err=str(e), lease_id=lease.lease_id)
        if pdf_path is not None:
            try:
                pdf_path.unlink(missing_ok=True)
            except Exception as e:
                _log.warning("apply_browser_pdf_unlink_failed", err=str(e), path=str(pdf_path))


async def _consume_loop(queue: RedisQ, worker_id: str, stop: asyncio.Event) -> None:
    """Loop until stop is set. Acks AFTER result publish so a crash mid-task
    leaves the message for XAUTOCLAIM."""
    async for msg in queue.consume(Streams.APPLY_BROWSER, Groups.BROWSER_APPLIERS):
        if stop.is_set():
            break
        task = dict(msg.fields)
        try:
            await _process_task(queue, task, worker_id=worker_id)
        finally:
            try:
                await queue.ack(Streams.APPLY_BROWSER, Groups.BROWSER_APPLIERS, msg.msg_id)
            except Exception as e:
                _log.warning("apply_browser_ack_failed", msg_id=msg.msg_id, err=str(e))


async def main() -> None:
    await init_pool()
    queue = await RedisQ.connect()
    worker_id = f"apply-browser-{socket.gethostname()}-{os.getpid()}"

    stop = asyncio.Event()

    def _stop(*_a: object) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop)

    _log.info("apply_browser_worker_ready", worker_id=worker_id)
    try:
        await _consume_loop(queue, worker_id, stop)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

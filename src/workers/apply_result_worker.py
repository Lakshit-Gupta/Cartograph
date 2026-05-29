"""Pi-side consumer of Streams.APPLY_BROWSER_RESULT.

The sidecar (ThinkPad apply-browser-worker) publishes one BrowserApplyResult
per task. This worker:

  1. Loads the matching `applications` row (by `opp_id` since the result
     carries `opportunity_id`, not application_id — the sidecar doesn't
     know it; the Pi-side submitter created the row before publishing
     the browser task).
  2. UPDATEs `applications.payload` with the result blob:
     `browser_status`, `submitted_at`, `selectors_version`, `error`.
  3. Sets `applications.response_status`:
       ok                 → 'auto_apply_dispatched'
       dry_run_captured   → 'auto_apply_dry_run'
       failed             → 'auto_apply_failed' (+ rolls opp state
                              back to 'queued' so the user can try
                              manual fallback via /apply later).
  4. Publishes one of three notify kinds onto Streams.NOTIFY for the
     Discord bot to surface:
       auto_applied       (status=ok)
       auto_apply_dry_run (status=dry_run_captured)
       auto_apply_failed  (status=failed)

Screenshots ride in the notify payload as base64; the Discord handler
decodes + sends as discord.File so the user sees the captured page.
"""

from __future__ import annotations

import asyncio
import json
import signal
from typing import Any

from src.common.db import acquire, close_pool, init_pool
from src.common.logger import configure_logging, get_logger
from src.common.queue import Groups, RedisQ, Streams

configure_logging("apply_result_worker")
_log = get_logger(__name__)


_NOTIFY_KIND_BY_STATUS = {
    "ok": "auto_applied",
    "dry_run_captured": "auto_apply_dry_run",
    "failed": "auto_apply_failed",
    # `closed` = Internshala detected the listing is no longer accepting
    # applications. Result-worker transitions opp to `expired` so the
    # cron never re-fires on it. Notifier surfaces with the same colour
    # bucket as `failed` (red) but distinct subject line.
    "closed": "auto_apply_failed",
}


async def _persist_result(result: dict[str, Any]) -> int | None:
    """Update applications + opportunities to reflect the sidecar result.

    Returns the application_id (or None if no matching row found, which
    means the Pi-side submitter never inserted — likely a stale stream
    entry from a previous schema). Best-effort: DB errors logged but not
    raised."""
    opp_id = result.get("opportunity_id")
    if not opp_id:
        _log.warning("apply_result_missing_opp_id", task_id=result.get("task_id"))
        return None
    status = result.get("status") or "failed"
    response_status = {
        "ok": "auto_apply_dispatched",
        "dry_run_captured": "auto_apply_dry_run",
        "failed": "auto_apply_failed",
        "closed": "auto_apply_closed",
    }.get(status, "auto_apply_failed")

    payload_update = {
        "browser_status": status,
        "browser_submitted_at": result.get("submitted_at"),
        "browser_error": result.get("error"),
        "selectors_version": result.get("selectors_version"),
        "task_id": result.get("task_id"),
        "dry_run": result.get("dry_run", False),
    }

    try:
        async with acquire() as conn, conn.transaction():
            rec = await conn.fetchrow(
                """
                UPDATE applications
                SET payload = COALESCE(payload, '{}'::jsonb) || $2::jsonb,
                    response_status = $3,
                    response_at = NOW()
                WHERE opportunity_id = $1
                ORDER BY id DESC
                LIMIT 1
                RETURNING id
                """,
                opp_id,
                json.dumps(payload_update),
                response_status,
            )
            application_id = int(rec["id"]) if rec else None

            if status == "failed":
                # Roll opp state back so /apply can be retried via the
                # manual fallback. opportunity_transitions audits the
                # reversal so we don't silently lose history.
                await conn.execute(
                    "UPDATE opportunities SET state='queued' WHERE id=$1 AND state='applied'",
                    opp_id,
                )
                await conn.execute(
                    """
                    INSERT INTO opportunity_transitions
                        (opportunity_id, from_state, to_state, trigger, metadata)
                    VALUES ($1, 'applied', 'queued', 'auto_apply_failed', $2::jsonb)
                    """,
                    opp_id,
                    json.dumps({"task_id": result.get("task_id"), "error": result.get("error")}),
                )
            elif status == "closed":
                # Internshala marked the listing closed. Transition to
                # `expired` and audit — the cron will never re-fire on
                # `expired` state, so this opp is locked out for good.
                await conn.execute(
                    "UPDATE opportunities SET state='expired' WHERE id=$1 AND state IN ('applied','queued','ranked','digested','seen')",
                    opp_id,
                )
                await conn.execute(
                    """
                    INSERT INTO opportunity_transitions
                        (opportunity_id, from_state, to_state, trigger, metadata)
                    VALUES ($1, 'applied', 'expired', 'auto_apply_closed', $2::jsonb)
                    """,
                    opp_id,
                    json.dumps({"task_id": result.get("task_id"), "error": result.get("error")}),
                )
    except Exception as e:
        _log.exception("apply_result_db_update_failed", err=str(e), opp_id=opp_id)
        return None
    return application_id


async def _publish_notify(queue: RedisQ, result: dict[str, Any], application_id: int | None) -> None:
    """Emit one Streams.NOTIFY entry for the Discord bot to surface."""
    status = result.get("status") or "failed"
    notify_kind = _NOTIFY_KIND_BY_STATUS.get(status, "auto_apply_failed")
    payload = {
        "kind": notify_kind,
        "user_id": result.get("user_id") or 1,
        "payload": {
            "application_id": application_id,
            "opportunity_id": result.get("opportunity_id"),
            "platform": result.get("platform"),
            "task_id": result.get("task_id"),
            "thread_title": result.get("thread_title"),
            "apply_url": result.get("apply_url"),
            "browser_status": status,
            "browser_error": result.get("error"),
            "submitted_at": result.get("submitted_at"),
            "selectors_version": result.get("selectors_version"),
            "screenshot_b64": result.get("screenshot_b64"),
            "dry_run": bool(result.get("dry_run", False)),
        },
    }
    await queue.publish(Streams.NOTIFY, payload)


async def _process(queue: RedisQ, result: dict[str, Any]) -> None:
    application_id = await _persist_result(result)
    try:
        await _publish_notify(queue, result, application_id)
    except Exception as e:
        _log.exception(
            "apply_result_notify_publish_failed",
            err=str(e),
            task_id=result.get("task_id"),
        )


async def _consume_loop(queue: RedisQ, stop: asyncio.Event) -> None:
    async for msg in queue.consume(Streams.APPLY_BROWSER_RESULT, Groups.APPLY_RESULTS):
        if stop.is_set():
            break
        try:
            await _process(queue, dict(msg.fields))
        finally:
            try:
                await queue.ack(Streams.APPLY_BROWSER_RESULT, Groups.APPLY_RESULTS, msg.msg_id)
            except Exception as e:
                _log.warning("apply_result_ack_failed", msg_id=msg.msg_id, err=str(e))


async def main() -> None:
    await init_pool()
    queue = await RedisQ.connect()
    stop = asyncio.Event()

    def _stop(*_a: object) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop)

    _log.info("apply_result_worker_ready")
    try:
        await _consume_loop(queue, stop)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

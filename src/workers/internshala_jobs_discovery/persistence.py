"""DB + notify IO for the jobs discovery worker.

A thin jobs-bound wrapper: reuses the internship persistence's source-agnostic
SQL (`_INSERT_CYCLE_LOG` / `_CYCLE_LOG_COLUMNS` — `source_slug` is a row value,
not baked into the SQL) and `publish_notify`, but resolves + reports against the
jobs `SOURCE_SLUG`. Keeps the internship persistence module untouched.
"""

from __future__ import annotations

from src.common.db import acquire
from src.common.logger import get_logger
from src.common.metrics import discovery_cycle_failures_total
from src.common.queue import RedisQ
from src.workers.internshala_discovery.persistence import (
    _CYCLE_LOG_COLUMNS,
    _INSERT_CYCLE_LOG,
    publish_notify,
)
from src.workers.internshala_discovery.report import (
    DiscoveryCycleReport,
    build_cycle_report_payload,
)
from src.workers.internshala_jobs_discovery.config import SOURCE_SLUG

_log = get_logger(__name__)


async def resolve_source_id() -> int:
    """One-shot `SELECT id FROM sources WHERE slug='in_internshala_jobs'`."""
    async with acquire() as conn:
        rec = await conn.fetchrow("SELECT id FROM sources WHERE slug = $1", SOURCE_SLUG)
    if rec is None:
        raise SystemExit(f"source slug {SOURCE_SLUG!r} not found in sources table — run migrations first")
    return int(rec["id"])


async def persist_cycle_report(q: RedisQ, report: DiscoveryCycleReport) -> None:
    """INSERT into `discovery_cycle_log` then publish the cycle-report card.

    Best-effort DB insert: a failure logs + emits a notify alert, but the report
    is still posted to Discord so the operator sees the cycle outcome.
    """
    row = report.to_row()
    try:
        async with acquire() as conn:
            await conn.execute(_INSERT_CYCLE_LOG, *(row[c] for c in _CYCLE_LOG_COLUMNS))
    except Exception as exc:
        _log.warning("jobs_cycle_log_insert_failed", cycle_id=report.cycle_id, err=str(exc))
        await publish_notify(
            q,
            {
                "kind": "discovery_cycle_persist_failed",
                "source_slug": SOURCE_SLUG,
                "cycle_id": report.cycle_id,
                "error": str(exc),
            },
        )
    await publish_notify(q, build_cycle_report_payload(report))


async def emit_cycle_failure_alert(q: RedisQ, exc: Exception, *, error_streak: int) -> None:
    """Publish a failure alert when a whole jobs cycle raises."""
    discovery_cycle_failures_total.inc()
    _log.error("jobs_cycle_failed", err=str(exc), error_streak=error_streak)
    await publish_notify(
        q,
        {
            "kind": "discovery_cycle_failure",
            "source_slug": SOURCE_SLUG,
            "summary": f"✗ jobs discovery cycle failed (streak {error_streak}): {exc}",
            "healthy": False,
            "error": str(exc),
        },
    )


__all__ = ["emit_cycle_failure_alert", "persist_cycle_report", "resolve_source_id"]

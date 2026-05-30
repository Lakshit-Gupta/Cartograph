"""DB + notify IO for the discovery worker.

Split out of the entrypoint to keep it under the 300-line ceiling. Holds the
side-effecting report-persistence helpers: the `discovery_cycle_log` INSERT, the
best-effort `stream:notify` publish, the whole-cycle failure alert, and the
one-shot source-id lookup. The pure report dataclass + payload builder live in
`report.py`; this module is where they meet Postgres / Redis.
"""

from __future__ import annotations

from typing import Any

from src.common.db import acquire
from src.common.logger import get_logger
from src.common.metrics import discovery_cycle_failures_total
from src.common.queue import RedisQ, Streams
from src.workers.internshala_discovery.config import SOURCE_SLUG
from src.workers.internshala_discovery.report import (
    DiscoveryCycleReport,
    build_cycle_report_payload,
)

_log = get_logger(__name__)

_INSERT_CYCLE_LOG = """
INSERT INTO discovery_cycle_log (
    cycle_id, worker_id, source_slug, started_at, duration_sec,
    combos_attempted, combos_succeeded, combo_timeouts, selector_misses,
    cards_scraped, cards_published, cards_rejected_subfloor,
    cards_rejected_dedup, cards_rejected_parse, cards_rejected_expired,
    cards_rejected_experience, healthy,
    selectors_version, matrix_version
)
VALUES ($1,$2,$3,$4::timestamptz,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
"""

# Column order MUST match the placeholders in _INSERT_CYCLE_LOG above.
_CYCLE_LOG_COLUMNS = (
    "cycle_id",
    "worker_id",
    "source_slug",
    "started_at",
    "duration_sec",
    "combos_attempted",
    "combos_succeeded",
    "combo_timeouts",
    "selector_misses",
    "cards_scraped",
    "cards_published",
    "cards_rejected_subfloor",
    "cards_rejected_dedup",
    "cards_rejected_parse",
    "cards_rejected_expired",
    "cards_rejected_experience",
    "healthy",
    "selectors_version",
    "matrix_version",
)


async def resolve_source_id() -> int:
    """One-shot `SELECT id FROM sources WHERE slug='in_internshala'`."""
    async with acquire() as conn:
        rec = await conn.fetchrow("SELECT id FROM sources WHERE slug = $1", SOURCE_SLUG)
    if rec is None:
        raise SystemExit(f"source slug {SOURCE_SLUG!r} not found in sources table — run migrations / seed first")
    return int(rec["id"])


async def publish_notify(q: RedisQ, payload: dict[str, Any]) -> None:
    """Best-effort `stream:notify` publish — never crash the loop on Redis OOM."""
    try:
        await q.publish(Streams.NOTIFY, payload)
    except Exception as exc:
        _log.warning("discovery_notify_publish_failed", kind=payload.get("kind"), err=str(exc))


async def persist_cycle_report(q: RedisQ, report: DiscoveryCycleReport) -> None:
    """INSERT the report into `discovery_cycle_log` then publish the cycle-report
    card onto `stream:notify`. The DB insert is best-effort: a failure logs +
    emits a notify alert, but the report is still posted to Discord so the
    operator sees the cycle outcome either way.
    """
    row = report.to_row()
    try:
        async with acquire() as conn:
            await conn.execute(_INSERT_CYCLE_LOG, *(row[c] for c in _CYCLE_LOG_COLUMNS))
    except Exception as exc:
        _log.warning("discovery_cycle_log_insert_failed", cycle_id=report.cycle_id, err=str(exc))
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
    """Publish a failure alert when a whole cycle raises (not a per-combo miss)."""
    discovery_cycle_failures_total.inc()
    _log.error("discovery_cycle_failed", err=str(exc), error_streak=error_streak)
    await publish_notify(
        q,
        {
            "kind": "discovery_cycle_failure",
            "source_slug": SOURCE_SLUG,
            "summary": f"✗ discovery cycle failed (streak {error_streak}): {exc}",
            "healthy": False,
            "error": str(exc),
        },
    )


__all__ = [
    "emit_cycle_failure_alert",
    "persist_cycle_report",
    "publish_notify",
    "resolve_source_id",
]

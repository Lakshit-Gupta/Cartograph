"""Persistence helpers shared by the LaTeX and legacy apply paths.

Pulled out of ``sender.py`` to keep the entry-point module under the
300-line cap. No behaviour changes - this module contains the exact
SQL + tenant resolution the legacy code did, now reachable from both
``sender_latex.pipeline`` and ``sender_legacy.send_with_json_template``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from src.common.db import acquire, current_tenant, fetch_one
from src.common.logger import get_logger
from src.common.types import ApplyMethod, OppState

_log = get_logger(__name__)

_FOLLOWUPS_DDL = """
CREATE TABLE IF NOT EXISTS followups (
    id              BIGSERIAL PRIMARY KEY,
    application_id  BIGINT NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    fire_at         TIMESTAMPTZ NOT NULL,
    fired_at        TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_followups_due
    ON followups (fire_at)
    WHERE fired_at IS NULL;
"""

_DDL_APPLIED = False


async def ensure_followups_table() -> None:
    """Idempotent CREATE TABLE for the followups schedule table."""
    global _DDL_APPLIED
    if _DDL_APPLIED:
        return
    try:
        async with acquire() as conn:
            await conn.execute(_FOLLOWUPS_DDL)
        _DDL_APPLIED = True
    except Exception as e:
        _log.warning("followups_ddl_failed", err=str(e))


async def upsert_application(
    opp_id: UUID,
    method: ApplyMethod,
    payload: dict[str, Any],
    *,
    resume_variant_id: int | None = None,
) -> int:
    """Insert/update the applications row under the current tenant.

    ``resume_variant_id`` (Phase 2.2, V011) is nullable; the COALESCE
    preserves a previously-set variant when a later send omits it.
    """
    rec = await fetch_one(
        """
        INSERT INTO applications (user_id, opportunity_id, method, payload, resume_variant_id)
        VALUES ($5, $1, $2::apply_method_enum, $3::jsonb, $4)
        ON CONFLICT (user_id, opportunity_id) DO UPDATE
            SET sent_at           = NOW(),
                method            = $2::apply_method_enum,
                payload           = $3::jsonb,
                resume_variant_id = COALESCE($4, applications.resume_variant_id)
        RETURNING id
        """,
        opp_id,
        method.value,
        json.dumps(payload, default=str),
        resume_variant_id,
        current_tenant(),
    )
    if rec is None:
        raise RuntimeError("applications insert returned no row")
    return int(rec["id"])


async def transition_to_applied(opp_id: UUID, application_id: int, method: ApplyMethod) -> None:
    """Move opp.state -> 'applied' and record one transition row."""
    async with acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT state FROM opportunities WHERE id = $1 FOR UPDATE",
            opp_id,
        )
        if row is None:
            return
        from_state = row["state"]
        if from_state == OppState.APPLIED.value:
            return
        await conn.execute(
            "UPDATE opportunities SET state = $1::opp_state_enum, last_seen = NOW() WHERE id = $2",
            OppState.APPLIED.value,
            opp_id,
        )
        await conn.execute(
            """
            INSERT INTO opportunity_transitions
                (opportunity_id, from_state, to_state, trigger, metadata)
            VALUES ($1, $2::opp_state_enum, $3::opp_state_enum, 'send_application', $4::jsonb)
            """,
            opp_id,
            from_state,
            OppState.APPLIED.value,
            json.dumps({"application_id": application_id, "method": method.value}),
        )


async def queue_followup(application_id: int, days: int = 4) -> int:
    """Schedule a followup row; scheduler reads ``WHERE fired_at IS NULL AND fire_at <= NOW()``."""
    await ensure_followups_table()
    fire_at = datetime.now(UTC) + timedelta(days=days)
    rec = await fetch_one(
        """
        INSERT INTO followups (application_id, fire_at)
        VALUES ($1, $2)
        RETURNING id
        """,
        application_id,
        fire_at,
    )
    if rec is None:
        raise RuntimeError("followups insert returned no row")
    fid = int(rec["id"])
    _log.info(
        "followup_queued",
        application_id=application_id,
        followup_id=fid,
        fire_at=fire_at.isoformat(),
    )
    return fid


__all__ = [
    "ensure_followups_table",
    "queue_followup",
    "transition_to_applied",
    "upsert_application",
]

"""Resume-compile audit log row builder.

Packs the 11 fields previously taken as keyword arguments by
``_log_compile_outcome`` into a frozen dataclass so the call site stays
compact and future schema additions are a single-field change. Mirrors
the ``FitOutcome`` pattern in ``src/ranker/global_refit.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from src.common.db import acquire
from src.common.logger import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CompileAudit:
    """All audit fields persisted to one ``resume_compile_log`` row.

    Bundled as a dataclass so ``log_compile_outcome`` takes a single
    argument instead of an 11-tuple — keeps callsites compact and makes
    schema additions a single-field change.
    """

    opportunity_id: UUID
    user_id: int
    status: str
    source_hash: str | None = None
    artifact_sha256: str | None = None
    block_overrides: dict[str, list[str]] | None = None
    compile_duration_ms: int | None = None
    tectonic_version: str | None = None
    tectonic_stderr: str | None = None


async def log_compile_outcome(audit: CompileAudit) -> None:
    """Insert one row into ``resume_compile_log``. Best-effort; never raises."""
    try:
        async with acquire() as conn:
            await conn.execute(
                """
                INSERT INTO resume_compile_log
                    (opportunity_id, user_id, source_hash, artifact_sha256,
                     block_overrides, compile_duration_ms, tectonic_version,
                     status, tectonic_stderr)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9)
                """,
                audit.opportunity_id,
                audit.user_id,
                audit.source_hash,
                audit.artifact_sha256,
                json.dumps(audit.block_overrides) if audit.block_overrides is not None else None,
                audit.compile_duration_ms,
                audit.tectonic_version,
                audit.status,
                audit.tectonic_stderr,
            )
    except Exception as e:  # pragma: no cover — best-effort logging
        _log.warning(
            "resume_compile_log_insert_failed",
            err=str(e),
            opp_id=str(audit.opportunity_id),
            status=audit.status,
        )


async def attach_resume_audit_to_application(
    application_id: int,
    *,
    artifact_sha256: str | None,
    source_hash: str | None,
    status: str,
) -> None:
    """Backfill the V007 columns onto an existing ``applications`` row."""
    try:
        async with acquire() as conn:
            await conn.execute(
                """
                UPDATE applications
                   SET resume_artifact_sha256 = $2,
                       resume_source_hash     = $3,
                       resume_compile_status  = $4
                 WHERE id = $1
                """,
                application_id,
                artifact_sha256,
                source_hash,
                status,
            )
    except Exception as e:  # pragma: no cover — best-effort logging
        _log.warning(
            "applications_resume_audit_update_failed",
            err=str(e),
            application_id=application_id,
        )


__all__ = [
    "CompileAudit",
    "attach_resume_audit_to_application",
    "log_compile_outcome",
]

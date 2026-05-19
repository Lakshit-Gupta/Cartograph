"""Hermetic tests for ``src.application.sender_latex.audit``.

Coverage:
  - ``CompileAudit`` dataclass round-trip: fields preserved verbatim.
  - ``log_compile_outcome`` inserts a row into ``resume_compile_log``
    via mocked ``acquire``. SQL params bound in the right order.
  - ``block_overrides`` is JSON-serialised before the bind.
  - DB exception is swallowed (best-effort logging).
  - ``attach_resume_audit_to_application`` updates the V007 columns and
    swallows DB exceptions.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest

from src.application.sender_latex import audit as audit_mod
from src.application.sender_latex.audit import (
    CompileAudit,
    attach_resume_audit_to_application,
    log_compile_outcome,
)

_OPP_ID = UUID("00000000-0000-0000-0000-000000005678")


class _FakeConn:
    def __init__(self, *, raise_on_execute: bool = False):
        self.captured: list[tuple[str, tuple[Any, ...]]] = []
        self._raise = raise_on_execute

    async def execute(self, sql: str, *args: Any) -> None:
        if self._raise:
            raise RuntimeError("db down")
        self.captured.append((sql, args))


class _AsyncCtx:
    def __init__(self, conn: _FakeConn):
        self._c = conn

    async def __aenter__(self):
        return self._c

    async def __aexit__(self, *a: Any, **k: Any) -> bool:
        return False


def _patch_acquire(monkeypatch: pytest.MonkeyPatch, conn: _FakeConn):
    monkeypatch.setattr(audit_mod, "acquire", lambda: _AsyncCtx(conn))


@pytest.mark.smoke
def test_compile_audit_dataclass_holds_all_fields():
    a = CompileAudit(
        opportunity_id=_OPP_ID,
        user_id=1,
        status="tailored",
        source_hash="abc",
        artifact_sha256="def",
        block_overrides={"b1": ["x"]},
        compile_duration_ms=42,
        tectonic_version="tectonic 0.16",
        tectonic_stderr=None,
    )
    assert a.opportunity_id == _OPP_ID
    assert a.user_id == 1
    assert a.status == "tailored"
    assert a.block_overrides == {"b1": ["x"]}


def test_compile_audit_default_fields_are_none():
    a = CompileAudit(opportunity_id=_OPP_ID, user_id=1, status="failed")
    assert a.source_hash is None
    assert a.artifact_sha256 is None
    assert a.block_overrides is None
    assert a.tectonic_stderr is None


@pytest.mark.smoke
async def test_log_compile_outcome_inserts_row_with_expected_params(
    monkeypatch: pytest.MonkeyPatch,
):
    conn = _FakeConn()
    _patch_acquire(monkeypatch, conn)

    audit = CompileAudit(
        opportunity_id=_OPP_ID,
        user_id=1,
        status="tailored",
        source_hash="hash-src",
        artifact_sha256="hash-art",
        block_overrides={"b1": ["bullet"]},
        compile_duration_ms=99,
        tectonic_version="tectonic 0.16",
        tectonic_stderr=None,
    )
    await log_compile_outcome(audit)

    assert len(conn.captured) == 1
    sql, args = conn.captured[0]
    assert "INSERT INTO resume_compile_log" in sql
    # parameter order: opp_id, user_id, source_hash, artifact_sha256,
    #                  block_overrides, duration, version, status, stderr
    assert args[0] == _OPP_ID
    assert args[1] == 1
    assert args[2] == "hash-src"
    assert args[3] == "hash-art"
    assert json.loads(args[4]) == {"b1": ["bullet"]}
    assert args[5] == 99
    assert args[6] == "tectonic 0.16"
    assert args[7] == "tailored"
    assert args[8] is None


async def test_log_compile_outcome_serialises_none_overrides_as_null(
    monkeypatch: pytest.MonkeyPatch,
):
    conn = _FakeConn()
    _patch_acquire(monkeypatch, conn)

    await log_compile_outcome(CompileAudit(opportunity_id=_OPP_ID, user_id=1, status="failed"))
    _sql, args = conn.captured[0]
    assert args[4] is None  # block_overrides → SQL NULL when None


async def test_log_compile_outcome_swallows_db_exception(
    monkeypatch: pytest.MonkeyPatch,
):
    """Audit logging is best-effort; never raises into the apply flow."""
    conn = _FakeConn(raise_on_execute=True)
    _patch_acquire(monkeypatch, conn)

    # Should not raise.
    await log_compile_outcome(CompileAudit(opportunity_id=_OPP_ID, user_id=1, status="failed"))


@pytest.mark.smoke
async def test_attach_resume_audit_updates_v007_columns(
    monkeypatch: pytest.MonkeyPatch,
):
    conn = _FakeConn()
    _patch_acquire(monkeypatch, conn)

    await attach_resume_audit_to_application(7, artifact_sha256="art", source_hash="src", status="tailored")
    sql, args = conn.captured[0]
    assert "UPDATE applications" in sql
    assert "resume_artifact_sha256" in sql
    assert "resume_source_hash" in sql
    assert "resume_compile_status" in sql
    assert args == (7, "art", "src", "tailored")


async def test_attach_resume_audit_swallows_db_exception(
    monkeypatch: pytest.MonkeyPatch,
):
    conn = _FakeConn(raise_on_execute=True)
    _patch_acquire(monkeypatch, conn)
    # Must not raise.
    await attach_resume_audit_to_application(7, artifact_sha256=None, source_hash=None, status="failed")

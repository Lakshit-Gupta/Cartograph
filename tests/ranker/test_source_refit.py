"""Phase 2.4 — source response-rate refit contract tests.

The refit module is DB-backed end-to-end, so the hermetic tests below
stub the asyncpg ``acquire()`` context with a tiny in-memory fake that
returns dataclass-like rows from a fixture list. All sklearn calls run
live — they're cheap and deterministic via ``random_state=0``.

Coverage:
  - Cold start (<50 rows) writes the audit row with ``status='cold_start'``
    and skips the UPDATE.
  - Happy path (3 sources, 200 rows, varying response rates) clamps
    weights into [0.5, 2.0] and gives the high-response source the
    highest weight.
  - Two consecutive runs on identical data produce identical weights.
  - Every run appends one row to ``source_refit_log``.
  - A response landing 31 days after sent_at is labeled 0 by
    ``_label_response`` (window guard).
"""

from __future__ import annotations

import asyncio
import json
import math
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Fake asyncpg pool — captures every query + returns canned rows.
# ---------------------------------------------------------------------------
class _FakeConn:
    """In-memory stand-in for asyncpg.Connection.

    Stores SQL + args on every call; the test inspects ``calls`` to
    assert UPDATE/INSERT semantics without a real DB.
    """

    def __init__(self, training_rows: list[dict[str, Any]]):
        self._training_rows = training_rows
        self.calls: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.inserts: list[dict[str, Any]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append({"op": "fetch", "query": query, "args": args})
        # Only one fetch path in source_refit — the training-data load.
        return list(self._training_rows)

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append({"op": "execute", "query": query, "args": args})
        q = query.strip().upper()
        if q.startswith("UPDATE SOURCES"):
            self.updates.append({"args": args})
        elif q.startswith("INSERT INTO SOURCE_REFIT_LOG"):
            self.inserts.append({"args": args})
        return "OK"

    @asynccontextmanager
    async def transaction(self):
        yield


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self._conn


def _patch_acquire(monkeypatch: pytest.MonkeyPatch, training_rows: list[dict[str, Any]]) -> _FakeConn:
    """Replace ``src.ranker.source_refit.acquire`` with the in-memory fake."""
    fake_conn = _FakeConn(training_rows)

    @asynccontextmanager
    async def fake_acquire():
        yield fake_conn

    monkeypatch.setattr("src.ranker.source_refit.acquire", fake_acquire)
    return fake_conn


# ---------------------------------------------------------------------------
# Fixture builders.
# ---------------------------------------------------------------------------
def _row(
    *,
    application_id: int,
    source_id: int,
    category: str = "fulltime",
    age_days: float = 7.0,
    comp_min: float | None = 50000.0,
    responded: int = 0,
) -> dict[str, Any]:
    """Build one canned training row in the shape ``_load_training_data`` returns."""
    return {
        "application_id": application_id,
        "source_id": source_id,
        "category": category,
        "posted_at_age_days": age_days,
        "comp_min": comp_min,
        "responded": responded,
    }


# ---------------------------------------------------------------------------
# 1. Cold-start gate — under 50 rows ⇒ no UPDATE, status='cold_start'.
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_cold_start_when_fewer_than_50_apps(monkeypatch: pytest.MonkeyPatch) -> None:
    """49 rows must short-circuit to ``cold_start`` and write zero weights."""
    rows = [_row(application_id=i, source_id=(i % 3) + 1, responded=i % 2) for i in range(49)]
    fake = _patch_acquire(monkeypatch, rows)

    from src.ranker.source_refit import run_weekly_refit

    out = asyncio.run(run_weekly_refit())
    assert out["status"] == "cold_start"
    assert out["rows_used"] == 49
    assert out["weight_writes"] == 0
    assert fake.updates == [], "no sources UPDATE should fire on cold start"
    # Exactly one audit row, status='cold_start'.
    assert len(fake.inserts) == 1
    insert_args = fake.inserts[0]["args"]
    # signature: (rows_used, positive_rate, auc, summary_json, writes, status, error)
    assert insert_args[0] == 49
    assert insert_args[5] == "cold_start"


# ---------------------------------------------------------------------------
# 2. Happy path — weights live in [0.5, 2.0] and order matches response rate.
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_writes_weights_in_expected_range(monkeypatch: pytest.MonkeyPatch) -> None:
    """200 rows / 3 sources with monotone response rates ⇒ monotone weights.

    Source 1: 80% response rate (best)
    Source 2: 30% response rate
    Source 3: 5%  response rate (worst)
    """
    rows: list[dict[str, Any]] = []
    aid = 0
    for source_id, rate in [(1, 0.8), (2, 0.3), (3, 0.05)]:
        for i in range(70):  # 210 rows total — well over the 50 threshold
            aid += 1
            responded = 1 if i < int(70 * rate) else 0
            rows.append(_row(application_id=aid, source_id=source_id, responded=responded))
    fake = _patch_acquire(monkeypatch, rows)

    from src.ranker.source_refit import run_weekly_refit

    out = asyncio.run(run_weekly_refit())
    assert out["status"] == "ok"
    assert out["rows_used"] == 210
    assert out["weight_writes"] == 3
    weights = out["weights"]
    assert set(weights.keys()) == {1, 2, 3}
    for w in weights.values():
        assert 0.5 <= w <= 2.0, f"weight {w} outside [0.5, 2.0]"
    # Highest response rate ⇒ highest weight.
    assert weights[1] >= weights[2] >= weights[3]
    # And the spread is non-degenerate (best != worst).
    assert weights[1] > weights[3]
    # UPDATE statement fired exactly once.
    assert len(fake.updates) == 1


# ---------------------------------------------------------------------------
# 3. Idempotence — two back-to-back runs give bit-identical weights.
# ---------------------------------------------------------------------------
def test_idempotent_dual_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run twice over the same canned rows; weights must match within 1e-9."""
    rows: list[dict[str, Any]] = []
    aid = 0
    for source_id, rate in [(1, 0.6), (2, 0.2)]:
        for i in range(60):
            aid += 1
            rows.append(
                _row(
                    application_id=aid,
                    source_id=source_id,
                    responded=1 if i < int(60 * rate) else 0,
                )
            )
    _patch_acquire(monkeypatch, rows)

    from src.ranker.source_refit import run_weekly_refit

    out1 = asyncio.run(run_weekly_refit())
    out2 = asyncio.run(run_weekly_refit())
    assert out1["status"] == out2["status"] == "ok"
    for sid in out1["weights"]:
        assert math.isclose(out1["weights"][sid], out2["weights"][sid], abs_tol=1e-9), f"weights drifted across runs for source {sid}"


# ---------------------------------------------------------------------------
# 4. Audit row always appears — even on the happy path.
# ---------------------------------------------------------------------------
def test_logs_run_to_audit_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """One row in ``source_refit_log`` per invocation."""
    rows = [_row(application_id=i, source_id=(i % 2) + 1, responded=i % 3 == 0) for i in range(120)]
    fake = _patch_acquire(monkeypatch, rows)

    from src.ranker.source_refit import run_weekly_refit

    out = asyncio.run(run_weekly_refit())
    assert out["status"] == "ok"
    assert len(fake.inserts) == 1
    args = fake.inserts[0]["args"]
    # Audit row carries (rows_used, positive_rate, auc, summary, writes, status, error).
    assert args[0] == 120
    assert 0.0 <= args[1] <= 1.0  # positive_rate
    # auc may be None if classes degenerate, else float
    assert args[2] is None or 0.0 <= args[2] <= 1.0
    # coefficient_summary serialised as JSON string
    summary = json.loads(args[3])
    assert isinstance(summary, dict)
    assert len(summary) >= 1
    assert args[5] == "ok"


# ---------------------------------------------------------------------------
# 5. Label-window guard — a 31-day-late response counts as 0.
# ---------------------------------------------------------------------------
def test_no_label_leak_outside_window() -> None:
    """The 30-day attribution window must reject late responses."""
    from src.ranker.source_refit import _label_response

    sent_at = datetime(2026, 1, 1, tzinfo=UTC)
    late = sent_at + timedelta(days=31)
    transitions = [
        {
            "application_id": 1,
            "to_state": "interview",
            "occurred_at": late.isoformat(),
        }
    ]
    assert (
        _label_response(
            application_id=1,
            sent_at_iso=sent_at.isoformat(),
            transitions=transitions,
        )
        == 0
    )

    # In-window response is labeled positive.
    early = sent_at + timedelta(days=5)
    transitions[0]["occurred_at"] = early.isoformat()
    assert (
        _label_response(
            application_id=1,
            sent_at_iso=sent_at.isoformat(),
            transitions=transitions,
        )
        == 1
    )

    # Wrong state — ignored even when in-window.
    transitions = [
        {
            "application_id": 1,
            "to_state": "applied",  # not an engagement state
            "occurred_at": early.isoformat(),
        }
    ]
    assert (
        _label_response(
            application_id=1,
            sent_at_iso=sent_at.isoformat(),
            transitions=transitions,
        )
        == 0
    )


# ---------------------------------------------------------------------------
# 6. Bonus — _build_features deterministic column ordering.
# ---------------------------------------------------------------------------
def test_build_features_deterministic_ordering() -> None:
    """Sorted source_ids / categories ⇒ stable feature matrix across runs."""
    from src.ranker.source_refit import TrainingRow, _build_features

    rows = [
        TrainingRow(application_id=1, source_id=3, category="b", posted_at_age_days=1, comp_min=0, responded=0),
        TrainingRow(application_id=2, source_id=1, category="a", posted_at_age_days=2, comp_min=10, responded=1),
        TrainingRow(application_id=3, source_id=2, category="a", posted_at_age_days=3, comp_min=20, responded=0),
    ]
    X1, _, idx1 = _build_features(rows)
    X2, _, idx2 = _build_features(rows)
    assert idx1 == idx2 == [1, 2, 3]
    assert (X1 == X2).all()

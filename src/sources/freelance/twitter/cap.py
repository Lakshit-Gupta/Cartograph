"""Per-handle daily fetch budget (cap = 10/day, rolls over at UTC midnight).

Per CLAUDE.md: 24 handles * 10 polls = 240 fetches/day worst case. The
budget is in-memory only; restarts reset the counter (acceptable — at
most we over-poll a handle by 10 across a restart).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

# Per-handle hard cap per UTC day. 24 handles * 10 = 240 fetches/day worst case.
_PER_HANDLE_DAILY_MAX = 10


class _DailyBudget:
    """Tracks per-handle fetch count per UTC day. Resets at midnight UTC."""

    def __init__(self, cap: int = _PER_HANDLE_DAILY_MAX) -> None:
        self._cap = cap
        self._day: date = datetime.now(UTC).date()
        self._counts: dict[str, int] = {}

    def _rollover_if_needed(self) -> None:
        today = datetime.now(UTC).date()
        if today != self._day:
            self._day = today
            self._counts.clear()

    def allowed(self, handle: str) -> bool:
        self._rollover_if_needed()
        return self._counts.get(handle, 0) < self._cap

    def increment(self, handle: str) -> None:
        self._rollover_if_needed()
        self._counts[handle] = self._counts.get(handle, 0) + 1

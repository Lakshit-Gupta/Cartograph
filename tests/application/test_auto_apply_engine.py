"""Tests for the auto-apply engine's category scoping.

The two lane-scoped Discord commands (`/auto-apply-inter`, `/auto-apply-job`)
pass a `category` into `find_eligible` / `dispatch`; these tests pin that the
category becomes a bound SQL filter and that `dispatch` threads it through.
DB + prefs are faked via monkeypatch (same pattern as test_followup).
"""

from __future__ import annotations

from typing import Any

import pytest

import src.application.auto_apply_engine as engine


@pytest.mark.asyncio
async def test_find_eligible_appends_category_clause(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def _fake_fetch_all(sql: str, *args: Any):
        captured["sql"] = sql
        captured["args"] = args
        return []

    monkeypatch.setattr(engine, "_load_prefs_block", lambda: {"enabled": True})
    monkeypatch.setattr(engine, "fetch_all", _fake_fetch_all)

    await engine.find_eligible(user_id=1, limit=5, category="fulltime")

    assert "o.category = $2" in captured["sql"]
    assert captured["args"] == (1, "fulltime")


@pytest.mark.asyncio
async def test_find_eligible_without_category_binds_only_user(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def _fake_fetch_all(sql: str, *args: Any):
        captured["sql"] = sql
        captured["args"] = args
        return []

    monkeypatch.setattr(engine, "_load_prefs_block", lambda: {"enabled": True})
    monkeypatch.setattr(engine, "fetch_all", _fake_fetch_all)

    await engine.find_eligible(user_id=7, limit=5)

    assert "o.category" not in captured["sql"]
    assert captured["args"] == (7,)


@pytest.mark.asyncio
async def test_dispatch_threads_category_into_find_eligible(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def _fake_find_eligible(*, user_id: int, limit: int, category: str | None = None):
        seen["category"] = category
        return []

    monkeypatch.setattr(engine, "_load_prefs_block", lambda: {"enabled": True, "max_per_day": 10})

    async def _fake_remaining(user_id: int, prefs: dict[str, Any]):
        return (10, 10, 0)

    monkeypatch.setattr(engine, "_remaining_cap", _fake_remaining)
    monkeypatch.setattr(engine, "find_eligible", _fake_find_eligible)

    await engine.dispatch(user_id=1, requested_count=2, category="internship")
    assert seen["category"] == "internship"

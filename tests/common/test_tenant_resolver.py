"""Contract tests for the Phase 4.2 tenant resolver in `src.common.db`.

These tests run hermetically — no DB pool required. They only exercise the
ContextVar shape: default value, `set_tenant`, `with_tenant` scoping +
exception safety, and the backwards-compatible alias.
"""

from __future__ import annotations

import asyncio

import pytest

from src.common import db


def test_current_tenant_defaults_to_founding_owner() -> None:
    # No handler has set the var — default = 1 keeps single-tenant + tests trivial.
    assert db.current_tenant() == 1


def test_set_tenant_pins_for_this_task() -> None:
    db.set_tenant(42)
    try:
        assert db.current_tenant() == 42
    finally:
        db.set_tenant(1)  # cleanup — ContextVar is task-scoped but pytest reuses the task.


def test_with_tenant_restores_on_exit() -> None:
    db.set_tenant(7)
    try:
        with db.with_tenant(99):
            assert db.current_tenant() == 99
        # Restored to the pre-block value, NOT to default.
        assert db.current_tenant() == 7
    finally:
        db.set_tenant(1)


def test_with_tenant_restores_on_exception() -> None:
    db.set_tenant(7)
    try:
        with pytest.raises(RuntimeError), db.with_tenant(99):
            raise RuntimeError("boom")
        assert db.current_tenant() == 7
    finally:
        db.set_tenant(1)


def test_current_user_id_alias_stays_in_sync() -> None:
    """Backwards-compatible alias must read the same value."""
    db.set_tenant(123)
    try:
        assert db.current_user_id() == 123
        assert db.current_user_id() == db.current_tenant()
    finally:
        db.set_tenant(1)


def test_concurrent_tasks_get_isolated_tenants() -> None:
    """ContextVar must be task-scoped — two concurrent tasks each see their own.

    Without ContextVar (e.g. thread-local), the asyncio scheduler running
    tenant A could leak into tenant B mid-await. This test fails loudly if
    that regression sneaks in.
    """

    async def run(tenant_id: int, hold_for: float) -> int:
        db.set_tenant(tenant_id)
        await asyncio.sleep(hold_for)
        return db.current_tenant()

    async def driver() -> tuple[int, int]:
        a, b = await asyncio.gather(run(5, 0.02), run(6, 0.01))
        return a, b

    a, b = asyncio.run(driver())
    # Each task observes its own tenant despite interleaving via await.
    assert (a, b) == (5, 6)

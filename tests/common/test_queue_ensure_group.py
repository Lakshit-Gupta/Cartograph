"""Regression tests for RedisQ.ensure_group OOM tolerance.

Locks in the fix for the camoufox-worker / crawler-worker restart loop:
`XGROUP CREATE ... MKSTREAM` is a write command and is rejected by Redis
under `noeviction` when used_memory >= maxmemory — even if the group
already exists. The fix probes with read-only `XINFO GROUPS` first and
only writes when the group is genuinely absent. Under OOM during the
write path it falls back to bounded retry instead of crashing the worker.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.common.queue import RedisQ


@pytest.mark.asyncio
async def test_ensure_group_skips_write_when_group_exists() -> None:
    """If XINFO GROUPS reports the group, no XGROUP CREATE is issued.

    This is the steady-state path. Pre-fix, every worker boot issued a
    write to MKSTREAM regardless, which is what made the tier restart-spam
    when Redis hit maxmemory.
    """
    client = AsyncMock()
    client.xinfo_groups.return_value = [{"name": "g:crawlers", "consumers": 0}]
    client.xgroup_create.side_effect = AssertionError("should not be called")
    q = RedisQ(client)

    await q.ensure_group("stream:fetch", "g:crawlers")

    client.xinfo_groups.assert_awaited_once_with("stream:fetch")
    client.xgroup_create.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_group_creates_when_absent() -> None:
    client = AsyncMock()
    client.xinfo_groups.return_value = []  # group not present
    q = RedisQ(client)

    await q.ensure_group("stream:fetch", "g:crawlers")

    client.xgroup_create.assert_awaited_once_with("stream:fetch", "g:crawlers", id="$", mkstream=True)


@pytest.mark.asyncio
async def test_ensure_group_creates_when_stream_missing() -> None:
    """`XINFO GROUPS` on a non-existent stream raises `no such key` — we
    must fall through and let MKSTREAM materialise the stream."""
    client = AsyncMock()
    client.xinfo_groups.side_effect = Exception("ERR no such key")
    q = RedisQ(client)

    await q.ensure_group("stream:brand_new", "g:noobs")

    client.xgroup_create.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_group_tolerates_busygroup_race() -> None:
    """Two workers racing to create the same group: loser sees BUSYGROUP
    and must return cleanly (not retry, not raise)."""
    client = AsyncMock()
    client.xinfo_groups.return_value = []
    client.xgroup_create.side_effect = Exception("BUSYGROUP Consumer Group name already exists")
    q = RedisQ(client)

    # Must not raise.
    await q.ensure_group("stream:fetch", "g:crawlers")


@pytest.mark.asyncio
async def test_ensure_group_recovers_when_oom_clears_mid_retry() -> None:
    """If MKSTREAM hits OOM, we re-probe — and if the group has since been
    created by a sibling worker, we exit cleanly without further writes.
    This is the smoking-gun scenario from the camoufox restart loop."""
    client = AsyncMock()
    # First probe: empty. Write fails with OOM. Re-probe inside retry:
    # group now present (e.g. sibling raced ahead OR memory freed).
    client.xinfo_groups.side_effect = [[], [{"name": "g:crawlers"}]]
    client.xgroup_create.side_effect = Exception("OOM command not allowed when used memory > 'maxmemory'.")
    q = RedisQ(client)

    await q.ensure_group("stream:fetch", "g:crawlers")

    # Exactly one create attempt; re-probe found the group; we returned.
    assert client.xgroup_create.await_count == 1


@pytest.mark.asyncio
async def test_dlq_swallows_oom_so_worker_stays_alive() -> None:
    """Second restart-loop trigger: under Redis OOM, the dlq XADD also fails.
    If dlq re-raises, the worker exception handler propagates and the
    process dies → docker restart-loop. dlq MUST be best-effort under OOM
    so the message stays unacked (XAUTOCLAIM reclaims later)."""
    client = AsyncMock()
    client.xadd.side_effect = Exception("OOM command not allowed when used memory > 'maxmemory'.")
    q = RedisQ(client)

    # Must not raise.
    await q.dlq("stream:fetch", "1234-0", {"foo": "bar"}, "extract failed")

    client.xadd.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_group_raises_after_oom_exhausts_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """If OOM persists across all retries AND the group never materialises,
    we raise a clear RuntimeError instead of leaking the raw redis error.
    Supervisor can then alert; worker stays dead intentionally."""

    # Patch sleep so test is fast.
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", _no_sleep)

    client = AsyncMock()
    client.xinfo_groups.return_value = []  # never finds it
    client.xgroup_create.side_effect = Exception("OOM command not allowed when used memory > 'maxmemory'.")
    q = RedisQ(client)

    with pytest.raises(RuntimeError, match="ensure_group"):
        await q.ensure_group("stream:fetch", "g:crawlers")

    # 4 attempts per the loop range(4)
    assert client.xgroup_create.await_count == 4

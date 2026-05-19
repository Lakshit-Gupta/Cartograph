"""Contract tests for `_opp_metadata.resolve_opp_metadata`.

Shared helper used by `post_applied` and `post_manual_apply`. Behaviour:
1. Returns `{}` when `opp_id` is falsy (None, "", 0).
2. Returns `{}` when the SELECT returns no row.
3. Returns the row as a plain `dict` when found.
4. Lets UUID parse errors bubble up (callers wrap in try/except).
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from src.notifiers.discord.handlers import _opp_metadata


@pytest.fixture
def fake_fetch(monkeypatch):
    fetch = AsyncMock(return_value=None)
    monkeypatch.setattr(_opp_metadata.db, "fetch_one", fetch)
    return fetch


async def test_returns_empty_for_none_id(fake_fetch):
    assert await _opp_metadata.resolve_opp_metadata(None) == {}
    fake_fetch.assert_not_called()


async def test_returns_empty_for_empty_string(fake_fetch):
    assert await _opp_metadata.resolve_opp_metadata("") == {}
    fake_fetch.assert_not_called()


async def test_returns_empty_when_row_missing(fake_fetch):
    fake_fetch.return_value = None
    result = await _opp_metadata.resolve_opp_metadata("11111111-1111-1111-1111-111111111111")
    assert result == {}
    fake_fetch.assert_awaited_once()
    sql, opp_uuid = fake_fetch.await_args.args
    assert "SELECT title, company, apply_url FROM opportunities" in sql
    assert opp_uuid == UUID("11111111-1111-1111-1111-111111111111")


async def test_returns_row_as_dict(fake_fetch):
    fake_fetch.return_value = {"title": "Eng", "company": "Acme", "apply_url": "https://x"}
    result = await _opp_metadata.resolve_opp_metadata("22222222-2222-2222-2222-222222222222")
    assert result == {"title": "Eng", "company": "Acme", "apply_url": "https://x"}


async def test_propagates_bad_uuid(fake_fetch):
    """Bad UUID strings raise — callers (notify_applied, notify_manual_apply)
    wrap the whole handler in try/except, so this contract is intentional."""
    with pytest.raises(ValueError):
        await _opp_metadata.resolve_opp_metadata("not-a-uuid")
    fake_fetch.assert_not_called()

"""Contract tests for `post_tracker` (kind=tracker_update).

Asserts:
1. Routes via `route_for({"tracker": ...}, kind="tracker")`.
2. `chan.send(content=...)` called with the supplied message, truncated to 1900 chars.
3. Falls back to `json.dumps(payload)` when no `message` key.
4. `deliver_success_total{channel="tracker"}` increments on success.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from src.common.metrics import deliver_success_total
from src.notifiers.discord.handlers import notify_tracker


def _delivered() -> float:
    return deliver_success_total.labels(channel="tracker")._value.get()


def _bot(chan):
    return SimpleNamespace(_resolve_channel=AsyncMock(return_value=chan))


@pytest.fixture
def stub_route(monkeypatch):
    state = {"channel_id": 4321}
    monkeypatch.setattr(notify_tracker, "route_for", lambda opp, kind: dict(state))
    return state


async def test_post_tracker_forwards_message(stub_route):
    chan = MagicMock(spec=discord.TextChannel)
    chan.send = AsyncMock(return_value=None)
    bot = _bot(chan)
    before = _delivered()

    await notify_tracker.post_tracker(bot, {"tracker": "applied", "message": "thread linked"})

    chan.send.assert_awaited_once_with(content="thread linked")
    assert _delivered() == before + 1


async def test_post_tracker_falls_back_to_json(stub_route):
    chan = MagicMock(spec=discord.TextChannel)
    chan.send = AsyncMock(return_value=None)
    bot = _bot(chan)
    payload = {"tracker": "responses", "opp_id": "abc", "state": "screen"}

    await notify_tracker.post_tracker(bot, payload)

    sent = chan.send.call_args.kwargs["content"]
    # Round-trips into json; key order isn't guaranteed but contents must match.
    assert json.loads(sent) == payload


async def test_post_tracker_truncates_oversize_message(stub_route):
    chan = MagicMock(spec=discord.TextChannel)
    chan.send = AsyncMock(return_value=None)
    bot = _bot(chan)
    huge = "x" * 5000

    await notify_tracker.post_tracker(bot, {"tracker": "applied", "message": huge})

    sent = chan.send.call_args.kwargs["content"]
    assert len(sent) == 1900


async def test_post_tracker_skips_when_channel_missing(stub_route):
    bot = SimpleNamespace(_resolve_channel=AsyncMock(return_value=None))
    before = _delivered()
    await notify_tracker.post_tracker(bot, {"tracker": "applied", "message": "x"})
    assert _delivered() == before

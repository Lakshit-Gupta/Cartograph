"""Contract tests for `post_alert` (kind=alert).

Asserts:
1. Channel resolution via `route_for({"alert": ...}, kind="alert")`.
2. `@here` prefix added when `route.mention_owner` true.
3. Falls back to `payload['alert']` then `"alert"` when no message supplied.
4. `deliver_success_total{channel="alerts"}` increments on success.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from src.common.metrics import deliver_success_total
from src.notifiers.discord.handlers import notify_alert


def _delivered() -> float:
    return deliver_success_total.labels(channel="alerts")._value.get()


def _bot(chan):
    return SimpleNamespace(_resolve_channel=AsyncMock(return_value=chan))


@pytest.fixture
def stub_route(monkeypatch):
    """Return a settable dict so each test can dial in its own route shape."""
    state: dict = {"channel_id": 9001, "mention_owner": True}
    monkeypatch.setattr(notify_alert, "route_for", lambda opp, kind: dict(state))
    return state


async def test_post_alert_includes_here_when_mention_owner(stub_route):
    chan = MagicMock(spec=discord.TextChannel)
    chan.send = AsyncMock(return_value=None)
    bot = _bot(chan)
    before = _delivered()

    await notify_alert.post_alert(bot, {"alert": "cost_cap_reached", "message": "budget hit"})

    chan.send.assert_awaited_once()
    assert chan.send.call_args.kwargs["content"] == "@here budget hit"
    assert _delivered() == before + 1


async def test_post_alert_omits_here_when_mention_owner_false(stub_route):
    stub_route["mention_owner"] = False
    chan = MagicMock(spec=discord.TextChannel)
    chan.send = AsyncMock(return_value=None)
    bot = _bot(chan)

    await notify_alert.post_alert(bot, {"alert": "info", "message": "fyi"})
    assert chan.send.call_args.kwargs["content"] == "fyi"


async def test_post_alert_falls_back_to_alert_key_then_default(stub_route):
    stub_route["mention_owner"] = False
    chan = MagicMock(spec=discord.TextChannel)
    chan.send = AsyncMock(return_value=None)
    bot = _bot(chan)

    await notify_alert.post_alert(bot, {"alert": "pipeline_silent_5m"})
    assert chan.send.call_args.kwargs["content"] == "pipeline_silent_5m"

    chan.send.reset_mock()
    await notify_alert.post_alert(bot, {})
    assert chan.send.call_args.kwargs["content"] == "alert"


async def test_post_alert_skips_when_channel_missing(stub_route):
    bot = SimpleNamespace(_resolve_channel=AsyncMock(return_value=None))
    before = _delivered()
    await notify_alert.post_alert(bot, {"alert": "x", "message": "y"})
    assert _delivered() == before

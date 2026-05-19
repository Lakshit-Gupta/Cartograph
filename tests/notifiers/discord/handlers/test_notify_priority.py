"""Contract tests for `post_priority` (kind=priority_push).

Asserts that the handler:
1. Resolves `channel_id_for("priority_push")`.
2. Builds the priority embed and an `OppActionView`.
3. Calls `chan.send(content=..., embed=..., view=...)` directly (not
   `_send_embed`, since priority push uses content + embed together).
4. Increments `deliver_success_total{channel="priority"}`.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from src.common.metrics import deliver_success_total
from src.notifiers.discord.handlers import notify_priority


def _delivered() -> float:
    return deliver_success_total.labels(channel="priority")._value.get()


def _bot_with_channel(chan):
    return SimpleNamespace(_resolve_channel=AsyncMock(return_value=chan))


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(notify_priority, "channel_id_for", lambda name: 777 if name == "priority_push" else None)
    fake_embed = MagicMock(spec=discord.Embed)
    monkeypatch.setattr(notify_priority, "build_priority_push", lambda *a, **kw: fake_embed)
    # Pin voice.pick output so the content arg is deterministic.
    monkeypatch.setattr(notify_priority.voice, "pick", lambda key: "MOVE FAST")
    return fake_embed


async def test_post_priority_sends_with_view_and_content(patched):
    chan = MagicMock(spec=discord.TextChannel)
    chan.send = AsyncMock(return_value=None)
    bot = _bot_with_channel(chan)
    before = _delivered()

    await notify_priority.post_priority(
        bot,
        {"opp": {"id": "33333333-3333-3333-3333-333333333333"}, "score": 0.91, "reason": "high-fit"},
    )

    bot._resolve_channel.assert_awaited_once_with(777)
    chan.send.assert_awaited_once()
    kwargs = chan.send.call_args.kwargs
    assert kwargs["content"] == "MOVE FAST"
    assert kwargs["embed"] is patched
    assert kwargs["view"].__class__.__name__ == "OppActionView"
    assert _delivered() == before + 1


async def test_post_priority_skips_when_channel_missing(patched):
    bot = SimpleNamespace(_resolve_channel=AsyncMock(return_value=None))
    before = _delivered()
    await notify_priority.post_priority(bot, {"opp": {"id": "x"}, "score": 0.9})
    assert _delivered() == before  # never incremented


async def test_post_priority_falls_back_to_opportunity_id(patched):
    """View opp_id comes from payload['opportunity_id'] when payload['opp']['id'] missing."""
    chan = MagicMock(spec=discord.TextChannel)
    chan.send = AsyncMock(return_value=None)
    bot = _bot_with_channel(chan)
    await notify_priority.post_priority(
        bot,
        {"opp": {}, "opportunity_id": "44444444-4444-4444-4444-444444444444"},
    )
    view = chan.send.call_args.kwargs["view"]
    # The view stores 5 buttons all keyed off the same opp_id; sniff the first
    # button's custom_id which encodes the opp uuid.
    cids = [getattr(item, "custom_id", "") for item in view.children]
    assert any("44444444-4444-4444-4444-444444444444" in c for c in cids if c)


async def test_post_priority_works_when_opp_is_flat_payload(patched):
    """If caller forwards the opp itself (no nested `opp`), use the payload directly."""
    chan = MagicMock(spec=discord.TextChannel)
    chan.send = AsyncMock(return_value=None)
    bot = _bot_with_channel(chan)
    await notify_priority.post_priority(
        bot,
        {"id": "55555555-5555-5555-5555-555555555555", "category": "freelance", "score": 0.8},
    )
    chan.send.assert_awaited_once()

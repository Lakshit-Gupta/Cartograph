"""Contract tests for `post_opp` (handles kind=opp|lane_post|ranked|digested).

The handler routes by `opp.category` → channel id via `route_for`, builds an
opp_card embed, attaches `OppActionView`, and forwards to `bot._send_embed`.
When `score >= push_threshold` it also fans out to `post_priority`.

Tests stay hermetic by stubbing `route_for`, `build_opp_card`, and the bot's
helper methods (`_resolve_channel`, `_send_embed`).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from src.notifiers.discord.handlers import notify_opp


def _fake_bot() -> SimpleNamespace:
    chan = MagicMock(spec=discord.TextChannel)
    bot = SimpleNamespace(
        _resolve_channel=AsyncMock(return_value=chan),
        _send_embed=AsyncMock(return_value=None),
        _chan=chan,
    )
    return bot


@pytest.fixture
def patched_handler(monkeypatch):
    """Stub the routing + embed builder so we exercise just the handler logic."""
    monkeypatch.setattr(
        notify_opp,
        "route_for",
        lambda opp, kind: {
            "channel_id": 555,
            "channel_name": "freelance",
            "embed_color": 0xEC4899,
            "forum": True,
            "push_threshold": 0.75,
            "mention_owner": False,
        },
    )
    fake_embed = MagicMock(spec=discord.Embed)
    monkeypatch.setattr(notify_opp, "build_opp_card", lambda *a, **kw: fake_embed)
    return fake_embed


async def test_post_opp_resolves_channel_and_sends_embed(patched_handler):
    bot = _fake_bot()
    opp = {"id": "11111111-1111-1111-1111-111111111111", "category": "freelance", "title": "x"}

    await notify_opp.post_opp(bot, {"opp": opp, "score": 0.5})

    bot._resolve_channel.assert_awaited_once_with(555)
    assert bot._send_embed.await_count == 1
    args, kwargs = bot._send_embed.call_args
    assert args[0] is bot._chan
    assert args[1] is patched_handler  # the stubbed embed object
    # OppActionView attached + route forwarded.
    assert "view" in kwargs and kwargs["view"].__class__.__name__ == "OppActionView"
    assert kwargs["route"]["channel_id"] == 555


async def test_post_opp_skips_when_channel_missing(monkeypatch, patched_handler):
    bot = _fake_bot()
    bot._resolve_channel = AsyncMock(return_value=None)
    await notify_opp.post_opp(bot, {"opp": {"id": "x", "category": "freelance"}})
    bot._send_embed.assert_not_called()


async def test_post_opp_fans_out_to_priority_when_over_threshold(monkeypatch, patched_handler):
    bot = _fake_bot()
    priority_spy = AsyncMock(return_value=None)
    # Patch the *late-imported* symbol from inside notify_opp's try block.
    import src.notifiers.discord.handlers.notify_priority as np

    monkeypatch.setattr(np, "post_priority", priority_spy)

    opp = {"id": "22222222-2222-2222-2222-222222222222", "category": "freelance"}
    await notify_opp.post_opp(bot, {"opp": opp, "score": 0.95})

    priority_spy.assert_awaited_once()
    fwd_args, _ = priority_spy.await_args
    assert fwd_args[1]["opp"] == opp
    assert fwd_args[1]["score"] == 0.95


async def test_post_opp_does_not_fan_out_below_threshold(monkeypatch, patched_handler):
    bot = _fake_bot()
    priority_spy = AsyncMock(return_value=None)
    import src.notifiers.discord.handlers.notify_priority as np

    monkeypatch.setattr(np, "post_priority", priority_spy)

    await notify_opp.post_opp(
        bot,
        {"opp": {"id": "x", "category": "freelance"}, "score": 0.50},
    )
    priority_spy.assert_not_called()


async def test_post_opp_handles_non_numeric_score(monkeypatch, patched_handler):
    """Bad payload score values must not raise — handler wraps in try/except."""
    bot = _fake_bot()
    priority_spy = AsyncMock(return_value=None)
    import src.notifiers.discord.handlers.notify_priority as np

    monkeypatch.setattr(np, "post_priority", priority_spy)

    # Should not raise.
    await notify_opp.post_opp(bot, {"opp": {"id": "x", "category": "freelance"}, "score": "not-a-number"})
    bot._send_embed.assert_awaited_once()
    priority_spy.assert_not_called()

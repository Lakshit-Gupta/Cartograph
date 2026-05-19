"""Contract tests for `post_followup_ready` (kind=followup_ready, Phase 2.3).

The handler surfaces an LLM-drafted follow-up with Send / Edit / Skip buttons:
1. Merges nested `payload['payload']` into top-level keys.
2. Bails (no send, no metric) when `followup_id` missing.
3. Attaches a `FollowupActionView(followup_id=int(...))`.
4. Branches on ForumChannel vs TextChannel for thread creation.
5. Increments `deliver_success_total{channel="followup"}` once on success.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from src.common.metrics import deliver_success_total
from src.notifiers.discord.handlers import notify_followup


def _delivered() -> float:
    return deliver_success_total.labels(channel="followup")._value.get()


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(notify_followup, "channel_id_for", lambda name: 654 if name == "applied" else None)
    fake_embed = MagicMock(spec=discord.Embed)
    monkeypatch.setattr(notify_followup, "build_followup_ready", lambda *a, **kw: fake_embed)
    monkeypatch.setattr(notify_followup, "thread_title", lambda t, c: f"Follow-up — {t} @ {c}")
    return fake_embed


def _text_channel():
    msg = MagicMock()
    msg.create_thread = AsyncMock(return_value=SimpleNamespace(id=1))
    chan = MagicMock(spec=discord.TextChannel)
    chan.send = AsyncMock(return_value=msg)
    return chan, msg


def _forum_channel():
    chan = MagicMock(spec=discord.ForumChannel)
    chan.create_thread = AsyncMock(return_value=None)
    return chan


async def test_post_followup_text_channel_creates_thread(patched):
    chan, msg = _text_channel()
    bot = SimpleNamespace(_resolve_channel=AsyncMock(return_value=chan))
    before = _delivered()

    await notify_followup.post_followup_ready(
        bot,
        {"followup_id": 17, "title": "Engineer", "company": "Acme", "body_markdown": "hi"},
    )

    chan.send.assert_awaited_once()
    view = chan.send.await_args.kwargs["view"]
    assert view.__class__.__name__ == "FollowupActionView"
    msg.create_thread.assert_awaited_once()
    assert _delivered() == before + 1


async def test_post_followup_forum_channel_creates_thread(patched):
    chan = _forum_channel()
    bot = SimpleNamespace(_resolve_channel=AsyncMock(return_value=chan))

    await notify_followup.post_followup_ready(
        bot,
        {"followup_id": 8, "title": "T", "company": "C"},
    )

    chan.create_thread.assert_awaited_once()


async def test_post_followup_missing_id_returns_silently(patched):
    bot = SimpleNamespace(_resolve_channel=AsyncMock())
    before = _delivered()

    await notify_followup.post_followup_ready(bot, {"title": "T"})

    bot._resolve_channel.assert_not_called()
    assert _delivered() == before


async def test_post_followup_no_channel_skips(patched):
    bot = SimpleNamespace(_resolve_channel=AsyncMock(return_value=None))
    before = _delivered()

    await notify_followup.post_followup_ready(bot, {"followup_id": 3})
    assert _delivered() == before


async def test_post_followup_merges_nested_payload(patched):
    chan, _msg = _text_channel()
    bot = SimpleNamespace(_resolve_channel=AsyncMock(return_value=chan))

    await notify_followup.post_followup_ready(
        bot,
        {"payload": {"followup_id": 22, "title": "T", "company": "C"}},
    )

    view = chan.send.await_args.kwargs["view"]
    cids = [getattr(item, "custom_id", "") for item in view.children]
    assert any("22" in (c or "") for c in cids)


async def test_post_followup_propagates_exceptions(patched, monkeypatch):
    """Handler re-raises after logging so DLQ kicks in."""
    monkeypatch.setattr(notify_followup, "build_followup_ready", MagicMock(side_effect=RuntimeError("boom")))
    bot = SimpleNamespace(_resolve_channel=AsyncMock())
    with pytest.raises(RuntimeError, match="boom"):
        await notify_followup.post_followup_ready(bot, {"followup_id": 1})

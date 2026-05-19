"""Contract tests for `post_explain_dm` (kind=explain_dm).

The handler:
1. Returns silently when `opp_id` missing.
2. SELECTs the latest `opportunity_scores` row for the opp.
3. JSON-decodes `score_components` when asyncpg returns a string.
4. Composes a "score breakdown" message and sends it to `payload['channel_id']`.

All DB calls are stubbed via monkeypatching `notify_explain.db`.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from src.notifiers.discord.handlers import notify_explain

_OPP_ID = "66666666-6666-6666-6666-666666666666"


@pytest.fixture
def fake_db(monkeypatch):
    fetch = AsyncMock()
    monkeypatch.setattr(notify_explain.db, "fetch_one", fetch)
    # Pin voice for a deterministic prefix.
    monkeypatch.setattr(notify_explain.voice, "pick", lambda key: "Score breakdown:")
    return fetch


def _bot(chan):
    return SimpleNamespace(_resolve_channel=AsyncMock(return_value=chan))


async def test_post_explain_dm_no_opp_id_returns_silently(fake_db):
    bot = SimpleNamespace(_resolve_channel=AsyncMock())
    await notify_explain.post_explain_dm(bot, {"channel_id": 1})
    fake_db.assert_not_called()
    bot._resolve_channel.assert_not_called()


async def test_post_explain_dm_no_row_returns_silently(fake_db):
    fake_db.return_value = None
    bot = SimpleNamespace(_resolve_channel=AsyncMock())
    await notify_explain.post_explain_dm(bot, {"opp_id": _OPP_ID, "channel_id": 1})
    fake_db.assert_awaited_once()
    bot._resolve_channel.assert_not_called()


async def test_post_explain_dm_sends_decoded_components(fake_db):
    fake_db.return_value = {
        "score": 0.7345,
        "score_components": '{"skill_overlap": 0.5, "freshness": 0.3}',
    }
    chan = MagicMock(spec=discord.TextChannel)
    chan.send = AsyncMock(return_value=None)
    bot = _bot(chan)

    await notify_explain.post_explain_dm(bot, {"opp_id": _OPP_ID, "channel_id": 42})

    bot._resolve_channel.assert_awaited_once_with(42)
    chan.send.assert_awaited_once()
    sent = chan.send.await_args.args[0]
    assert sent.startswith("Score breakdown: score=0.73")
    assert "skill_overlap=0.50" in sent
    assert "freshness=0.30" in sent


async def test_post_explain_dm_handles_dict_components(fake_db):
    fake_db.return_value = {"score": 0.5, "score_components": {"a": 0.1}}
    chan = MagicMock(spec=discord.TextChannel)
    chan.send = AsyncMock(return_value=None)
    bot = _bot(chan)
    await notify_explain.post_explain_dm(bot, {"opp_id": _OPP_ID, "channel_id": 9})
    sent = chan.send.await_args.args[0]
    assert "a=0.10" in sent


async def test_post_explain_dm_swallows_exception(fake_db):
    """The handler is wrapped in try/except — any error must not propagate."""
    fake_db.side_effect = RuntimeError("db down")
    bot = SimpleNamespace(_resolve_channel=AsyncMock())
    # Must not raise.
    await notify_explain.post_explain_dm(bot, {"opp_id": _OPP_ID, "channel_id": 1})


async def test_post_explain_dm_skips_when_channel_unresolved(fake_db):
    fake_db.return_value = {"score": 0.1, "score_components": {}}
    bot = SimpleNamespace(_resolve_channel=AsyncMock(return_value=None))
    # Must not raise — channel None just means no send.
    await notify_explain.post_explain_dm(bot, {"opp_id": _OPP_ID, "channel_id": 0})

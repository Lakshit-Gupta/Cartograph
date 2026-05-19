"""Contract tests for `post_applied` (kind=applied).

The handler:
1. Merges nested `payload['payload']` into top-level (per-handler flatten).
2. Looks up opp metadata via `resolve_opp_metadata`.
3. Resolves the `applied` channel.
4. Branches on ForumChannel vs TextChannel for thread creation.
5. Persists the thread id on `applications.discord_thread_id`.
6. Increments `deliver_success_total{channel="applied"}`.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from src.common.metrics import deliver_success_total
from src.notifiers.discord.handlers import notify_applied


def _delivered() -> float:
    return deliver_success_total.labels(channel="applied")._value.get()


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(notify_applied, "channel_id_for", lambda name: 123 if name == "applied" else None)
    fake_embed = MagicMock(spec=discord.Embed)
    monkeypatch.setattr(notify_applied.applied_embed, "build_applied", lambda *a, **kw: fake_embed)
    monkeypatch.setattr(notify_applied.applied_embed, "build_view", lambda *a, **kw: MagicMock(spec=discord.ui.View))
    monkeypatch.setattr(notify_applied.applied_embed, "thread_title", lambda t, c: f"{t} @ {c}")

    resolve = AsyncMock(return_value={})
    monkeypatch.setattr(notify_applied, "resolve_opp_metadata", resolve)

    execute = AsyncMock(return_value=None)
    monkeypatch.setattr(notify_applied.db, "execute", execute)
    return SimpleNamespace(embed=fake_embed, resolve=resolve, execute=execute)


def _text_channel():
    chan = MagicMock(spec=discord.TextChannel)
    msg = MagicMock()
    msg.create_thread = AsyncMock(return_value=SimpleNamespace(id=987))
    chan.send = AsyncMock(return_value=msg)
    return chan


def _forum_channel():
    chan = MagicMock(spec=discord.ForumChannel)
    thread_obj = SimpleNamespace(thread=SimpleNamespace(id=555))
    chan.create_thread = AsyncMock(return_value=thread_obj)
    return chan


async def test_post_applied_text_channel_path(patched):
    chan = _text_channel()
    bot = SimpleNamespace(_resolve_channel=AsyncMock(return_value=chan))
    before = _delivered()

    await notify_applied.post_applied(
        bot,
        {
            "opportunity_id": "abc",
            "title": "Engineer",
            "company": "Acme",
            "method": "email",
            "target": "jobs@acme.com",
            "application_id": 42,
        },
    )

    chan.send.assert_awaited_once()
    # discord_thread_id persisted.
    patched.execute.assert_awaited_once()
    sql, thread_id, app_id = patched.execute.await_args.args
    assert "UPDATE applications SET discord_thread_id" in sql
    assert thread_id == 987
    assert app_id == 42
    assert _delivered() == before + 1


async def test_post_applied_forum_channel_path(patched):
    chan = _forum_channel()
    bot = SimpleNamespace(_resolve_channel=AsyncMock(return_value=chan))

    await notify_applied.post_applied(
        bot,
        {"opportunity_id": "abc", "title": "T", "company": "C", "application_id": 7},
    )

    chan.create_thread.assert_awaited_once()
    # The forum branch returns thread id 555 (from the SimpleNamespace fixture).
    _, thread_id, app_id = patched.execute.await_args.args
    assert thread_id == 555
    assert app_id == 7


async def test_post_applied_merges_nested_payload(patched):
    chan = _text_channel()
    bot = SimpleNamespace(_resolve_channel=AsyncMock(return_value=chan))

    await notify_applied.post_applied(
        bot,
        {"payload": {"title": "Inner", "company": "InnerCo", "application_id": 99}},
    )

    # Persist called with the merged application_id.
    _, _, app_id = patched.execute.await_args.args
    assert app_id == 99


async def test_post_applied_uses_opp_row_fallback(patched):
    """When payload omits title/company, the opp DB row supplies them."""
    patched.resolve.return_value = {"title": "DB Title", "company": "DBCo", "apply_url": "https://x"}
    chan = _text_channel()
    bot = SimpleNamespace(_resolve_channel=AsyncMock(return_value=chan))

    await notify_applied.post_applied(bot, {"opportunity_id": "abc"})
    chan.send.assert_awaited_once()


async def test_post_applied_no_channel_skips(patched):
    bot = SimpleNamespace(_resolve_channel=AsyncMock(return_value=None))
    before = _delivered()

    await notify_applied.post_applied(bot, {"opportunity_id": "abc", "application_id": 1})

    patched.execute.assert_not_called()
    assert _delivered() == before


async def test_post_applied_propagates_exceptions(patched):
    """The handler re-raises after logging — DLQ depends on this for retries."""
    patched.resolve.side_effect = RuntimeError("boom")
    bot = SimpleNamespace(_resolve_channel=AsyncMock())

    with pytest.raises(RuntimeError, match="boom"):
        await notify_applied.post_applied(bot, {"opportunity_id": "abc"})

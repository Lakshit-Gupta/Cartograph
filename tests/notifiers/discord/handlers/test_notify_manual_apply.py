"""Contract tests for `post_manual_apply` (kind=manual_apply_ready).

The handler:
1. Merges nested `payload['payload']` into top-level keys.
2. Resolves opp metadata + the `applied` channel.
3. Builds the amber [REVIEW] embed + an `OppReviewView`.
4. Branches on ForumChannel vs TextChannel to create the review thread.
5. Fans out the cover-letter as chunked code-block messages into the thread.
6. Increments `deliver_success_total{channel="applied"}`.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from src.common.metrics import deliver_success_total
from src.notifiers.discord.handlers import notify_manual_apply


def _delivered() -> float:
    return deliver_success_total.labels(channel="applied")._value.get()


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(notify_manual_apply, "channel_id_for", lambda name: 321 if name == "applied" else None)
    fake_embed = MagicMock(spec=discord.Embed)
    monkeypatch.setattr(notify_manual_apply.manual_apply_embed, "build_manual_apply", lambda *a, **kw: fake_embed)
    monkeypatch.setattr(notify_manual_apply.manual_apply_embed, "thread_title", lambda t, c: f"[REVIEW] {t} @ {c}")
    # Default chunker — split on 1900 chars; we override in specific tests if needed.
    monkeypatch.setattr(
        notify_manual_apply.manual_apply_embed,
        "chunk_cover_letter",
        lambda text, max_len=1900: [text] if text else [],
    )

    resolve = AsyncMock(return_value={})
    monkeypatch.setattr(notify_manual_apply, "resolve_opp_metadata", resolve)
    return SimpleNamespace(embed=fake_embed, resolve=resolve)


def _text_channel_with_thread():
    thread = MagicMock(spec=discord.Thread)
    thread.send = AsyncMock(return_value=None)
    msg = MagicMock()
    msg.create_thread = AsyncMock(return_value=thread)
    chan = MagicMock(spec=discord.TextChannel)
    chan.send = AsyncMock(return_value=msg)
    return chan, thread


def _forum_channel():
    thread = MagicMock(spec=discord.Thread)
    thread.send = AsyncMock(return_value=None)
    chan = MagicMock(spec=discord.ForumChannel)
    chan.create_thread = AsyncMock(return_value=SimpleNamespace(thread=thread))
    return chan, thread


async def test_post_manual_apply_text_channel_fans_out_cover_letter(patched):
    chan, thread = _text_channel_with_thread()
    bot = SimpleNamespace(_resolve_channel=AsyncMock(return_value=chan))
    before = _delivered()

    await notify_manual_apply.post_manual_apply(
        bot,
        {
            "opp_id": "abc",
            "title": "Engineer",
            "company": "Acme",
            "apply_url": "https://acme/apply",
            "tailored_bullets": ["one", "two"],
            "cover_letter_markdown": "Dear hiring manager,\n\nI am ...",
        },
    )

    chan.send.assert_awaited_once()
    # Cover-letter chunk sent into the thread, code-fenced.
    thread.send.assert_awaited_once()
    sent = thread.send.await_args.kwargs["content"]
    assert sent.startswith("```\n") and sent.endswith("\n```")
    assert _delivered() == before + 1


async def test_post_manual_apply_forum_channel(patched):
    chan, thread = _forum_channel()
    bot = SimpleNamespace(_resolve_channel=AsyncMock(return_value=chan))

    await notify_manual_apply.post_manual_apply(
        bot,
        {"opp_id": "abc", "title": "T", "company": "C", "cover_letter_markdown": "hi"},
    )

    chan.create_thread.assert_awaited_once()
    thread.send.assert_awaited_once()


async def test_post_manual_apply_skips_chunks_when_no_cover_letter(patched):
    chan, thread = _text_channel_with_thread()
    bot = SimpleNamespace(_resolve_channel=AsyncMock(return_value=chan))

    await notify_manual_apply.post_manual_apply(bot, {"opp_id": "abc", "title": "T", "company": "C"})

    chan.send.assert_awaited_once()
    thread.send.assert_not_called()


async def test_post_manual_apply_no_channel_skips(patched):
    bot = SimpleNamespace(_resolve_channel=AsyncMock(return_value=None))
    before = _delivered()
    await notify_manual_apply.post_manual_apply(bot, {"opp_id": "abc"})
    assert _delivered() == before


async def test_post_manual_apply_merges_nested_payload(patched):
    chan, _thread = _text_channel_with_thread()
    bot = SimpleNamespace(_resolve_channel=AsyncMock(return_value=chan))
    captured_view: list = []

    real_send = chan.send

    async def _capture(*args, **kwargs):
        captured_view.append(kwargs.get("view"))
        return await real_send(*args, **kwargs)

    chan.send = AsyncMock(side_effect=_capture)

    await notify_manual_apply.post_manual_apply(
        bot,
        {"opp_id": "outer", "payload": {"opp_id": "inner", "title": "T", "company": "C"}},
    )

    # OppReviewView's children carry the merged opp id in their custom_ids.
    view = captured_view[0]
    cids = [getattr(item, "custom_id", "") for item in view.children]
    assert any("inner" in (c or "") for c in cids)


async def test_post_manual_apply_propagates_exceptions(patched):
    patched.resolve.side_effect = RuntimeError("boom")
    bot = SimpleNamespace(_resolve_channel=AsyncMock())
    with pytest.raises(RuntimeError, match="boom"):
        await notify_manual_apply.post_manual_apply(bot, {"opp_id": "abc"})

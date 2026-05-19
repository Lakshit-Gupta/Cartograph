"""Contract tests for `post_digest` (kind=digest).

The handler is the most complex: it loads top-K opps from Postgres, posts a
header embed, fans out opp_card embeds, then flips successfully-posted opps
to state='digested'. The state flip MUST only fire after at least one
successful send.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from src.common.metrics import deliver_success_total
from src.notifiers.discord.handlers import notify_digest


def _digest_delivered() -> float:
    return deliver_success_total.labels(channel="digest")._value.get()


def _opp_delivered() -> float:
    return deliver_success_total.labels(channel="opp")._value.get()


def _row(idx: int, score: float, components=None):
    return {
        "id": f"{idx:08d}-0000-0000-0000-000000000000",
        "title": f"Job {idx}",
        "company": "Acme",
        "description": "desc",
        "canonical_url": "https://x",
        "apply_url": "https://x/apply",
        "comp_min": None,
        "comp_max": None,
        "comp_currency": None,
        "comp_period": None,
        "location": "Remote",
        "remote_type": "remote",
        "category": "freelance",
        "posted_at": None,
        "score": score,
        "score_components": components if components is not None else '{"skill": 0.5}',
    }


@pytest.fixture
def fakes(monkeypatch):
    """Stub channel resolution, DB calls, and the embed builders."""
    monkeypatch.setattr(notify_digest, "channel_id_for", lambda name: 100 if name == "daily_digest" else None)

    fetch_all = AsyncMock(return_value=[])
    execute = AsyncMock(return_value=None)
    monkeypatch.setattr(notify_digest.db, "fetch_all", fetch_all)
    monkeypatch.setattr(notify_digest.db, "execute", execute)
    monkeypatch.setattr(notify_digest.db, "current_tenant", lambda: 1)

    header_embed = MagicMock(spec=discord.Embed)
    opp_embed = MagicMock(spec=discord.Embed)
    monkeypatch.setattr(notify_digest, "build_digest_header", lambda *a, **kw: header_embed)
    monkeypatch.setattr(notify_digest, "build_opp_card", lambda *a, **kw: opp_embed)
    return SimpleNamespace(fetch_all=fetch_all, execute=execute, header=header_embed, card=opp_embed)


def _bot_with_send():
    chan = MagicMock(spec=discord.TextChannel)
    chan.send = AsyncMock(return_value=None)
    return SimpleNamespace(_resolve_channel=AsyncMock(return_value=chan), _chan=chan)


async def test_post_digest_no_channel_skips_entirely(fakes):
    bot = SimpleNamespace(_resolve_channel=AsyncMock(return_value=None))
    await notify_digest.post_digest(bot, {})
    fakes.fetch_all.assert_not_called()
    fakes.execute.assert_not_called()


async def test_post_digest_empty_rows_sends_header_no_flip(fakes):
    fakes.fetch_all.return_value = []
    bot = _bot_with_send()
    before_digest = _digest_delivered()

    await notify_digest.post_digest(bot, {"user_id": 1})

    # Header sent exactly once.
    assert bot._chan.send.await_count == 1
    sent_embed = bot._chan.send.await_args.kwargs["embed"]
    assert sent_embed is fakes.header
    # No state flip.
    fakes.execute.assert_not_called()
    assert _digest_delivered() == before_digest + 1


async def test_post_digest_fans_out_cards_and_flips_only_posted(fakes):
    """Two rows succeed, one raises; flip ANY($1::uuid[]) must contain only the
    two posted ids."""
    fakes.fetch_all.return_value = [_row(1, 0.9), _row(2, 0.8), _row(3, 0.7)]
    bot = _bot_with_send()
    # First call = header (success), then 3 card sends; make middle one raise.
    bot._chan.send.side_effect = [None, None, RuntimeError("discord 500"), None]

    before_opp = _opp_delivered()
    await notify_digest.post_digest(bot, {"user_id": 1})

    # Header + 3 cards attempted.
    assert bot._chan.send.await_count == 4
    # State flip ran exactly once, with the two ids that posted (rows 1 and 3).
    fakes.execute.assert_awaited_once()
    sql, posted_ids = fakes.execute.await_args.args
    assert "UPDATE opportunities SET state = 'digested'" in sql
    assert posted_ids == [_row(1, 0.9)["id"], _row(3, 0.7)["id"]]
    # Only the successful card sends bump the metric.
    assert _opp_delivered() == before_opp + 2


async def test_post_digest_flip_skipped_when_no_send_succeeds(fakes):
    """All card sends fail → no UPDATE issued."""
    fakes.fetch_all.return_value = [_row(1, 0.5)]
    bot = _bot_with_send()
    # Header succeeds (1st), card raises (2nd).
    bot._chan.send.side_effect = [None, RuntimeError("nope")]

    await notify_digest.post_digest(bot, {"user_id": 1})

    fakes.execute.assert_not_called()


async def test_post_digest_resolves_user_id_then_current_tenant(fakes, monkeypatch):
    """When `user_id` missing, the handler falls back to `db.current_tenant()`."""
    fakes.fetch_all.return_value = []
    monkeypatch.setattr(notify_digest.db, "current_tenant", lambda: 7)
    bot = _bot_with_send()

    await notify_digest.post_digest(bot, {})  # no user_id key

    # First positional arg after the SQL is the tenant id.
    fakes.fetch_all.assert_awaited_once()
    _, tenant = fakes.fetch_all.await_args.args
    assert tenant == 7


async def test_post_digest_normalizes_str_components(fakes):
    """`score_components` arriving as JSON string must be JSON-decoded before
    passing to `build_opp_card`; this guards the asyncpg-no-jsonb path."""
    fakes.fetch_all.return_value = [_row(1, 0.6, components='{"k":1}')]
    bot = _bot_with_send()
    captured: list[dict] = []

    def _build(opp, *, score, score_components):
        captured.append(score_components)
        return fakes.card

    import src.notifiers.discord.handlers.notify_digest as nd

    nd.build_opp_card = _build  # monkeypatch directly

    await notify_digest.post_digest(bot, {"user_id": 1})

    assert captured and captured[0] == {"k": 1}

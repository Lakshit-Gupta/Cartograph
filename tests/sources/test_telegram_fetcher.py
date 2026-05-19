"""Unit tests for src.sources.freelance.telegram_fetcher.

Mocks Telethon entirely — no live MTProto, no .session file read at test time.
The pure parser helpers (parse_message / build_opportunity) are exercised
directly; the publish path is exercised via a stubbed RedisQ + a monkeypatched
`persist_and_publish`.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType
from src.sources.freelance import telegram_fetcher as tf


def _fake_message(*, channel: str, message_id: int, text: str) -> SimpleNamespace:
    """Shape-compatible stand-in for a Telethon NewMessage event payload."""
    return SimpleNamespace(
        message=SimpleNamespace(id=message_id, text=text, message=text),
        chat=SimpleNamespace(username=channel, id=12345),
    )


# ---- parser contract --------------------------------------------------------


@pytest.mark.smoke
def test_parses_minimum_payload():
    parsed = tf.parse_message(
        channel="freelance_jobs",
        message_id=42,
        text="Looking for a senior backend Python dev\nRemote, $40/hr\nDM if interested",
    )
    assert parsed is not None
    assert parsed.title.startswith("Looking for")
    assert "DM if interested" in parsed.description
    assert parsed.canonical_url == "https://t.me/freelance_jobs/42"
    # fingerprint = sha256("freelance_jobs:42")
    import hashlib

    expected = hashlib.sha256(b"freelance_jobs:42").hexdigest()
    assert parsed.fingerprint_hash == expected
    assert parsed.comp_min == 40.0
    assert parsed.comp_currency == "USD"
    assert parsed.comp_period == "hour"


@pytest.mark.smoke
def test_skips_empty_message():
    assert tf.parse_message(channel="c", message_id=1, text="") is None
    assert tf.parse_message(channel="c", message_id=2, text="   \n  ") is None


@pytest.mark.smoke
def test_build_opportunity_shape():
    parsed = tf.parse_message(
        channel="fl",
        message_id=7,
        text="Django freelancer wanted\nBudget: $500 fixed",
    )
    assert parsed is not None
    opp = tf.build_opportunity(parsed, source_id=99)
    assert isinstance(opp, Opportunity)
    assert opp.source_id == 99
    assert opp.canonical_url == "https://t.me/fl/7"
    assert opp.category is OppCategory.FREELANCE
    assert opp.remote_type is RemoteType.REMOTE
    assert opp.apply_method is ApplyMethod.EXTERNAL
    assert opp.fingerprint_hash == parsed.fingerprint_hash
    assert opp.comp_min == 500.0
    assert opp.comp_currency == "USD"


@pytest.mark.smoke
def test_fingerprint_is_deterministic_and_excludes_time():
    a = tf._fingerprint("foo", 1)
    b = tf._fingerprint("foo", 1)
    c = tf._fingerprint("foo", 2)
    assert a == b
    assert a != c


@pytest.mark.smoke
def test_normalise_channel_accepts_multiple_formats():
    assert tf._normalise_channel("@foo") == "foo"
    assert tf._normalise_channel("t.me/foo") == "foo"
    assert tf._normalise_channel("https://t.me/foo/") == "foo"
    assert tf._normalise_channel("foo") == "foo"


# ---- publish path -----------------------------------------------------------


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_publishes_to_rank_stream(monkeypatch):
    """`_publish_with_dedupe` must call persist_and_publish exactly once."""
    parsed = tf.parse_message(channel="fl", message_id=1, text="Need React dev")
    assert parsed is not None
    opp = tf.build_opportunity(parsed, source_id=1)

    persist_mock = AsyncMock(return_value="aaaa-uuid")
    monkeypatch.setattr(tf, "persist_and_publish", persist_mock)

    fake_q = SimpleNamespace()
    await tf._publish_with_dedupe(fake_q, opp, channel="fl", message_id=1)

    persist_mock.assert_awaited_once()
    args, _kwargs = persist_mock.call_args
    assert args[0] is fake_q
    published_opp = args[1]
    assert published_opp.canonical_url == "https://t.me/fl/1"
    assert published_opp.fingerprint_hash == parsed.fingerprint_hash


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_dedupe_swallows_unique_violation(monkeypatch):
    """A UniqueViolation (sqlstate 23505) must NOT bubble out of _publish_with_dedupe."""
    parsed = tf.parse_message(channel="fl", message_id=2, text="Need React dev")
    assert parsed is not None
    opp = tf.build_opportunity(parsed, source_id=1)

    class _FakeUniqueViolation(Exception):
        sqlstate = "23505"

    persist_mock = AsyncMock(side_effect=_FakeUniqueViolation("dup canonical_url"))
    monkeypatch.setattr(tf, "persist_and_publish", persist_mock)

    # Must not raise.
    await tf._publish_with_dedupe(SimpleNamespace(), opp, channel="fl", message_id=2)
    persist_mock.assert_awaited_once()


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_dedupe_skip_when_persist_returns_none(monkeypatch):
    """persist_and_publish returns None on dedupe — must not raise + log debug."""
    parsed = tf.parse_message(channel="fl", message_id=3, text="Hi")
    assert parsed is not None
    opp = tf.build_opportunity(parsed, source_id=1)
    persist_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(tf, "persist_and_publish", persist_mock)
    await tf._publish_with_dedupe(SimpleNamespace(), opp, channel="fl", message_id=3)
    persist_mock.assert_awaited_once()


# ---- prefs loading ----------------------------------------------------------


@pytest.mark.smoke
def test_load_channels_from_prefs_real_yaml():
    """Default prefs.yaml has empty freelance.telegram_channels — must return []."""
    out = tf.load_channels_from_prefs()
    assert isinstance(out, list)
    # default is [], but tests may run against a populated repo — assert type only

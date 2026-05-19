"""Unit tests for src.sources.freelance.twitter_fetcher (Phase 3.1).

All tests are hermetic — no live HTTP, no DB, no Redis. The publish path is
exercised via a stubbed RedisQ + a monkeypatched `persist_and_publish`; the
fetch path is exercised via httpx.MockTransport against an in-memory
ASGI-like router.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType
from src.sources.freelance import twitter_fetcher as tw

# ---- hiring keyword filter --------------------------------------------------


@pytest.mark.smoke
@pytest.mark.parametrize(
    "text",
    [
        "We're hiring a senior backend engineer — DM me",
        "Looking for a frontend dev to join our YC W26 team",
        "Paid project — Python automation, $40/hr, 4 weeks",
        "Join us at Acme Robotics, founding engineer role",
        "we need a designer ASAP, contract role",
        "freelance project for our launch next month",
        "We're recruiting interns for the summer",
    ],
)
def test_hiring_pattern_matches_common_phrases(text):
    assert tw.matches_hiring(text) is True


@pytest.mark.smoke
def test_non_hiring_tweet_skipped():
    """Casual / off-topic tweets must NOT trip the filter."""
    negatives = [
        "Great coffee this morning ☕️",
        "Just shipped v2 of our SDK 🚀",
        "Thoughts on the new MacBook? Tempted to upgrade.",
        "Reading 'The Mythical Man-Month' for the third time.",
        "",
        "   ",
    ]
    for text in negatives:
        assert tw.matches_hiring(text) is False, f"unexpectedly matched: {text!r}"


# ---- category inference -----------------------------------------------------


def test_infer_category_internship_wins_over_freelance():
    assert tw.infer_category("Hiring summer interns — paid project") is OppCategory.INTERNSHIP


def test_infer_category_fulltime_wins_over_freelance():
    assert tw.infer_category("We're hiring a full-time backend engineer (freelance to FT)") is OppCategory.FULLTIME


def test_infer_category_defaults_to_freelance():
    assert tw.infer_category("Looking for a Python dev — DM rates") is OppCategory.FREELANCE


# ---- handle normalisation ---------------------------------------------------


def test_normalise_handle_accepts_multiple_formats():
    assert tw._normalise_handle("@paulg") == "paulg"
    assert tw._normalise_handle("paulg") == "paulg"
    assert tw._normalise_handle("https://twitter.com/paulg/") == "paulg"
    assert tw._normalise_handle("https://x.com/paulg") == "paulg"
    assert tw._normalise_handle("twitter.com/Paulg") == "paulg"
    assert tw._normalise_handle("") == ""


# ---- fingerprint stability --------------------------------------------------


@pytest.mark.smoke
def test_fingerprint_stable_across_runs():
    """sha256(twitter:handle:tweet_id) must be deterministic + restart-safe."""
    a = tw._fingerprint("paulg", "1234567890")
    b = tw._fingerprint("paulg", "1234567890")
    c = tw._fingerprint("paulg", "1234567891")
    d = tw._fingerprint("dhh", "1234567890")
    assert a == b
    assert a != c
    assert a != d
    expected = hashlib.sha256(b"twitter:paulg:1234567890").hexdigest()
    assert a == expected


# ---- HTML parser ------------------------------------------------------------


_NITTER_FIXTURE = """
<html><body>
  <div class="timeline-item">
    <a class="tweet-link" href="/paulg/status/1700000000000000001#m"></a>
    <div class="tweet-header">
      <span class="tweet-date"><a title="Jan 15, 2026 · 4:32 PM UTC">15 Jan</a></span>
    </div>
    <div class="tweet-content">We're hiring a senior backend engineer. DM if interested.</div>
  </div>
  <div class="timeline-item">
    <a class="tweet-link" href="/paulg/status/1700000000000000002#m"></a>
    <div class="tweet-content">Great cappuccino this morning ☕️</div>
  </div>
  <div class="timeline-item">
    <a class="tweet-link" href="/paulg/status/1700000000000000003#m"></a>
    <div class="tweet-content">Looking for a freelance Python dev — $50/hr</div>
  </div>
  <!-- malformed: no tweet-link, must be skipped -->
  <div class="timeline-item">
    <div class="tweet-content">Orphan tweet without a link</div>
  </div>
</body></html>
"""


@pytest.mark.smoke
def test_parse_tweet_html_extracts_id_and_text():
    parsed = tw.parse_tweet_html(_NITTER_FIXTURE, handle="paulg")
    assert len(parsed) == 3
    ids = [p.tweet_id for p in parsed]
    assert ids == [
        "1700000000000000001",
        "1700000000000000002",
        "1700000000000000003",
    ]
    assert "hiring" in parsed[0].text.lower()
    # canonical link routes through twitter.com (not the Nitter mirror).
    assert parsed[0].link == "https://twitter.com/paulg/status/1700000000000000001"
    # First match has a parseable timestamp, second doesn't — both must
    # be returned regardless.
    assert parsed[0].posted_at is not None
    # Fingerprint is stable.
    assert parsed[0].fingerprint_hash == tw._fingerprint("paulg", "1700000000000000001")


def test_parse_tweet_html_empty_input():
    assert tw.parse_tweet_html("", handle="paulg") == []
    assert tw.parse_tweet_html("<html></html>", handle="paulg") == []


# ---- tweet → Opportunity shape ---------------------------------------------


def test_tweet_to_opportunity_shape():
    match = tw.TweetMatch(
        tweet_id="42",
        handle="paulg",
        text="We're hiring a backend engineer — Python, remote, $80k+",
        link="https://twitter.com/paulg/status/42",
        posted_at=None,
        fingerprint_hash=tw._fingerprint("paulg", "42"),
    )
    opp = tw.tweet_to_opportunity(match, source_id=99)
    assert isinstance(opp, Opportunity)
    assert opp.source_id == 99
    assert opp.canonical_url == "https://twitter.com/paulg/status/42"
    assert opp.apply_method is ApplyMethod.EXTERNAL
    assert opp.remote_type is RemoteType.UNSPECIFIED
    # 'hiring' alone defaults to freelance (no explicit FT/intern signal).
    assert opp.category is OppCategory.FREELANCE
    assert opp.fingerprint_hash == match.fingerprint_hash
    assert opp.title.startswith("We're hiring")


# ---- publish path -----------------------------------------------------------


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_publishes_to_rank_stream(monkeypatch):
    match = tw.TweetMatch(
        tweet_id="1",
        handle="paulg",
        text="Looking for a React dev",
        link="https://twitter.com/paulg/status/1",
        posted_at=None,
        fingerprint_hash=tw._fingerprint("paulg", "1"),
    )
    opp = tw.tweet_to_opportunity(match, source_id=1)
    persist_mock = AsyncMock(return_value="aaaa-uuid")
    monkeypatch.setattr(tw, "persist_and_publish", persist_mock)
    await tw._publish_with_dedupe(SimpleNamespace(), opp, handle="paulg", tweet_id="1")
    persist_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_dedupe_swallows_unique_violation(monkeypatch):
    match = tw.TweetMatch(
        tweet_id="2",
        handle="paulg",
        text="Looking for a React dev",
        link="https://twitter.com/paulg/status/2",
        posted_at=None,
        fingerprint_hash=tw._fingerprint("paulg", "2"),
    )
    opp = tw.tweet_to_opportunity(match, source_id=1)

    class _FakeUniqueViolation(Exception):
        sqlstate = "23505"

    persist_mock = AsyncMock(side_effect=_FakeUniqueViolation("dup canonical_url"))
    monkeypatch.setattr(tw, "persist_and_publish", persist_mock)
    # Must not raise.
    await tw._publish_with_dedupe(SimpleNamespace(), opp, handle="paulg", tweet_id="2")
    persist_mock.assert_awaited_once()


# ---- mirror rotation --------------------------------------------------------


def test_mirror_rotator_cools_used_mirror():
    rot = tw._MirrorRotator(("https://a", "https://b", "https://c"))
    first = rot.pick()
    assert first is not None
    rot.cool(first)
    # Next pick must be a different mirror (the others are still ready).
    second = rot.pick()
    assert second is not None
    assert second != first


def test_daily_budget_caps_per_handle():
    bud = tw._DailyBudget(cap=2)
    assert bud.allowed("paulg") is True
    bud.increment("paulg")
    assert bud.allowed("paulg") is True
    bud.increment("paulg")
    assert bud.allowed("paulg") is False
    # different handle has its own bucket
    assert bud.allowed("dhh") is True


# ---- fetch + mirror failover ------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_handle_returns_empty_on_all_mirrors_500(monkeypatch):
    """Every mirror returning 500 ⇒ empty result, no exception."""

    def handler(request):
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    rotator = tw._MirrorRotator(tw.NITTER_INSTANCES)
    async with httpx.AsyncClient(transport=transport) as client:
        out = await tw.fetch_handle("paulg", http_client=client, rotator=rotator)
    assert out == []


@pytest.mark.asyncio
async def test_fetch_handle_parses_first_healthy_mirror():
    """A 200 response with hiring tweet must yield a TweetMatch."""

    def handler(request):
        return httpx.Response(200, text=_NITTER_FIXTURE)

    transport = httpx.MockTransport(handler)
    rotator = tw._MirrorRotator(tw.NITTER_INSTANCES)
    async with httpx.AsyncClient(transport=transport) as client:
        out = await tw.fetch_handle("paulg", http_client=client, rotator=rotator)
    # Two of three fixtures are hiring-intent.
    assert len(out) == 2
    assert all(tw.matches_hiring(m.text) for m in out)


# ---- prefs loading ----------------------------------------------------------


def test_load_handles_from_prefs_real_yaml():
    """Default prefs.yaml has empty freelance.twitter_handles — must return []."""
    out = tw.load_handles_from_prefs()
    assert isinstance(out, list)
    # The repo ships with [] but tests may run against a populated prefs.
    for h in out:
        assert h == h.lower()
        assert not h.startswith("@")


# ---- worker boots with no handles ------------------------------------------


@pytest.mark.asyncio
async def test_run_boots_with_no_handles(monkeypatch):
    """`run()` must idle (not crash) when prefs has zero handles configured."""
    # Make DB + Redis no-ops.
    monkeypatch.setattr(tw, "init_pool", AsyncMock(return_value=None))
    monkeypatch.setattr(tw, "close_pool", AsyncMock(return_value=None))
    monkeypatch.setattr(tw, "RedisQ", SimpleNamespace(connect=AsyncMock(return_value=SimpleNamespace())))

    # Force handles list to empty + cancel after one idle tick.
    monkeypatch.setattr(tw, "load_handles_from_prefs", lambda: [])
    monkeypatch.setattr(tw, "resolve_source_id", AsyncMock(return_value=None))
    # Shrink the idle sleep so we don't hang the suite.
    monkeypatch.setattr(tw, "_IDLE_SLEEP_SECONDS", 0.01)

    import asyncio as _asyncio

    async def _runner():
        task = _asyncio.create_task(tw.run())
        # Let the idle loop tick a few times, then cancel.
        await _asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except _asyncio.CancelledError:
            pass

    # Must not raise.
    await _runner()

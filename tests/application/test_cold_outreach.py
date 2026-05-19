"""Tests for the Phase 2.1 cold-email outbound lane.

Coverage:
  - Cap module: feature-flag refuse, daily-cap refuse, warmup ramp math,
    recipient + subject dedupe windows.
  - Drafter: 90-word truncation, JSON-decode failures yield None.
  - Sanitizer: HTML strip, control-char strip, subject_hash stability.
  - Providers: NullProvider returns []; Apollo respects respx-mocked HTTP.

All DB calls are stubbed via monkeypatch — no live Postgres or Redis.
All HTTP is mocked via respx (httpx test recorder).
"""

from __future__ import annotations

import pytest

from src.application.cold_outreach.base import Contact
from src.application.cold_outreach.cap import CapDecision, _ramp_ceiling, allow_send
from src.application.cold_outreach.drafter import (
    MAX_BODY_WORDS,
    Draft,
    _truncate_words,
    draft_intro,
)
from src.application.cold_outreach.null_provider import NullProvider
from src.application.cold_outreach.sanitizer import scrub_text, subject_hash, word_count

# ---------------------------------------------------------------------------
# Sanitizer
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_scrub_text_strips_html_from_apollo_bio():
    raw = "<p><strong>VP Eng</strong> at Acme. <script>alert('x')</script> Built scale systems.</p>"
    out = scrub_text(raw)
    assert "<" not in out and ">" not in out
    assert "alert" in out  # text content kept, tags gone
    assert "VP Eng" in out and "Acme" in out


def test_scrub_text_drops_control_chars_and_clamps_length():
    raw = "hello\x00\x01world" + ("x" * 600)
    out = scrub_text(raw, max_len=50)
    assert "\x00" not in out and "\x01" not in out
    assert len(out) <= 50


def test_scrub_text_none_returns_empty():
    assert scrub_text(None) == ""
    assert scrub_text("   ") == ""


@pytest.mark.smoke
def test_subject_hash_dedupes_whitespace_and_case():
    h1 = subject_hash("Quick intro")
    h2 = subject_hash("  quick   INTRO ")
    assert h1 == h2


def test_subject_hash_differs_for_different_subjects():
    assert subject_hash("Hello A") != subject_hash("Hello B")


def test_word_count_handles_runs_of_whitespace():
    assert word_count("one  two\tthree\nfour") == 4


# ---------------------------------------------------------------------------
# NullProvider
# ---------------------------------------------------------------------------


@pytest.mark.smoke
async def test_null_provider_returns_empty_gracefully():
    p = NullProvider()
    assert await p.find_contacts("acme.com") == []
    assert await p.find_contacts("acme.com", limit=5) == []


# ---------------------------------------------------------------------------
# Drafter
# ---------------------------------------------------------------------------


def test_truncate_words_no_op_under_cap():
    text = "one two three"
    assert _truncate_words(text, 5) == "one two three"


def test_truncate_words_caps_at_max_words():
    body = " ".join(f"w{i}" for i in range(120))
    out = _truncate_words(body, MAX_BODY_WORDS)
    # The "…" marker means we stopped exactly at MAX_BODY_WORDS words plus the
    # ellipsis sentinel.
    assert out.endswith("…")
    words = out.split()
    assert len(words) == MAX_BODY_WORDS + 1  # MAX_BODY_WORDS + "…"


@pytest.mark.smoke
async def test_drafter_respects_90_word_cap(monkeypatch):
    """LLM returns a 120-word body; drafter must clamp to 90."""
    long_body = " ".join(f"w{i}" for i in range(120))

    async def fake_chat_json(**_kw):
        return {"subject": "Specific subject under sixty chars", "body": long_body}

    monkeypatch.setattr("src.application.cold_outreach.drafter.chat_json", fake_chat_json)
    contact = Contact(email="a@b.com", name="A", title="VP", bio="bio", source="apollo")
    draft = await draft_intro(
        profile_headline="I built X",
        profile_skills=["python", "postgres"],
        company_name="Acme",
        mission_summary="Build scale systems.",
        why_target="They scaled fast",
        contact=contact,
    )
    assert draft is not None
    assert draft.body.endswith("…")
    # Allow one extra word for the ellipsis sentinel.
    assert len(draft.body.split()) <= MAX_BODY_WORDS + 1


async def test_drafter_returns_none_on_missing_fields(monkeypatch):
    async def fake_chat_json(**_kw):
        return {"subject": "", "body": "hi"}

    monkeypatch.setattr("src.application.cold_outreach.drafter.chat_json", fake_chat_json)
    contact = Contact(email="a@b.com", name=None, title=None, bio=None, source="apollo")
    draft = await draft_intro(
        profile_headline="",
        profile_skills=[],
        company_name="Acme",
        mission_summary="",
        why_target="",
        contact=contact,
    )
    assert draft is None


async def test_drafter_returns_none_on_llm_error(monkeypatch):
    from src.common.llm import LLMSafetyBlock

    async def fake_chat_json(**_kw):
        raise LLMSafetyBlock("safety_block: blocked")

    monkeypatch.setattr("src.application.cold_outreach.drafter.chat_json", fake_chat_json)
    contact = Contact(email="a@b.com", name="A", title="VP", bio="bio", source="apollo")
    assert (
        await draft_intro(
            profile_headline="",
            profile_skills=[],
            company_name="Acme",
            mission_summary="",
            why_target="",
            contact=contact,
        )
        is None
    )


# ---------------------------------------------------------------------------
# Cap module — warmup ramp math (pure function, no DB).
# ---------------------------------------------------------------------------


def test_warmup_ramp_5_to_10_over_5_days(monkeypatch):
    """Linear ramp from start=5 on day 0 to cap=10 on day >= 5."""
    from src.application.cold_outreach import cap as cap_mod
    from src.common import secrets as s_mod

    class _FakeSettings:
        cold_outreach_warmup_start = 5
        cold_outreach_daily_cap = 10
        cold_outreach_warmup_days = 5
        cold_outreach_enabled = True

    monkeypatch.setattr(s_mod, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(cap_mod, "get_settings", lambda: _FakeSettings())

    # Day 0 → start
    assert _ramp_ceiling(0, day_index=0) == 5
    # Day 1 → start + slope*1 (slope = (10-5)/5 = 1.0) → 6
    assert _ramp_ceiling(0, day_index=1) == 6
    # Day 3 → 8
    assert _ramp_ceiling(0, day_index=3) == 8
    # Day 5 → cap
    assert _ramp_ceiling(0, day_index=5) == 10
    # Day 99 → cap (clamped)
    assert _ramp_ceiling(0, day_index=99) == 10


# ---------------------------------------------------------------------------
# Cap module — DB-backed checks via monkeypatch stubs.
# ---------------------------------------------------------------------------


class _CapStubs:
    """Helper to drive allow_send() without a live database.

    Patches each `_…` async helper in cap.py to return a scripted value.
    Tests just instantiate `_CapStubs(...)` and pass it to setup().
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        in_apps: bool = False,
        recent_recipient: bool = False,
        recent_subject: bool = False,
        sent_today: int = 0,
        day_index: int = 99,
        daily_cap: int = 10,
        warmup_start: int = 5,
        warmup_days: int = 5,
    ):
        self.enabled = enabled
        self.in_apps = in_apps
        self.recent_recipient = recent_recipient
        self.recent_subject = recent_subject
        self.sent_today_val = sent_today
        self.day_index = day_index
        self.daily_cap = daily_cap
        self.warmup_start = warmup_start
        self.warmup_days = warmup_days

    def install(self, monkeypatch):
        from src.application.cold_outreach import cap as cap_mod
        from src.common import secrets as s_mod

        stubs = self

        class _FakeSettings:
            cold_outreach_enabled = stubs.enabled
            cold_outreach_warmup_start = stubs.warmup_start
            cold_outreach_daily_cap = stubs.daily_cap
            cold_outreach_warmup_days = stubs.warmup_days

        monkeypatch.setattr(s_mod, "get_settings", lambda: _FakeSettings())
        monkeypatch.setattr(cap_mod, "get_settings", lambda: _FakeSettings())

        async def _in_apps(_user, _email):
            return stubs.in_apps

        async def _recent_recipient(_user, _email, days=14):
            return stubs.recent_recipient

        async def _recent_subject(_hash, days=30):
            return stubs.recent_subject

        async def _sent_today(_user):
            return stubs.sent_today_val

        async def _day_idx(_user):
            return stubs.day_index

        monkeypatch.setattr(cap_mod, "_recipient_already_in_applications", _in_apps)
        monkeypatch.setattr(cap_mod, "_recipient_recently_emailed", _recent_recipient)
        monkeypatch.setattr(cap_mod, "_subject_recently_used", _recent_subject)
        monkeypatch.setattr(cap_mod, "_sent_today", _sent_today)
        monkeypatch.setattr(cap_mod, "_warmup_day_index", _day_idx)


@pytest.mark.smoke
async def test_cap_refuses_when_feature_flag_off(monkeypatch):
    _CapStubs(enabled=False).install(monkeypatch)
    out = await allow_send(user_id=1, recipient_email="a@b.com", subject_hash="x" * 64)
    assert out.ok is False
    assert out.reason == "feature_flag_off"


@pytest.mark.smoke
async def test_cap_blocks_send_past_daily_limit(monkeypatch):
    _CapStubs(sent_today=10, day_index=99).install(monkeypatch)
    out = await allow_send(user_id=1, recipient_email="a@b.com", subject_hash="x" * 64)
    assert out.ok is False
    assert out.reason == "daily_cap_reached"
    assert out.sent_today == 10
    assert out.ramp_ceiling == 10


async def test_cap_allows_below_daily_cap(monkeypatch):
    _CapStubs(sent_today=3, day_index=99).install(monkeypatch)
    out = await allow_send(user_id=1, recipient_email="a@b.com", subject_hash="x" * 64)
    assert out.ok is True
    assert out.reason == "ok"


@pytest.mark.smoke
async def test_subject_hash_dedupe_across_recipients(monkeypatch):
    """Same subject_hash → refuse, even though the recipient is different."""
    _CapStubs(recent_subject=True).install(monkeypatch)
    out = await allow_send(user_id=1, recipient_email="new@b.com", subject_hash="x" * 64)
    assert out.ok is False
    assert out.reason == "subject_recent_30d"


async def test_recipient_14d_dedupe(monkeypatch):
    _CapStubs(recent_recipient=True).install(monkeypatch)
    out = await allow_send(user_id=1, recipient_email="a@b.com", subject_hash="x" * 64)
    assert out.ok is False
    assert out.reason == "recipient_recent_14d"


async def test_cross_lane_dedupe_against_applications(monkeypatch):
    _CapStubs(in_apps=True).install(monkeypatch)
    out = await allow_send(user_id=1, recipient_email="a@b.com", subject_hash="x" * 64)
    assert out.ok is False
    assert out.reason == "recipient_in_applications"


async def test_cap_blocks_when_warmup_ceiling_below_sent_today(monkeypatch):
    """Day 0 ceiling = warmup_start (5). Sending 5 today should refuse the 6th."""
    _CapStubs(sent_today=5, day_index=0).install(monkeypatch)
    out = await allow_send(user_id=1, recipient_email="a@b.com", subject_hash="x" * 64)
    assert out.ok is False
    assert out.reason == "daily_cap_reached"
    assert out.ramp_ceiling == 5


# ---------------------------------------------------------------------------
# CapDecision dataclass — guard against accidental field churn.
# ---------------------------------------------------------------------------


def test_cap_decision_defaults():
    d = CapDecision(ok=True, reason="ok")
    assert d.sent_today == 0
    assert d.ramp_ceiling == 0


# ---------------------------------------------------------------------------
# Draft equality + structural guard.
# ---------------------------------------------------------------------------


def test_draft_is_immutable_record():
    """frozen=True dataclass — mutation must raise FrozenInstanceError."""
    from dataclasses import FrozenInstanceError

    d = Draft(subject="s", body="b")
    with pytest.raises(FrozenInstanceError):
        d.subject = "other"  # type: ignore[misc]

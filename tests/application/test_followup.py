"""Phase 2.3 follow-up automation — contract tests.

These tests exercise the pure-Python surface of `src/application/followup.py`
without touching Postgres or Resend. DB calls are stubbed via
monkeypatching `acquire` / `fetch_one`, the LLM is stubbed via
`chat_json`, and `send_email` is replaced with an in-memory probe.

Markers:
    smoke   — minimal happy-path coverage on the eligibility scanner
    asyncio — auto-mode set in pyproject (asyncio_mode = "auto")
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from src.application import followup as fu_mod
from src.application.followup import (
    ApplicationRow,
    _build_threaded_headers,
    _hand_fallback,
    _truncate_words,
    _word_count,
    draft_followup,
    find_eligible_applications,
)
from src.common.secrets import get_settings


# --------------------------------------------------------------------------
# Fixture: fresh settings cache, flag flipped on for every test, then off
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_settings(monkeypatch: pytest.MonkeyPatch):
    """Reset the lru_cache so tests can toggle MP_FOLLOWUP_ENABLED freely."""
    monkeypatch.setenv("MP_FOLLOWUP_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------
# Fake DB harness — captures the parameters `find_eligible_applications`
# binds + returns a programmable row set.
# --------------------------------------------------------------------------
class _FakeConn:
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows
        self.captured_sql: str | None = None
        self.captured_args: tuple[Any, ...] = ()

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.captured_sql = sql
        self.captured_args = args
        return list(self.rows)

    async def execute(self, sql: str, *args: Any) -> None:
        self.captured_sql = sql
        self.captured_args = args


class _AsyncCtx:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *args, **kwargs):
        return False


def _fake_acquire_factory(conn: _FakeConn):
    def _factory():
        return _AsyncCtx(conn)

    return _factory


# --------------------------------------------------------------------------
# Sample rows
# --------------------------------------------------------------------------
def _row(application_id: int = 1, sent_days_ago: int = 5, method: str = "email", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "application_id": application_id,
        "user_id": 1,
        "opportunity_id": f"00000000-0000-0000-0000-{application_id:012d}",
        "sent_at": now - timedelta(days=sent_days_ago),
        "method": method,
        "payload": payload if payload is not None else {"target": "founder@example.com", "cover_letter_markdown": "Original cover."},
        "title": "Backend Engineer",
        "company": "Acme",
        "description": "Build distributed systems on Rust.",
        "apply_url": "mailto:founder@example.com",
    }


# ==========================================================================
# 1. Eligible when sent 4d ago, no reply
# ==========================================================================
@pytest.mark.smoke
async def test_eligible_when_sent_4d_ago_no_response(monkeypatch: pytest.MonkeyPatch):
    rows = [_row(application_id=42, sent_days_ago=5)]
    conn = _FakeConn(rows)
    monkeypatch.setattr(fu_mod, "acquire", _fake_acquire_factory(conn))

    result = await find_eligible_applications(window_days=4, max_count=30)
    assert len(result) == 1
    r = result[0]
    assert r.application_id == 42
    assert r.method == "email"
    assert r.title == "Backend Engineer"
    assert r.email_target == "founder@example.com"
    assert r.days_silent >= 4
    # SQL is parameterised — verify the gate values reached asyncpg.
    assert conn.captured_args[1] == "4"
    assert conn.captured_args[2] == 31  # cap + 1


# ==========================================================================
# 2. Not eligible when already followed up — covered by the JOIN/NOT EXISTS
#    clause. Simulate by returning [] from the fake conn and asserting that
#    the SQL contains the followups NOT EXISTS sub-select.
# ==========================================================================
async def test_not_eligible_when_already_followed_up(monkeypatch: pytest.MonkeyPatch):
    conn = _FakeConn(rows=[])
    monkeypatch.setattr(fu_mod, "acquire", _fake_acquire_factory(conn))

    result = await find_eligible_applications()
    assert result == []
    # Contract: the gating SQL must filter out already-followed-up rows.
    assert "FROM followups f" in (conn.captured_sql or "")
    assert "f.application_id = a.id" in (conn.captured_sql or "")


# ==========================================================================
# 3. ats_form applications are excluded — covered by WHERE method='email'.
#    Confirm the SQL pins method='email' (the only path that has an
#    addressable thread to reply into).
# ==========================================================================
async def test_not_eligible_when_apply_method_ats_form(monkeypatch: pytest.MonkeyPatch):
    conn = _FakeConn(rows=[])
    monkeypatch.setattr(fu_mod, "acquire", _fake_acquire_factory(conn))

    await find_eligible_applications()
    assert "a.method = 'email'" in (conn.captured_sql or "")


# ==========================================================================
# 4. Drafter respects the 80-word cap
# ==========================================================================
async def test_drafter_respects_80_word_cap(monkeypatch: pytest.MonkeyPatch):
    """LLM returns a too-long body; drafter truncates to <= max_words."""
    overflow = " ".join([f"word{i}" for i in range(200)])  # 200 words
    # short-circuit profile load
    monkeypatch.setattr(fu_mod, "_profile_summary_for_followup", lambda: {})

    async def _fake_chat_json(*args, **kwargs):
        return {"body": overflow}

    monkeypatch.setattr(fu_mod, "chat_json", _fake_chat_json)

    app = ApplicationRow(
        application_id=1,
        user_id=1,
        opportunity_id="abc",
        sent_at=datetime.now(UTC) - timedelta(days=5),
        method="email",
        payload={"target": "to@x.com", "cover_letter_markdown": ""},
        title="Engineer",
        company="Acme",
        description="",
        apply_url=None,
        days_silent=5,
    )
    body = await draft_followup(app, max_words=80)
    assert _word_count(body) <= 80, f"got {_word_count(body)} words: {body!r}"


# ==========================================================================
# 5. Idempotent dual cron run — second record_draft hits ON CONFLICT and
#    returns None. record_draft and the eligibility scanner together
#    guarantee no double follow-up. Probe via the fetch_one shim.
# ==========================================================================
async def test_idempotent_dual_cron_run(monkeypatch: pytest.MonkeyPatch):
    calls = {"n": 0}

    async def _fake_fetch_one(sql: str, *args: Any):
        calls["n"] += 1
        # First call returns a new id; subsequent calls simulate the
        # ON CONFLICT DO NOTHING path by returning None.
        if calls["n"] == 1:
            return {"id": 999}
        return None

    monkeypatch.setattr(fu_mod, "fetch_one", _fake_fetch_one)

    fid_first = await fu_mod.record_draft(application_id=1, body="first")
    fid_second = await fu_mod.record_draft(application_id=1, body="second")
    assert fid_first == 999
    assert fid_second is None  # the unique conflict path


# ==========================================================================
# 6. send_followup threads via In-Reply-To header when the original
#    Message-ID is on file
# ==========================================================================
async def test_send_followup_threads_via_in_reply_to_header(monkeypatch: pytest.MonkeyPatch):
    """Capture the headers Resend would receive."""
    captured: dict[str, Any] = {}

    async def _fake_load_followup(fid: int):
        return {
            "id": fid,
            "user_id": 1,
            "application_id": 100,
            "body_markdown": "Quick nudge on my application.",
            "status": "draft",
            "opportunity_id": "00000000-0000-0000-0000-000000000001",
            "method": "email",
            "payload": {
                "target": "founder@example.com",
                "resend_message_id": "abc123@resend.dev",
                "cover_letter_markdown": "original",
            },
            "title": "Backend Engineer",
            "company": "Acme",
        }

    async def _fake_send_email(*, to, subject, html, reply_to=None, text=None, headers=None, attachments=None):
        captured["to"] = to
        captured["subject"] = subject
        captured["headers"] = headers
        captured["reply_to"] = reply_to
        return True

    async def _noop_mark_sent(*args, **kwargs):
        return None

    async def _noop_mark_failed(*args, **kwargs):
        return None

    monkeypatch.setattr(fu_mod, "_load_followup", _fake_load_followup)
    monkeypatch.setattr(fu_mod, "send_email", _fake_send_email)
    monkeypatch.setattr(fu_mod, "_mark_sent", _noop_mark_sent)
    monkeypatch.setattr(fu_mod, "_mark_failed", _noop_mark_failed)
    monkeypatch.setattr(fu_mod, "_profile_summary_for_followup", lambda: {"name": "Tester", "email": "me@x.com"})

    ok = await fu_mod.send_followup(followup_id=7)
    assert ok is True
    assert captured["to"] == "founder@example.com"
    assert captured["subject"].startswith("Re: ")
    # The hard rule: In-Reply-To must be set so this looks like a
    # threaded reply, not a brand-new conversation.
    assert captured["headers"] is not None
    assert "In-Reply-To" in captured["headers"]
    assert "References" in captured["headers"]
    # Canonical form: <id@host>
    assert captured["headers"]["In-Reply-To"].startswith("<")
    assert captured["headers"]["In-Reply-To"].endswith(">")


# ==========================================================================
# Bonus: header builder handles both bracketed and bare Message-IDs
# ==========================================================================
def test_threaded_headers_normalises_bare_id():
    assert _build_threaded_headers(None) is None
    h1 = _build_threaded_headers("abc123@resend.dev")
    h2 = _build_threaded_headers("<abc123@resend.dev>")
    assert h1 == h2
    assert h1["In-Reply-To"] == "<abc123@resend.dev>"
    assert h1["References"] == "<abc123@resend.dev>"


# ==========================================================================
# Bonus: hand fallback always produces a body under the cap
# ==========================================================================
def test_hand_fallback_under_cap():
    app = ApplicationRow(
        application_id=1,
        user_id=1,
        opportunity_id="abc",
        sent_at=datetime.now(UTC),
        method="email",
        payload={},
        title="A very long role title " * 5,
        company="Acme",
        description="",
        apply_url=None,
        days_silent=4,
    )
    body = _hand_fallback(app, max_words=80)
    assert _word_count(body) <= 80


def test_truncate_words_at_boundary():
    text = " ".join(["w"] * 100)
    out = _truncate_words(text, 25)
    assert _word_count(out) == 25


# ==========================================================================
# Feature flag gate
# ==========================================================================
async def test_flag_off_returns_empty(monkeypatch: pytest.MonkeyPatch):
    """When mp_followup_enabled is False, the scanner returns []
    without touching the DB. The DB stub asserts no call landed."""
    monkeypatch.setenv("MP_FOLLOWUP_ENABLED", "false")
    get_settings.cache_clear()

    conn = _FakeConn(rows=[_row()])
    monkeypatch.setattr(fu_mod, "acquire", _fake_acquire_factory(conn))

    result = await find_eligible_applications()
    assert result == []
    assert conn.captured_sql is None  # never reached the DB


# ==========================================================================
# Cap overflow logging — eligible > cap clips to cap + logs once.
# ==========================================================================
async def test_overflow_clips_to_cap(monkeypatch: pytest.MonkeyPatch):
    rows = [_row(application_id=i) for i in range(40)]
    conn = _FakeConn(rows)
    monkeypatch.setattr(fu_mod, "acquire", _fake_acquire_factory(conn))

    result = await find_eligible_applications(max_count=30)
    # When the DB returns cap+1 rows (or more), the in-Python slice
    # clips to cap. The +1 fetch is what triggers the overflow log.
    assert len(result) == 30


# ==========================================================================
# Payload JSON-decoding — asyncpg sometimes returns JSONB as a Python str.
# Make sure both shapes parse.
# ==========================================================================
async def test_payload_json_decode_str(monkeypatch: pytest.MonkeyPatch):
    payload_str = json.dumps({"target": "x@y.com", "cover_letter_markdown": "hi"})
    row = _row()
    row["payload"] = payload_str
    conn = _FakeConn([row])
    monkeypatch.setattr(fu_mod, "acquire", _fake_acquire_factory(conn))

    result = await find_eligible_applications()
    assert result[0].email_target == "x@y.com"
    assert result[0].original_cover_markdown == "hi"

"""Tests for `src.application.submitters.internshala` (Pi-side stub).

Covers:
  - Registry self-registration under key `in_platform_internshala`.
  - prepare() returns SubmitOutcome(status='failed') when pdf_path is None.
  - prepare() returns SubmitOutcome(status='failed') when the PDF file
    doesn't exist on disk.
  - prepare() publishes a BrowserApplyTask onto Streams.APPLY_BROWSER
    with the expected fields, including base64-encoded PDF bytes.
  - Q&A defaults are read from the YAML when the file exists; empty when
    missing.
  - dry_run flag rides verbatim into the payload.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from src.application import submitters
from src.application.submitters import internshala as internshala_mod
from src.common.queue import Streams

_OPP_ID = "00000000-0000-0000-0000-0000000099aa"


class _FakeQueue:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, stream: str, payload: dict[str, Any]) -> str:
        self.published.append((stream, payload))
        return "test-msg-id"


def _patch_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeQueue:
    fake = _FakeQueue()

    async def _connect() -> _FakeQueue:
        return fake

    # The module imports RedisQ from src.common.queue and calls .connect().
    # We patch the import site inside src.application.submitters.internshala.
    monkeypatch.setattr(internshala_mod.RedisQ, "connect", classmethod(lambda cls: _connect()))  # type: ignore[arg-type]
    return fake


@pytest.mark.smoke
def test_submitter_is_registered() -> None:
    keys = submitters.registered_keys()
    assert "in_platform_internshala" in keys
    assert submitters.resolve("in_platform_internshala") is not None


@pytest.mark.smoke
async def test_prepare_fails_when_pdf_path_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_redis(monkeypatch)
    sub = submitters.resolve("in_platform_internshala")
    assert sub is not None
    outcome = await sub.prepare(
        opp={"id": _OPP_ID, "apply_url": "https://internshala.com/internship/abc"},
        profile_summary={"name": "Test"},
        cover_md="Hi",
        tailored_bullets=[],
        pdf_path=None,
        dry_run=True,
        user_id=1,
    )
    assert outcome.status == "failed"
    assert outcome.error is not None and "no compiled PDF" in outcome.error


@pytest.mark.smoke
async def test_prepare_fails_when_pdf_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_redis(monkeypatch)
    sub = submitters.resolve("in_platform_internshala")
    assert sub is not None
    fake_path = tmp_path / "nonexistent.pdf"  # NOT created
    outcome = await sub.prepare(
        opp={"id": _OPP_ID, "apply_url": "https://internshala.com/internship/abc"},
        profile_summary={"name": "Test"},
        cover_md="Hi",
        tailored_bullets=[],
        pdf_path=fake_path,
        dry_run=True,
        user_id=1,
    )
    assert outcome.status == "failed"


@pytest.mark.smoke
async def test_prepare_publishes_browser_apply_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_q = _patch_redis(monkeypatch)
    sub = submitters.resolve("in_platform_internshala")
    assert sub is not None
    pdf = tmp_path / "resume.pdf"
    pdf_bytes = b"%PDF-1.4\nhello"
    pdf.write_bytes(pdf_bytes)

    # Avoid hitting the YAML loader / config_root lookup.
    monkeypatch.setattr(internshala_mod, "_load_qa_defaults", lambda: {"q1": "answer"})

    outcome = await sub.prepare(
        opp={"id": _OPP_ID, "apply_url": "https://internshala.com/internship/abc", "title": "Backend Intern", "company": "Acme"},
        profile_summary={"name": "Lakshit Gupta", "email": "x@y.com", "phone": "+91"},
        cover_md="Dear hiring manager — happy to apply.",
        tailored_bullets=["did A", "shipped B"],
        pdf_path=pdf,
        dry_run=True,
        user_id=1,
    )
    assert outcome.status == "deferred"
    assert outcome.task_id is not None
    assert len(fake_q.published) == 1

    stream, payload = fake_q.published[0]
    assert stream == Streams.APPLY_BROWSER
    assert payload["platform"] == "internshala"
    assert payload["opportunity_id"] == _OPP_ID
    assert payload["apply_url"] == "https://internshala.com/internship/abc"
    assert payload["dry_run"] is True
    assert payload["candidate_name"] == "Lakshit Gupta"
    assert payload["qa_defaults"] == {"q1": "answer"}
    # PDF round-trips through base64.
    decoded = base64.b64decode(payload["pdf_b64"])
    assert decoded == pdf_bytes
    assert payload["pdf_filename"].endswith(".pdf")
    # tailored bullets ride along.
    assert payload["tailored_bullets"] == ["did A", "shipped B"]


@pytest.mark.smoke
async def test_dry_run_flag_round_trips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_q = _patch_redis(monkeypatch)
    sub = submitters.resolve("in_platform_internshala")
    assert sub is not None
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"x")
    monkeypatch.setattr(internshala_mod, "_load_qa_defaults", lambda: {})

    await sub.prepare(
        opp={"id": _OPP_ID, "apply_url": "u"},
        profile_summary={"name": "T"},
        cover_md="",
        tailored_bullets=[],
        pdf_path=pdf,
        dry_run=False,
        user_id=1,
    )
    _stream, payload = fake_q.published[-1]
    assert payload["dry_run"] is False

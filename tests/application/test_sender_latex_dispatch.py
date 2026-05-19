"""Hermetic tests for ``src.application.sender_latex.dispatch``.

Coverage:
  - ``publish_notify`` payload contains NO ``pdf_path`` / ``resume_pdf_path``
    keys (CLAUDE.md hard rule #5). Pins so reintroduction fails the test.
  - notify_kind = "applied" for EMAIL; "manual_apply_ready" for non-EMAIL.
  - ``dispatch_email`` happy path: invokes ``send_email`` with the PDF
    attached for the EMAIL branch.
  - ``dispatch_email`` downgrades to EXTERNAL when no mailto target is
    discoverable; ``send_email`` is NOT called.
  - non-EMAIL methods set ``target`` to ``opp.apply_url``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from src.application.sender_latex import dispatch as dispatch_mod
from src.application.sender_latex.dispatch import dispatch_email, publish_notify
from src.common.types import ApplyMethod

_OPP_ID = UUID("00000000-0000-0000-0000-000000001234")


class _FakeQueue:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, stream: str, payload: dict[str, Any]) -> str:
        self.calls.append((stream, payload))
        return "0-0"


def _patch_queue(monkeypatch: pytest.MonkeyPatch) -> _FakeQueue:
    fake = _FakeQueue()

    async def _connect():
        return fake

    import src.common.queue as q_mod

    monkeypatch.setattr(q_mod.RedisQ, "connect", classmethod(lambda _cls: _connect()))
    return fake


def _scan_for_pdf_keys(payload: Any, path: str = "") -> list[str]:
    """Walk every nested key/value and surface any pdf-shaped entry."""
    offenders: list[str] = []
    if isinstance(payload, dict):
        for k, v in payload.items():
            kl = str(k).lower()
            if "pdf" in kl:
                offenders.append(f"{path}{k}")
            offenders.extend(_scan_for_pdf_keys(v, f"{path}{k}."))
    elif isinstance(payload, list):
        for i, v in enumerate(payload):
            offenders.extend(_scan_for_pdf_keys(v, f"{path}[{i}]."))
    return offenders


@pytest.mark.smoke
async def test_publish_notify_has_no_pdf_field(monkeypatch: pytest.MonkeyPatch):
    """CLAUDE.md hard rule #5: NOTIFY never carries a PDF path."""
    fake = _patch_queue(monkeypatch)
    await publish_notify(
        application_id=42,
        opp={"title": "Eng", "company": "Acme", "apply_url": "https://x"},
        opp_id=_OPP_ID,
        user_id=1,
        method=ApplyMethod.EMAIL,
        target="founder@example.com",
        cover_md="hi",
        tailored_bullets=["a", "b"],
        compile_status="tailored",
    )
    assert len(fake.calls) == 1
    _stream, payload = fake.calls[0]
    offenders = _scan_for_pdf_keys(payload)
    assert offenders == [], f"PDF keys leaked into NOTIFY payload: {offenders}"


@pytest.mark.smoke
async def test_publish_notify_uses_applied_kind_for_email(
    monkeypatch: pytest.MonkeyPatch,
):
    fake = _patch_queue(monkeypatch)
    await publish_notify(
        application_id=1,
        opp={"title": "t", "company": "c"},
        opp_id=_OPP_ID,
        user_id=1,
        method=ApplyMethod.EMAIL,
        target="a@b.c",
        cover_md="",
        tailored_bullets=[],
        compile_status="tailored",
    )
    _stream, payload = fake.calls[0]
    assert payload["kind"] == "applied"
    assert payload["payload"]["method"] == "email"


async def test_publish_notify_uses_manual_kind_for_non_email(
    monkeypatch: pytest.MonkeyPatch,
):
    fake = _patch_queue(monkeypatch)
    await publish_notify(
        application_id=1,
        opp={"title": "t", "company": "c"},
        opp_id=_OPP_ID,
        user_id=1,
        method=ApplyMethod.EXTERNAL,
        target="https://apply.example.com",
        cover_md="",
        tailored_bullets=[],
        compile_status="fallback",
    )
    _stream, payload = fake.calls[0]
    assert payload["kind"] == "manual_apply_ready"
    assert payload["payload"]["method"] == "external"


async def test_dispatch_email_sends_with_pdf_attachment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pdf = tmp_path / "out.pdf"
    pdf.write_bytes(b"%PDF-fake")

    captured: list[dict[str, Any]] = []

    async def _fake_send(**kwargs: Any) -> bool:
        captured.append(kwargs)
        return True

    monkeypatch.setattr(dispatch_mod, "send_email", _fake_send)

    method, target = await dispatch_email(
        {
            "apply_method": "email",
            "apply_url": "mailto:founder@example.com",
            "title": "Eng",
            "description": "",
        },
        cover_md="hi",
        tailored_bullets=["bullet"],
        profile_summary={"name": "Me", "email": "me@x"},
        pdf_path=pdf,
        opp_id=_OPP_ID,
    )
    assert method == ApplyMethod.EMAIL
    assert target == "founder@example.com"
    assert len(captured) == 1
    assert captured[0]["attachments"] == [pdf]
    assert captured[0]["to"] == "founder@example.com"


async def test_dispatch_email_downgrades_to_external_when_no_mailto(
    monkeypatch: pytest.MonkeyPatch,
):
    called = {"v": False}

    async def _fake_send(**_k: Any) -> bool:
        called["v"] = True
        return True

    monkeypatch.setattr(dispatch_mod, "send_email", _fake_send)

    method, target = await dispatch_email(
        {
            "apply_method": "email",
            "apply_url": "https://no-mailto.example.com",
            "title": "Eng",
            "description": "apply on our site, no email",
        },
        cover_md="",
        tailored_bullets=[],
        profile_summary={"name": "Me"},
        pdf_path=None,
        opp_id=_OPP_ID,
    )
    assert method == ApplyMethod.EXTERNAL
    assert target == "https://no-mailto.example.com"
    assert called["v"] is False  # send_email never invoked


async def test_dispatch_email_non_email_method_sets_apply_url_target(
    monkeypatch: pytest.MonkeyPatch,
):
    """For ATS_FORM / EXTERNAL / IN_PLATFORM, target == opp.apply_url."""
    sent = {"v": False}

    async def _fake_send(**_k: Any) -> bool:
        sent["v"] = True
        return True

    monkeypatch.setattr(dispatch_mod, "send_email", _fake_send)

    method, target = await dispatch_email(
        {
            "apply_method": "ats_form",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/123",
        },
        cover_md="",
        tailored_bullets=[],
        profile_summary={},
        pdf_path=None,
        opp_id=_OPP_ID,
    )
    assert method == ApplyMethod.ATS_FORM
    assert target == "https://boards.greenhouse.io/acme/jobs/123"
    assert sent["v"] is False


async def test_dispatch_email_swallows_send_exceptions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A Resend failure must NOT raise — apply still records as EMAIL."""
    pdf = tmp_path / "out.pdf"
    pdf.write_bytes(b"%PDF-fake")

    async def _boom(**_k: Any) -> bool:
        raise RuntimeError("resend 503")

    monkeypatch.setattr(dispatch_mod, "send_email", _boom)

    method, target = await dispatch_email(
        {"apply_method": "email", "apply_url": "mailto:x@y.z"},
        cover_md="",
        tailored_bullets=[],
        profile_summary={"name": "Me"},
        pdf_path=pdf,
        opp_id=_OPP_ID,
    )
    assert method == ApplyMethod.EMAIL
    assert target == "x@y.z"

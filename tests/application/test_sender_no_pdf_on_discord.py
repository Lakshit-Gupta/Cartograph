"""Hard-rule pinning test: PDF NEVER reaches Discord.

CLAUDE.md hard rule #5: ``PDF NEVER posted to Discord channel. Email
attachment only.`` This file is the canary — it constructs realistic
NOTIFY payloads through every code path that can reach ``publish_notify``
(both ``applied`` and ``manual_apply_ready`` kinds) and fails LOUDLY
if any key/value looks like a PDF path.

The scan walks the payload recursively and flags ANY key containing
``pdf`` (case-insensitive) and ANY string value ending in ``.pdf``. If
this test fires, a regression was introduced — check
``src.application.sender_latex.dispatch.publish_notify``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from src.application.sender_latex import dispatch as dispatch_mod
from src.application.sender_latex.dispatch import publish_notify
from src.common.types import ApplyMethod

_OPP_ID = UUID("00000000-0000-0000-0000-00000000babe")


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


_PDF_KEY_HINTS = ("pdf", "attachment", "resume_path", "artifact_path")


def _walk_for_pdf(payload: Any, prefix: str = "") -> list[str]:
    """Return every key/value path that looks like a PDF leak."""
    offenders: list[str] = []
    if isinstance(payload, dict):
        for k, v in payload.items():
            kl = str(k).lower()
            for hint in _PDF_KEY_HINTS:
                if hint in kl:
                    offenders.append(f"key:{prefix}{k}")
                    break
            offenders.extend(_walk_for_pdf(v, f"{prefix}{k}."))
    elif isinstance(payload, list):
        for i, v in enumerate(payload):
            offenders.extend(_walk_for_pdf(v, f"{prefix}[{i}]."))
    elif isinstance(payload, str):
        if payload.lower().endswith(".pdf"):
            offenders.append(f"value:{prefix} ({payload!r})")
    elif isinstance(payload, Path):
        # Path stringified anywhere in payload — always treat as a leak.
        offenders.append(f"path-value:{prefix} ({payload!s})")
    return offenders


@pytest.mark.smoke
async def test_no_pdf_in_applied_payload(monkeypatch: pytest.MonkeyPatch):
    """EMAIL → 'applied' kind. PDF stays out of the NOTIFY payload."""
    fake = _patch_queue(monkeypatch)
    await publish_notify(
        application_id=1,
        opp={
            "title": "Backend Eng",
            "company": "Acme",
            "apply_url": "mailto:founder@example.com",
        },
        opp_id=_OPP_ID,
        user_id=1,
        method=ApplyMethod.EMAIL,
        target="founder@example.com",
        cover_md="Cover.",
        tailored_bullets=["bullet"],
        compile_status="tailored",
    )
    _stream, payload = fake.calls[0]
    offenders = _walk_for_pdf(payload)
    assert offenders == [], f"REGRESSION: CLAUDE.md hard rule #5 violated — PDF leaked into NOTIFY payload: {offenders}"


@pytest.mark.smoke
async def test_no_pdf_in_manual_apply_payload(monkeypatch: pytest.MonkeyPatch):
    """Non-EMAIL → 'manual_apply_ready' kind. PDF stays out too."""
    fake = _patch_queue(monkeypatch)
    await publish_notify(
        application_id=1,
        opp={
            "title": "Backend Eng",
            "company": "Acme",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
        },
        opp_id=_OPP_ID,
        user_id=1,
        method=ApplyMethod.EXTERNAL,
        target="https://boards.greenhouse.io/acme/jobs/1",
        cover_md="Cover.",
        tailored_bullets=["bullet"],
        compile_status="fallback",
    )
    _stream, payload = fake.calls[0]
    offenders = _walk_for_pdf(payload)
    assert offenders == [], f"REGRESSION: CLAUDE.md hard rule #5 violated — PDF leaked into manual-apply NOTIFY payload: {offenders}"


@pytest.mark.smoke
async def test_no_pdf_when_cover_md_mentions_pdf_filename(
    monkeypatch: pytest.MonkeyPatch,
):
    """Cover letter may legitimately mention 'resume.pdf' in body text.

    Pin that we don't accidentally accept ``cover_md`` ending in ``.pdf``
    as a leak — but if a future change names a *key* with 'pdf', this still
    fires. This test exists so the scanner is calibrated.
    """
    fake = _patch_queue(monkeypatch)
    await publish_notify(
        application_id=1,
        opp={"title": "t", "company": "c"},
        opp_id=_OPP_ID,
        user_id=1,
        method=ApplyMethod.EMAIL,
        target="a@b.c",
        cover_md="See attached resume PDF. Sincerely.",  # no .pdf suffix
        tailored_bullets=[],
        compile_status="tailored",
    )
    _stream, payload = fake.calls[0]
    assert _walk_for_pdf(payload) == []


async def test_scanner_catches_synthetic_pdf_key():
    """Sanity-check: the scanner DOES flag a payload with a 'pdf_path' key.

    If this test ever fails, the scanner is broken and the other tests
    in this file are giving false negatives.
    """
    bad = {"payload": {"resume_pdf_path": "/tmp/x.pdf"}}
    offenders = _walk_for_pdf(bad)
    assert any("pdf_path" in o for o in offenders)
    assert any(".pdf" in o for o in offenders)


async def test_scanner_catches_path_object_in_payload():
    """Sanity-check: Path objects nested anywhere are flagged."""
    bad = {"payload": {"attached": Path("/tmp/x.pdf")}}
    offenders = _walk_for_pdf(bad)
    assert offenders, "scanner should flag Path-typed values"


async def test_dispatch_email_does_not_leak_pdf_into_notify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Even when a PDF is attached to the email, NOTIFY stays clean.

    Exercises the full EMAIL branch via dispatch_email → publish_notify.
    """
    pdf = tmp_path / "out.pdf"
    pdf.write_bytes(b"%PDF-fake")

    async def _fake_send(**_k: Any) -> bool:
        return True

    monkeypatch.setattr(dispatch_mod, "send_email", _fake_send)
    fake_q = _patch_queue(monkeypatch)

    from src.application.sender_latex.dispatch import dispatch_email

    method, target = await dispatch_email(
        {"apply_method": "email", "apply_url": "mailto:x@y.z"},
        cover_md="cover",
        tailored_bullets=["b"],
        profile_summary={"name": "Me"},
        pdf_path=pdf,
        opp_id=_OPP_ID,
    )
    assert method == ApplyMethod.EMAIL
    # Now invoke publish_notify with the EXACT pdf_path-aware return —
    # publish_notify doesn't take pdf_path so the leak guard is enforced.
    await publish_notify(
        application_id=1,
        opp={"title": "t", "company": "c"},
        opp_id=_OPP_ID,
        user_id=1,
        method=method,
        target=target,
        cover_md="cover",
        tailored_bullets=["b"],
        compile_status="tailored",
    )
    _stream, payload = fake_q.calls[0]
    assert _walk_for_pdf(payload) == []

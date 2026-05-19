"""Hermetic tests for ``src.application.sender_latex.pipeline.send_with_latex``.

Coverage:
  - call order is prepare_blocks → compile_with_fallback → write_cover →
    dispatch_email → record_application → publish_notify.
  - 'tailored' branch: payload carries compile_status='tailored' + sha.
  - 'fallback' branch: payload carries compile_status='fallback' + sha.
  - 'failed' branch: payload carries 'failed' status, sha=None.
  - ``override_cover_markdown`` is used verbatim when given.

Every phase + dispatch hop is mocked so the test exercises only the
orchestrator's gluing logic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from src.application.sender_latex import pipeline as pipeline_mod
from src.application.sender_latex.phases import CompileOutcome, PreparedBlocks
from src.application.sender_latex.pipeline import send_with_latex
from src.common.types import ApplyMethod

_OPP_ID = UUID("00000000-0000-0000-0000-000000009999")


class _FakeDoc:
    def __init__(self):
        self.blocks: list[Any] = []
        self.files = {"mmayer.tex": ""}
        self.source_hashes = {"mmayer.tex": "src-hash"}


def _prepared() -> PreparedBlocks:
    return PreparedBlocks(
        document=_FakeDoc(),
        manifest=object(),
        top_blocks=[],
        sanitized_edits={"b1": ["one"]},
        sanitizer_reject_msg=None,
        variant_label="backend",
        resume_variant_id_db=1,
        template_name="default",
    )


def _outcome(status: str, pdf: Path | None) -> CompileOutcome:
    sha = "sha-art" if status != "failed" else None
    return CompileOutcome(
        pdf_path=pdf,
        compile_status=status,
        artifact_sha256=sha,
        source_hash="src-hash",
    )


def _patch_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    outcome: CompileOutcome,
    cover_md: str = "Dear team,",
    apply_method: ApplyMethod = ApplyMethod.EMAIL,
    target: str | None = "founder@example.com",
) -> dict[str, Any]:
    """Stub every collaborator pipeline.send_with_latex touches."""
    import src.application.cover_letter as cl_mod
    import src.application.sender as sender_mod

    order: list[str] = []
    captured: dict[str, Any] = {"order": order, "notify_payload": None}

    def _mark(name: str, retval: Any):
        async def _fn(*_a: Any, **_k: Any):
            order.append(name)
            return retval

        return _fn

    async def _fake_notify(**kwargs: Any):
        order.append("notify")
        captured["notify_payload"] = kwargs

    monkeypatch.setattr(pipeline_mod, "prepare_blocks", _mark("prepare", _prepared()))
    monkeypatch.setattr(pipeline_mod, "compile_with_fallback", _mark("compile", outcome))
    monkeypatch.setattr(pipeline_mod, "dispatch_email", _mark("dispatch", (apply_method, target)))
    monkeypatch.setattr(pipeline_mod, "record_application", _mark("record", 4242))
    monkeypatch.setattr(pipeline_mod, "publish_notify", _fake_notify)
    monkeypatch.setattr(cl_mod, "write_cover", _mark("cover", cover_md))
    monkeypatch.setattr(cl_mod, "pick_template", lambda _o, variant_label=None: "default")
    monkeypatch.setattr(sender_mod, "_resume_root", lambda: Path("/tmp/fake-root"))
    monkeypatch.setattr(sender_mod, "_manifest_path", lambda: Path("/tmp/fake-root/manifest.yaml"))
    return captured


def _opp() -> dict[str, Any]:
    return {
        "title": "Engineer",
        "company": "Acme",
        "apply_method": "email",
        "apply_url": "mailto:founder@example.com",
        "description": "",
    }


@pytest.mark.smoke
async def test_pipeline_runs_phases_in_order_tailored_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pdf = tmp_path / "out.pdf"
    pdf.write_bytes(b"%PDF")
    captured = _patch_pipeline(monkeypatch, outcome=_outcome("tailored", pdf))

    result = await send_with_latex(_OPP_ID, _opp(), {}, {"name": "Me"}, {}, user_id=1)
    assert captured["order"] == [
        "prepare",
        "compile",
        "cover",
        "dispatch",
        "record",
        "notify",
    ]
    assert result["application_id"] == 4242
    assert result["resume_compile_status"] == "tailored"
    assert result["resume_artifact_sha256"] == "sha-art"
    assert result["method"] == "email"
    assert result["cover_letter_markdown"] == "Dear team,"


async def test_pipeline_fallback_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pdf = tmp_path / "fb.pdf"
    pdf.write_bytes(b"%PDF")
    captured = _patch_pipeline(monkeypatch, outcome=_outcome("fallback", pdf))

    result = await send_with_latex(_OPP_ID, _opp(), {}, {"name": "Me"}, {}, user_id=1)
    assert result["resume_compile_status"] == "fallback"
    assert result["resume_artifact_sha256"] == "sha-art"
    np = captured["notify_payload"]
    assert np["compile_status"] == "fallback"


async def test_pipeline_failed_branch_carries_none_sha(
    monkeypatch: pytest.MonkeyPatch,
):
    captured = _patch_pipeline(monkeypatch, outcome=_outcome("failed", None))

    result = await send_with_latex(_OPP_ID, _opp(), {}, {"name": "Me"}, {}, user_id=1)
    assert result["resume_compile_status"] == "failed"
    assert result["resume_artifact_sha256"] is None
    np = captured["notify_payload"]
    assert np["compile_status"] == "failed"


async def test_pipeline_uses_override_cover_when_given(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF")
    captured = _patch_pipeline(monkeypatch, outcome=_outcome("tailored", pdf))

    result = await send_with_latex(
        _OPP_ID,
        _opp(),
        {},
        {"name": "Me"},
        {},
        user_id=1,
        override_cover_markdown="VERBATIM cover.",
    )
    # write_cover stub returned "Dear team," — override must win and skip
    # the stub entirely, so "cover" must NOT appear in order.
    assert "cover" not in captured["order"]
    assert result["cover_letter_markdown"] == "VERBATIM cover."


async def test_pipeline_emits_user_id_on_notify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF")
    captured = _patch_pipeline(monkeypatch, outcome=_outcome("tailored", pdf))

    await send_with_latex(_OPP_ID, _opp(), {}, {}, {}, user_id=99)
    assert captured["notify_payload"]["user_id"] == 99


async def test_pipeline_non_email_method_propagates_through_result(
    monkeypatch: pytest.MonkeyPatch,
):
    captured = _patch_pipeline(
        monkeypatch,
        outcome=_outcome("failed", None),
        apply_method=ApplyMethod.EXTERNAL,
        target="https://apply.example.com",
    )
    result = await send_with_latex(
        _OPP_ID,
        {**_opp(), "apply_method": "external"},
        {},
        {},
        {},
        user_id=1,
    )
    assert result["method"] == "external"
    assert result["target"] == "https://apply.example.com"
    assert captured["notify_payload"]["method"] == ApplyMethod.EXTERNAL


@pytest.mark.smoke
async def test_pipeline_compile_uses_resolved_user_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """compile_with_fallback is invoked with the caller-supplied user_id."""
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF")
    _patch_pipeline(monkeypatch, outcome=_outcome("tailored", pdf))

    seen: dict[str, Any] = {}

    async def _capturing_compile(_blocks, _opp_id, user_id, *, source_root):
        _ = source_root
        seen["user_id"] = user_id
        return _outcome("tailored", pdf)

    monkeypatch.setattr(pipeline_mod, "compile_with_fallback", _capturing_compile)
    await send_with_latex(_OPP_ID, _opp(), {}, {}, {}, user_id=7)
    assert seen["user_id"] == 7

"""Hermetic tests for ``src.application.sender_latex.phases``.

Coverage:
  - ``compile_with_fallback`` happy 'tailored' path.
  - tectonic ``CompileError`` → 'fallback'.
  - ``SourceDriftError`` → 'fallback' (with ``source_drift:`` stderr).
  - unexpected exception → 'fallback' with ``render_error:`` stderr.
  - no fallback PDF available → 'failed'.
  - ``log_compile_outcome`` is invoked with the right status + sha256.
  - ``collect_surface_bullets`` honours the 5-bullet cap.

``render_and_compile`` + ``resume_latex.fallback.get_fallback`` are mocked.
The audit insert (``acquire``/``log_compile_outcome``) is no-oped so we
never touch Postgres.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from src.application.sender_latex import phases as phases_mod
from src.application.sender_latex.phases import (
    PreparedBlocks,
    collect_surface_bullets,
    compile_with_fallback,
)

_OPP_ID = UUID("00000000-0000-0000-0000-000000000abc")


@dataclass
class _FakeDoc:
    files: dict[str, str]
    source_hashes: dict[str, str]
    blocks: list[Any]

    def __init__(self):
        self.files = {"mmayer.tex": "% src\n"}
        self.source_hashes = {"mmayer.tex": "sha-fake"}
        self.blocks = []


@dataclass
class _FakeBlock:
    id: str
    kind: str
    title: str
    bullets: list[str]


def _prepared(edits: dict[str, list[str]] | None = None) -> PreparedBlocks:
    return PreparedBlocks(
        document=_FakeDoc(),
        manifest=object(),
        top_blocks=[
            _FakeBlock(id="b1", kind="event", title="x", bullets=["a"]),
            _FakeBlock(id="b2", kind="project", title="y", bullets=["b"]),
        ],
        sanitized_edits=edits or {"b1": ["new a"]},
        sanitizer_reject_msg=None,
        variant_label="backend",
        resume_variant_id_db=1,
        template_name="default",
    )


@pytest.fixture(autouse=True)
def _isolate_filesystem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect the artifact root to a tmpdir so mkdir is safe."""
    monkeypatch.setattr(phases_mod, "_ARTIFACT_ROOT", tmp_path / "artifacts")
    yield


@pytest.fixture
def _stub_audit(monkeypatch: pytest.MonkeyPatch):
    """No-op the DB-touching audit insert."""
    captured: dict[str, Any] = {}

    async def _fake_log(audit) -> None:
        captured["audit"] = audit

    monkeypatch.setattr(phases_mod, "log_compile_outcome", _fake_log)
    return captured


def _write_dummy_pdf(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"%PDF-fake")
    return p


@pytest.mark.smoke
async def test_compile_with_fallback_happy_tailored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stub_audit):
    pdf = _write_dummy_pdf(tmp_path / "out.pdf")

    @dataclass
    class _Success:
        pdf_path: Path
        duration_ms: int | None
        tectonic_version: str | None

    async def _fake_render(*_a: Any, **_k: Any):
        return _Success(pdf_path=pdf, duration_ms=1234, tectonic_version="tectonic 0.16")

    monkeypatch.setattr(phases_mod, "render_and_compile", _fake_render)

    outcome = await compile_with_fallback(_prepared(), _OPP_ID, user_id=1, source_root=tmp_path / "src")
    assert outcome.compile_status == "tailored"
    assert outcome.pdf_path == pdf
    assert outcome.artifact_sha256 == hashlib.sha256(b"%PDF-fake").hexdigest()
    assert outcome.source_hash == "sha-fake"
    assert _stub_audit["audit"].status == "tailored"
    assert _stub_audit["audit"].tectonic_version == "tectonic 0.16"


async def test_compile_with_fallback_tectonic_failure_drops_to_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stub_audit):
    from src.application.resume_latex.compile import CompileError

    async def _boom(*_a: Any, **_k: Any):
        raise CompileError("tectonic exited 1")

    monkeypatch.setattr(phases_mod, "render_and_compile", _boom)

    fb_pdf = _write_dummy_pdf(tmp_path / "fb.pdf")

    import src.application.resume_latex.fallback as fb_mod

    monkeypatch.setattr(fb_mod, "get_fallback", lambda _u, variant_label=None: fb_pdf)

    outcome = await compile_with_fallback(_prepared(), _OPP_ID, user_id=1, source_root=tmp_path)
    assert outcome.compile_status == "fallback"
    assert outcome.pdf_path == fb_pdf
    assert _stub_audit["audit"].status == "fallback"
    assert "tectonic exited 1" in (_stub_audit["audit"].tectonic_stderr or "")


async def test_compile_with_fallback_source_drift_drops_to_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stub_audit):
    """Pins CLAUDE.md hard rule #6: source-drift triggers fallback, not raise."""
    from src.application.resume_latex.render import SourceDriftError

    async def _drift(*_a: Any, **_k: Any):
        raise SourceDriftError("hash changed on disk")

    monkeypatch.setattr(phases_mod, "render_and_compile", _drift)

    fb_pdf = _write_dummy_pdf(tmp_path / "fb.pdf")
    import src.application.resume_latex.fallback as fb_mod

    monkeypatch.setattr(fb_mod, "get_fallback", lambda _u, variant_label=None: fb_pdf)

    outcome = await compile_with_fallback(_prepared(), _OPP_ID, user_id=1, source_root=tmp_path)
    assert outcome.compile_status == "fallback"
    assert _stub_audit["audit"].tectonic_stderr.startswith("source_drift:")


async def test_compile_with_fallback_unexpected_exception_drops_to_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stub_audit):
    async def _explode(*_a: Any, **_k: Any):
        raise ValueError("never seen this before")

    monkeypatch.setattr(phases_mod, "render_and_compile", _explode)

    fb_pdf = _write_dummy_pdf(tmp_path / "fb.pdf")
    import src.application.resume_latex.fallback as fb_mod

    monkeypatch.setattr(fb_mod, "get_fallback", lambda _u, variant_label=None: fb_pdf)

    outcome = await compile_with_fallback(_prepared(), _OPP_ID, user_id=1, source_root=tmp_path)
    assert outcome.compile_status == "fallback"
    assert "render_error:" in (_stub_audit["audit"].tectonic_stderr or "")


async def test_compile_with_fallback_failed_when_no_fallback_pdf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stub_audit):
    """No tailored PDF and no warm fallback → status='failed', pdf=None."""
    from src.application.resume_latex.compile import CompileError

    async def _boom(*_a: Any, **_k: Any):
        raise CompileError("nope")

    monkeypatch.setattr(phases_mod, "render_and_compile", _boom)

    import src.application.resume_latex.fallback as fb_mod

    monkeypatch.setattr(fb_mod, "get_fallback", lambda _u, variant_label=None: None)

    outcome = await compile_with_fallback(_prepared(), _OPP_ID, user_id=1, source_root=tmp_path)
    assert outcome.compile_status == "failed"
    assert outcome.pdf_path is None
    assert outcome.artifact_sha256 is None
    assert _stub_audit["audit"].status == "failed"


def test_collect_surface_bullets_caps_at_five():
    """``_BULLET_SURFACE_CAP`` is 5 — extras are discarded."""
    blocks = [_FakeBlock(id=f"b{i}", kind="event", title=str(i), bullets=[]) for i in range(3)]
    edits = {
        "b0": ["x1", "x2", "x3"],
        "b1": ["y1", "y2"],
        "b2": ["z1"],
    }
    out = collect_surface_bullets(blocks, edits)
    assert len(out) == 5
    assert out == ["x1", "x2", "x3", "y1", "y2"]


def test_collect_surface_bullets_skips_blocks_without_edits():
    blocks = [
        _FakeBlock(id="b1", kind="event", title="t", bullets=[]),
        _FakeBlock(id="b2", kind="event", title="t", bullets=[]),
    ]
    out = collect_surface_bullets(blocks, {"b2": ["only b2"]})
    assert out == ["only b2"]

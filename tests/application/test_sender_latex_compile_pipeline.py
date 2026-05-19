"""Hermetic tests for ``src.application.sender_latex.compile_pipeline``.

Coverage:
  - fs happy path — write_partial → tectonic stub → commit_complete → resolve PDF.
  - atomic ``.partial → .complete`` rename: ``.partial`` gone, ``.complete`` exists.
  - variant overlay: when ``manifest.variants[label]`` points at a real file,
    the overlay flattens it into ``partial/main.tex``.
  - missing PDF on success path raises ``CompileError`` (caller falls back).

``compile.run`` is mocked — no tectonic. ``write_partial`` & ``commit_complete``
are stubbed too so the test stays under 250 LOC and doesn't depend on the
real parser-document shape.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from src.application.sender_latex.compile_pipeline import render_and_compile

_OPP_ID = UUID("00000000-0000-0000-0000-000000000def")


@dataclass
class _FakeManifest:
    main_file: str = "mmayer.tex"
    variants: dict[str, str] | None = None


@dataclass
class _FakeDoc:
    blocks: list[Any]
    files: dict[str, str]
    source_hashes: dict[str, str]


def _doc() -> _FakeDoc:
    return _FakeDoc(
        blocks=[],
        files={"mmayer.tex": "\\documentclass{altacv}\n"},
        source_hashes={"mmayer.tex": "h"},
    )


@dataclass
class _FakeCompileResult:
    duration_ms: int
    tectonic_version: str


def _stub_write_partial(monkeypatch: pytest.MonkeyPatch, artifact_dir: Path) -> Path:
    """Create the partial dir and main file, return its path."""
    partial = artifact_dir.with_suffix(".partial")
    partial.mkdir(parents=True, exist_ok=True)
    (partial / "mmayer.tex").write_text("\\documentclass{altacv}\n", encoding="utf-8")

    def _fake(*_a: Any, **_k: Any) -> Path:
        return partial

    # write_partial is imported lazily inside _write_partial_tree
    import src.application.resume_latex.render as render_mod

    monkeypatch.setattr(render_mod, "write_partial", _fake)
    return partial


def _stub_commit_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real-os rename so the atomic semantics are observable."""

    def _fake(partial: Path) -> Path:
        target = partial.with_suffix(".complete")
        if target.exists():
            import shutil

            shutil.rmtree(target)
        os.rename(partial, target)
        # tectonic would have created the PDF inside the dir; emulate that.
        (target / "mmayer.pdf").write_bytes(b"%PDF-fake")
        return target

    import src.application.resume_latex.render as render_mod

    monkeypatch.setattr(render_mod, "commit_complete", _fake)


def _stub_compile_run(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(_main: Path) -> _FakeCompileResult:
        return _FakeCompileResult(duration_ms=42, tectonic_version="tectonic 0.16")

    import src.application.resume_latex.compile as compile_mod

    monkeypatch.setattr(compile_mod, "run", _fake)


@pytest.mark.smoke
async def test_render_and_compile_happy_path_writes_pdf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    artifact_dir = tmp_path / "art" / "opp"
    _stub_write_partial(monkeypatch, artifact_dir)
    _stub_commit_complete(monkeypatch)
    _stub_compile_run(monkeypatch)

    result = await render_and_compile(
        _doc(),
        {},
        artifact_dir,
        source_root=tmp_path / "src",
        manifest=_FakeManifest(),
        variant_label="backend",
        opp_id=_OPP_ID,
    )
    assert result.pdf_path.exists()
    assert result.pdf_path.name == "mmayer.pdf"
    assert result.duration_ms == 42
    assert result.tectonic_version == "tectonic 0.16"


async def test_render_and_compile_atomic_partial_to_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Partial dir must vanish after commit; complete dir must exist."""
    artifact_dir = tmp_path / "art" / "opp"
    partial = _stub_write_partial(monkeypatch, artifact_dir)
    _stub_commit_complete(monkeypatch)
    _stub_compile_run(monkeypatch)

    await render_and_compile(
        _doc(),
        {},
        artifact_dir,
        source_root=tmp_path / "src",
        manifest=_FakeManifest(),
        variant_label="backend",
        opp_id=_OPP_ID,
    )
    assert not partial.exists(), "partial directory should be renamed away"
    complete = artifact_dir.with_suffix(".complete")
    assert complete.is_dir()


async def test_render_and_compile_raises_when_no_pdf_produced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Promoted ``.complete/`` dir without any .pdf → ``CompileError``."""
    from src.application.resume_latex.compile import CompileError

    artifact_dir = tmp_path / "art" / "opp"
    _stub_write_partial(monkeypatch, artifact_dir)

    def _empty_commit(partial: Path) -> Path:
        target = partial.with_suffix(".complete")
        os.rename(partial, target)  # no PDF written
        return target

    import src.application.resume_latex.render as render_mod

    monkeypatch.setattr(render_mod, "commit_complete", _empty_commit)
    _stub_compile_run(monkeypatch)

    with pytest.raises(CompileError, match=r"no \.pdf"):
        await render_and_compile(
            _doc(),
            {},
            artifact_dir,
            source_root=tmp_path / "src",
            manifest=_FakeManifest(),
            variant_label="backend",
            opp_id=_OPP_ID,
        )


async def test_render_and_compile_variant_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """When manifest has a variant override, the overlay overwrites main.tex."""
    source_root = tmp_path / "src"
    source_root.mkdir()
    variant_path = source_root / "variants" / "backend" / "main.tex"
    variant_path.parent.mkdir(parents=True)
    variant_path.write_text("% variant-only content\n\\documentclass{altacv}\n", encoding="utf-8")

    artifact_dir = tmp_path / "art" / "opp"
    partial = _stub_write_partial(monkeypatch, artifact_dir)
    _stub_commit_complete(monkeypatch)
    _stub_compile_run(monkeypatch)

    # Stub the variant flattening so we never need a real \input pass.
    import src.application.resume_latex.fallback as fb_mod

    def _flat(p: Path, _root: Path) -> str:
        return p.read_text(encoding="utf-8") + "% flattened\n"

    monkeypatch.setattr(fb_mod, "resolve_variant_main", _flat)

    manifest = _FakeManifest(variants={"backend": "variants/backend/main.tex"})
    await render_and_compile(
        _doc(),
        {},
        artifact_dir,
        source_root=source_root,
        manifest=manifest,
        variant_label="backend",
        opp_id=_OPP_ID,
    )
    # commit_complete renamed partial → complete, so check the complete dir
    complete = artifact_dir.with_suffix(".complete")
    overlaid = (complete / "mmayer.tex").read_text(encoding="utf-8")
    assert "variant-only" in overlaid
    assert "flattened" in overlaid
    assert not partial.exists()


async def test_render_and_compile_falls_back_to_glob_when_main_pdf_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Tectonic may rename the PDF — fallback is a *.pdf glob."""
    artifact_dir = tmp_path / "art" / "opp"
    _stub_write_partial(monkeypatch, artifact_dir)

    def _odd_commit(partial: Path) -> Path:
        target = partial.with_suffix(".complete")
        os.rename(partial, target)
        # No mmayer.pdf — but a differently-named PDF lives in dir.
        (target / "resume.pdf").write_bytes(b"%PDF-glob")
        return target

    import src.application.resume_latex.render as render_mod

    monkeypatch.setattr(render_mod, "commit_complete", _odd_commit)
    _stub_compile_run(monkeypatch)

    result = await render_and_compile(
        _doc(),
        {},
        artifact_dir,
        source_root=tmp_path / "src",
        manifest=_FakeManifest(),
        variant_label="backend",
        opp_id=_OPP_ID,
    )
    assert result.pdf_path.name == "resume.pdf"

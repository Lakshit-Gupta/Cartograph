"""Tests for the LaTeX render pipeline."""
from __future__ import annotations

import pytest

from src.application.resume_latex.parser.blocks import Block, Document
from src.application.resume_latex.parser.manifest import ResumeManifest
from src.application.resume_latex.render import (
    SourceDriftError,
    _format_bullets,
    _splice_block_region,
    commit_complete,
    write_partial,
)


def _doc(source: str, *blocks: Block) -> Document:
    import hashlib
    return Document(
        blocks=list(blocks),
        files={"mmayer.tex": source},
        source_hashes={"mmayer.tex": hashlib.sha256(source.encode()).hexdigest()},
        manifest=ResumeManifest(
            main_file="mmayer.tex",
            class_file="altacv.cls",
            macro_vocabulary={"event": ["cvevent"]},
        ),
    )


def test_format_bullets_adds_smallskip_between_items():
    out = _format_bullets(["A", "B", "C"])
    assert out.count("\\smallskip") == 2
    assert out.count("\\item") == 3
    assert out.startswith("\\begin{itemize}")
    assert out.endswith("\\end{itemize}")


def test_format_bullets_single_item_no_smallskip():
    out = _format_bullets(["solo"])
    assert "\\smallskip" not in out
    assert out.count("\\item") == 1


def test_format_bullets_empty_emits_empty_block():
    out = _format_bullets([])
    assert "\\item" not in out
    assert out.startswith("\\begin{itemize}")


def test_splice_block_region_replaces_existing_itemize():
    region = (
        "\\cvevent{Title}{Co}{2026}{Loc}\n"
        "\\begin{itemize}\n\\item old\n\\end{itemize}"
    )
    out = _splice_block_region(region, ["new1", "new2"])
    assert "\\item new1" in out
    assert "\\item new2" in out
    assert "\\item old" not in out
    assert "\\cvevent{Title}{Co}{2026}{Loc}" in out


def test_splice_block_region_appends_when_no_itemize():
    region = "\\cvevent{Title}{Co}{2026}{Loc}"
    out = _splice_block_region(region, ["only bullet"])
    assert "\\cvevent{Title}{Co}{2026}{Loc}" in out
    assert "\\item only bullet" in out


def test_write_partial_writes_unchanged_when_no_edits(tmp_path):
    source = "\\cvevent{T}{C}{D}{L}\n\\begin{itemize}\n\\item original\n\\end{itemize}\n"
    doc = _doc(source, Block(
        id="b1", kind="event", title="T", bullets=["original"],
        file="mmayer.tex", char_range=(0, len(source) - 1),
    ))
    partial = write_partial(doc, edits={}, artifact_dir=tmp_path / "out")
    assert partial.name == "out.partial"
    written = (partial / "mmayer.tex").read_text()
    assert written == source


def test_write_partial_splices_edits(tmp_path):
    source = "\\cvevent{T}{C}{D}{L}\n\\begin{itemize}\n\\item original\n\\end{itemize}\n"
    block = Block(
        id="b1", kind="event", title="T", bullets=["original"],
        file="mmayer.tex", char_range=(0, len(source) - 1),
    )
    partial = write_partial(_doc(source, block), edits={"b1": ["new bullet"]},
                            artifact_dir=tmp_path / "out")
    written = (partial / "mmayer.tex").read_text()
    assert "\\item new bullet" in written
    assert "\\item original" not in written


def test_write_partial_descending_offset_order(tmp_path):
    """Multiple edits in one file must not invalidate each other's offsets."""
    source = (
        "AA\\begin{itemize}\\item a\\end{itemize}BB"
        "CC\\begin{itemize}\\item c\\end{itemize}DD"
    )
    b1 = Block(id="b1", kind="event", title="", bullets=[], file="mmayer.tex",
               char_range=(0, 38))  # first itemize region
    b2 = Block(id="b2", kind="event", title="", bullets=[], file="mmayer.tex",
               char_range=(40, len(source)))  # second itemize region
    partial = write_partial(
        _doc(source, b1, b2),
        edits={"b1": ["one"], "b2": ["two"]},
        artifact_dir=tmp_path / "out",
    )
    written = (partial / "mmayer.tex").read_text()
    assert "\\item one" in written
    assert "\\item two" in written


def test_commit_complete_atomic_rename(tmp_path):
    p = tmp_path / "out.partial"
    p.mkdir()
    (p / "x.tex").write_text("body")
    out = commit_complete(p)
    assert out.name == "out.complete"
    assert (out / "x.tex").read_text() == "body"
    assert not p.exists()


def test_commit_complete_overwrites_stale_complete_dir(tmp_path):
    stale = tmp_path / "out.complete"
    stale.mkdir()
    (stale / "old.tex").write_text("stale")
    p = tmp_path / "out.partial"
    p.mkdir()
    (p / "fresh.tex").write_text("fresh")
    out = commit_complete(p)
    # Old contents are gone, fresh contents present.
    assert not (out / "old.tex").exists()
    assert (out / "fresh.tex").read_text() == "fresh"


def test_write_partial_skips_invalid_ranges(tmp_path):
    source = "short source"
    block = Block(
        id="b1", kind="event", title="", bullets=[],
        file="mmayer.tex", char_range=(500, 1000),  # past end of source
    )
    partial = write_partial(_doc(source, block), edits={"b1": ["x"]},
                            artifact_dir=tmp_path / "out")
    # Invalid range = no-op; source written unchanged.
    assert (partial / "mmayer.tex").read_text() == source


def test_source_drift_error_is_raised_when_imported():
    # The error type is part of the module contract; ensure it's importable
    # and is a RuntimeError subclass.
    assert issubclass(SourceDriftError, RuntimeError)


def test_render_raises_on_source_drift(tmp_path):
    """If the on-disk file changes between parse and render, SourceDriftError."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    # Parse-time source.
    parsed_source = "\\cvevent{T}{C}{D}{L}\n\\begin{itemize}\n\\item original\n\\end{itemize}\n"
    (src_dir / "mmayer.tex").write_text(parsed_source)

    doc = _doc(parsed_source, Block(
        id="b1", kind="event", title="T", bullets=["original"],
        file="mmayer.tex", char_range=(0, len(parsed_source) - 1),
    ))

    # Simulate the user editing mmayer.tex between parse and render.
    (src_dir / "mmayer.tex").write_text("\\cvevent{Different}{}{}{}\n")

    with pytest.raises(SourceDriftError):
        write_partial(
            doc,
            edits={"b1": ["new bullet"]},
            artifact_dir=tmp_path / "out",
            source_root=src_dir,
        )

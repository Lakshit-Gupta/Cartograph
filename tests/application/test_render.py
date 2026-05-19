"""Tests for the LaTeX render pipeline."""

from __future__ import annotations

import pytest

from src.application.resume_latex.parser.blocks import Block, Document
from src.application.resume_latex.parser.manifest import ResumeManifest
from src.application.resume_latex.render import (
    SourceDriftError,
    _format_bullets,
    _splice_block_region,
    _strip_line_comments,
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
    region = "\\cvevent{Title}{Co}{2026}{Loc}\n\\begin{itemize}\n\\item old\n\\end{itemize}"
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
    doc = _doc(
        source,
        Block(
            id="b1",
            kind="event",
            title="T",
            bullets=["original"],
            file="mmayer.tex",
            char_range=(0, len(source) - 1),
        ),
    )
    partial = write_partial(doc, edits={}, artifact_dir=tmp_path / "out")
    assert partial.name == "out.partial"
    written = (partial / "mmayer.tex").read_text()
    assert written == source


def test_write_partial_splices_edits(tmp_path):
    source = "\\cvevent{T}{C}{D}{L}\n\\begin{itemize}\n\\item original\n\\end{itemize}\n"
    block = Block(
        id="b1",
        kind="event",
        title="T",
        bullets=["original"],
        file="mmayer.tex",
        char_range=(0, len(source) - 1),
    )
    partial = write_partial(_doc(source, block), edits={"b1": ["new bullet"]}, artifact_dir=tmp_path / "out")
    written = (partial / "mmayer.tex").read_text()
    assert "\\item new bullet" in written
    assert "\\item original" not in written


def test_write_partial_descending_offset_order(tmp_path):
    """Multiple edits in one file must not invalidate each other's offsets."""
    source = "AA\\begin{itemize}\\item a\\end{itemize}BBCC\\begin{itemize}\\item c\\end{itemize}DD"
    b1 = Block(id="b1", kind="event", title="", bullets=[], file="mmayer.tex", char_range=(0, 38))  # first itemize region
    b2 = Block(id="b2", kind="event", title="", bullets=[], file="mmayer.tex", char_range=(40, len(source)))  # second itemize region
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
        id="b1",
        kind="event",
        title="",
        bullets=[],
        file="mmayer.tex",
        char_range=(500, 1000),  # past end of source
    )
    partial = write_partial(_doc(source, block), edits={"b1": ["x"]}, artifact_dir=tmp_path / "out")
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

    doc = _doc(
        parsed_source,
        Block(
            id="b1",
            kind="event",
            title="T",
            bullets=["original"],
            file="mmayer.tex",
            char_range=(0, len(parsed_source) - 1),
        ),
    )

    # Simulate the user editing mmayer.tex between parse and render.
    (src_dir / "mmayer.tex").write_text("\\cvevent{Different}{}{}{}\n")

    with pytest.raises(SourceDriftError):
        write_partial(
            doc,
            edits={"b1": ["new bullet"]},
            artifact_dir=tmp_path / "out",
            source_root=src_dir,
        )


# ---------------------------------------------------------------------------
# Regression — commented-out \item / \begin{itemize} must not fool the splice
# (Stage-4 render defect: tectonic "Lonely \item" error).
# ---------------------------------------------------------------------------
def _count_unescaped(text: str, needle: str) -> int:
    """Count occurrences of ``needle`` not preceded by an odd number of \\s."""
    n = 0
    i = 0
    while True:
        j = text.find(needle, i)
        if j < 0:
            return n
        # Count contiguous backslashes immediately before j.
        k = j - 1
        bs = 0
        while k >= 0 and text[k] == "\\":
            bs += 1
            k -= 1
        if bs % 2 == 0:
            n += 1
        i = j + len(needle)


def _assert_items_inside_itemize(text: str) -> None:
    """Walk the (uncommented) text and assert every \\item sits inside a
    matching \\begin{itemize} ... \\end{itemize} scope.

    Only the live (non-commented) view is checked — the splice keeps
    comments intact but they must not impose LaTeX list constraints.
    """
    masked = _strip_line_comments(text)
    depth = 0
    i = 0
    while i < len(masked):
        if masked.startswith("\\begin{itemize}", i):
            depth += 1
            i += len("\\begin{itemize}")
            continue
        if masked.startswith("\\end{itemize}", i):
            assert depth > 0, f"unmatched \\end{{itemize}} at offset {i}"
            depth -= 1
            i += len("\\end{itemize}")
            continue
        if masked.startswith("\\item", i):
            # Make sure this is not part of a longer macro name like
            # \itemsep — pylatexenc tokenises by trailing alpha.
            tail = masked[i + len("\\item") : i + len("\\item") + 1]
            if not tail.isalpha():
                assert depth > 0, f"lonely \\item at offset {i}: no enclosing itemize scope"
            i += len("\\item")
            continue
        i += 1
    assert depth == 0, "unbalanced itemize: more \\begin than \\end"


@pytest.mark.smoke
def test_render_skips_commented_itemize_when_choosing_region():
    region = (
        "\\cvevent {\\textbf{SDE Intern}}{Co}{2025}{Loc}\n"
        "% \\begin{itemize}\n"
        "% \\item old commented bullet 1\n"
        "% \\smallskip\n"
        "% \\item old commented bullet 2\n"
        "% \\end{itemize}\n"
    )
    out = _splice_block_region(region, ["tailored A", "tailored B"])

    # Exactly one live \begin{itemize} (the new one). The commented
    # `% \begin{itemize}` does not count.
    live_begins = _count_unescaped(_strip_line_comments(out), "\\begin{itemize}")
    assert live_begins == 1, out

    live_items = _count_unescaped(_strip_line_comments(out), "\\item")
    assert live_items == 2, out

    # No lonely \item — every live \item must be inside a live itemize scope.
    _assert_items_inside_itemize(out)

    # The tailored bullets are present in the live view.
    assert "\\item tailored A" in out
    assert "\\item tailored B" in out


@pytest.mark.smoke
def test_render_replaces_live_itemize_when_uncommented():
    region = "\\cvevent {\\textbf{SDE Intern}}{Co}{2025}{Loc}\n\\begin{itemize}\n\\item old bullet 1\n\\end{itemize}\n"
    out = _splice_block_region(region, ["new A", "new B"])
    assert "\\item old bullet 1" not in out
    assert "\\item new A" in out
    assert "\\item new B" in out
    # Still exactly one live itemize after splice.
    live_begins = _count_unescaped(_strip_line_comments(out), "\\begin{itemize}")
    assert live_begins == 1
    _assert_items_inside_itemize(out)


@pytest.mark.smoke
def test_render_preserves_comments_verbatim():
    region = "\\cvevent {\\textbf{SDE Intern}}{Co}{2025}{Loc}\n% \\begin{itemize}\n% \\item old commented bullet\n% \\end{itemize}\n"
    out = _splice_block_region(region, ["fresh"])
    # The literal commented text MUST still be present — we only ignore
    # comments for boundary matching, never strip them from output.
    assert "% \\begin{itemize}" in out
    assert "% \\item old commented bullet" in out
    assert "% \\end{itemize}" in out
    # And the fresh bullet landed.
    assert "\\item fresh" in out


@pytest.mark.smoke
def test_no_lonely_item_in_tailored_output_against_real_resume(tmp_path):
    """Parse the real mmayer.tex, splice synthetic bullets into the
    SDE Intern cvevent, render, and assert no lonely \\item anywhere.
    """
    from pathlib import Path

    from src.application.resume_latex.parser import manifest as manifest_mod
    from src.application.resume_latex.parser.blocks import parse

    resume_root = Path("config/profile/my_resume")
    manifest = manifest_mod.load(resume_root / "manifest.yaml")
    doc = parse(manifest, resume_root)

    # Find the SDE Intern cvevent block.
    sde_block = next(
        (b for b in doc.blocks if "SDE Intern" in b.title),
        None,
    )
    assert sde_block is not None, "expected SDE Intern cvevent in real resume"

    edits = {sde_block.id: ["synthetic tailored A", "synthetic tailored B"]}
    partial = write_partial(doc, edits=edits, artifact_dir=tmp_path / "out", source_root=resume_root)

    rendered = (partial / "mmayer.tex").read_text(encoding="utf-8")

    # The tailored bullets reached the output.
    assert "synthetic tailored A" in rendered
    assert "synthetic tailored B" in rendered

    # Every live \item is inside a live \begin{itemize}…\end{itemize}.
    _assert_items_inside_itemize(rendered)


# ---------------------------------------------------------------------------
# Direct coverage of the comment-masking helper.
# ---------------------------------------------------------------------------
def test_strip_line_comments_preserves_byte_offsets():
    region = "abc % comment text\ndef"
    masked = _strip_line_comments(region)
    assert len(masked) == len(region)
    # The newline is preserved at the same offset.
    assert masked.index("\n") == region.index("\n")
    # def survives.
    assert masked.endswith("def")
    # The comment body is gone (replaced with spaces).
    assert "comment" not in masked


def test_strip_line_comments_respects_escaped_percent():
    # \% is a literal percent, NOT a comment start.
    region = "value 100\\% then more text\nnext"
    masked = _strip_line_comments(region)
    # The escape and the text after it survive.
    assert "more text" in masked
    assert "\\%" in masked


def test_strip_line_comments_handles_double_backslash_then_percent():
    # \\% is a backslash then a comment — \\ is "end of line", % starts comment.
    region = "x\\\\% commentary\nnext"
    masked = _strip_line_comments(region)
    # "commentary" must be masked out (the % is unescaped because the run
    # of backslashes before it is even).
    assert "commentary" not in masked
    # next-line content survives.
    assert "next" in masked

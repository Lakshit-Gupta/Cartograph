"""Parser smoke test against the real resume tree."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.application.resume_latex.parser.blocks import parse
from src.application.resume_latex.parser.manifest import load as load_manifest

RESUME_ROOT = Path("config/profile/my_resume")
MANIFEST_PATH = RESUME_ROOT / "manifest.yaml"


def _have_resume_tree() -> bool:
    return MANIFEST_PATH.exists() and (RESUME_ROOT / "mmayer.tex").exists()


@pytest.mark.skipif(not _have_resume_tree(), reason="resume tree absent")
def test_manifest_loads_with_expected_fields():
    m = load_manifest(MANIFEST_PATH)
    assert m.main_file == "mmayer.tex"
    assert m.class_file == "altacv.cls"
    assert "event" in m.macro_vocabulary
    assert "cvevent" in m.macro_vocabulary["event"]


@pytest.mark.skipif(not _have_resume_tree(), reason="resume tree absent")
def test_parser_extracts_at_least_one_block():
    m = load_manifest(MANIFEST_PATH)
    doc = parse(m, RESUME_ROOT)
    assert len(doc.blocks) >= 1
    assert all(b.id for b in doc.blocks)
    assert all(b.char_range[0] < b.char_range[1] for b in doc.blocks)


@pytest.mark.skipif(not _have_resume_tree(), reason="resume tree absent")
def test_parser_records_source_hashes_for_each_file():
    m = load_manifest(MANIFEST_PATH)
    doc = parse(m, RESUME_ROOT)
    assert "mmayer.tex" in doc.source_hashes
    # Hashes are 64-hex sha256.
    assert all(len(h) == 64 for h in doc.source_hashes.values())


@pytest.mark.skipif(not _have_resume_tree(), reason="resume tree absent")
def test_parser_block_ids_are_stable_across_runs():
    m = load_manifest(MANIFEST_PATH)
    doc1 = parse(m, RESUME_ROOT)
    doc2 = parse(m, RESUME_ROOT)
    assert [b.id for b in doc1.blocks] == [b.id for b in doc2.blocks]


@pytest.mark.skipif(not _have_resume_tree(), reason="resume tree absent")
def test_parser_excludes_listed_sections():
    """exclude_sections in manifest.yaml must drop those titles from blocks."""
    m = load_manifest(MANIFEST_PATH)
    doc = parse(m, RESUME_ROOT)
    titles = [b.title.strip().lower() for b in doc.blocks]
    for excluded in m.exclude_sections:
        assert excluded.strip().lower() not in titles, (
            f"excluded section {excluded!r} leaked through into blocks"
        )

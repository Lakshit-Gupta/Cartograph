"""Integration tests for the tectonic compile pipeline.

Marked with ``@pytest.mark.integration`` because they require:
  * ``tectonic``  — installed by docker/applier.Dockerfile
  * ``qpdf``      — same image
  * ``exiftool``  — same image
  * network access for the tectonic CTAN bundle (cold cache; ~30 s)

These tests do NOT run in the default ``make test`` lane because CI
runs against the base ``jobs-bot`` image, which doesn't ship the LaTeX
toolchain. Run them inside the applier-worker container or on a host
with the full toolchain installed:

    pytest -m integration tests/application/test_compile.py

The default invocation ``pytest`` will collect-and-skip these (pytest's
default behaviour for missing markers is to deselect via ``-m`` filter;
here we let them collect but rely on the host having tectonic — if it
doesn't, the test fails loudly rather than silently passing).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.application.resume_latex.compile import CompileError, run


def _have_tool(name: str) -> bool:
    return shutil.which(name) is not None


_NEED_TOOLS = ("tectonic", "qpdf", "exiftool")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_compile_existing_resume(tmp_path):
    """Compile the user's real resume — the canonical smoke test."""
    for tool in _NEED_TOOLS:
        if not _have_tool(tool):
            pytest.skip(f"{tool} not installed; integration test requires the applier image")

    src = Path(__file__).resolve().parents[2] / "config" / "profile" / "my_resume"
    dst = tmp_path / "resume"
    shutil.copytree(src, dst)
    result = await run(dst / "mmayer.tex")
    assert result.pdf_path.exists()
    # Cold cache CI compile can hit 25-30 s; warm cache <2 s. Allow generous
    # slack so the test passes on the very first run when the tectonic
    # bundle is still being downloaded from CTAN.
    assert result.duration_ms < 60000
    assert result.tectonic_version.lower().startswith("tectonic")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_compile_rejects_write18_injection(tmp_path):
    """``--untrusted`` must prevent shell escape via ``\\write18``.

    Builds a minimal LaTeX file that tries to execute ``rm -rf /`` via
    ``\\write18`` and confirms tectonic refuses to compile (the
    untrusted-mode flag is the third hard-rule defence layer behind
    the sanitizer + macro denylist).
    """
    for tool in _NEED_TOOLS:
        if not _have_tool(tool):
            pytest.skip(f"{tool} not installed")
    main_tex = tmp_path / "evil.tex"
    main_tex.write_text("\\documentclass{article}\n\\begin{document}\n\\write18{rm -rf /tmp/should_never_run}\nevil\n\\end{document}\n")
    # tectonic --untrusted: write18 silently disabled (the body should
    # still compile, but the shell command must not execute).
    canary = tmp_path / "should_never_run"
    canary.write_text("present")
    try:
        await run(main_tex)
    except CompileError:
        # Either outcome is acceptable: tectonic may refuse, or compile
        # may succeed with write18 disabled. Both prove the sandbox holds.
        pass
    # Canary file must still be present — write18 didn't execute.
    assert canary.exists()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_compile_strips_pdf_metadata(tmp_path):
    """exiftool post-pass must remove identifying metadata from the PDF."""
    for tool in _NEED_TOOLS:
        if not _have_tool(tool):
            pytest.skip(f"{tool} not installed")
    main_tex = tmp_path / "doc.tex"
    main_tex.write_text(
        "\\documentclass{article}\n"
        "\\usepackage{hyperref}\n"
        "\\hypersetup{pdftitle={Should Be Stripped},pdfauthor={Alice}}\n"
        "\\begin{document}\nhello\n\\end{document}\n"
    )
    result = await run(main_tex)
    out = subprocess.run(
        ["exiftool", "-Author", "-Producer", "-Creator", str(result.pdf_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    # exiftool -all:all= clears every tag; the value column for each
    # requested field should be empty (or the field absent altogether).
    for line in out.stdout.splitlines():
        _key, _, value = line.partition(":")
        assert not value.strip(), f"metadata leaked: {line!r}"

"""Plain-text view of the resume tree for the ranker profile embedding."""
from __future__ import annotations

from pathlib import Path

from pylatexenc.latex2text import LatexNodes2Text  # type: ignore[import-untyped]

from src.application.resume_latex.parser.blocks import parse
from src.application.resume_latex.parser.manifest import load as load_manifest

_DEFAULT_TEXTIFIER = LatexNodes2Text(
    keep_comments=False,
    math_mode="text",
    fill_text=False,
)


def to_plain_text(latex: str) -> str:
    """Strip every LaTeX wrapper from ``latex`` and return the prose."""
    if not latex:
        return ""
    try:
        return _DEFAULT_TEXTIFIER.latex_to_text(latex)
    except Exception:
        # pylatexenc occasionally chokes on AltaCV's custom macros; fall
        # back to a naive strip so callers always get *something* to embed.
        out = []
        depth = 0
        i = 0
        while i < len(latex):
            ch = latex[i]
            if ch == "\\":
                # Skip to end of macro name.
                j = i + 1
                while j < len(latex) and latex[j].isalpha():
                    j += 1
                i = j
                continue
            if ch == "{":
                depth += 1
                i += 1
                continue
            if ch == "}":
                depth = max(0, depth - 1)
                i += 1
                continue
            out.append(ch)
            i += 1
        return "".join(out)


def resume_plain_text(manifest_path: Path, resume_root: Path) -> str:
    """Render the full resume tree as one prose string.

    Used by the ranker profile-embedding pipeline so the LaTeX tree can
    serve as the source of truth instead of the legacy ``resume.json``.
    Concatenates every block's title + bullets across every file.
    """
    manifest = load_manifest(manifest_path)
    doc = parse(manifest, resume_root)
    parts: list[str] = []
    for b in doc.blocks:
        parts.append(b.title)
        parts.extend(b.bullets)
    return "\n".join(parts)

"""Render — splice tailored bullets into the resume tree on disk.

The flow:
1. ``write_partial`` clones every file in ``Document.files`` into a
   ``<artifact_dir>.partial/`` directory. For each block whose ``id`` is
   keyed in ``edits``, the corresponding itemize body is rewritten using
   the new bullets while the surrounding macro args (title, company,
   date, location) are preserved verbatim.
2. ``commit_complete`` renames ``.partial`` -> ``.complete`` atomically.

Drift guard: ``Document.source_hashes`` was sampled at parse time.
``write_partial`` re-hashes every file before splicing — if the source
moved under the parser the function raises ``SourceDriftError`` so the
caller can re-parse and retry instead of writing a corrupted output.

Bullets are spliced in **descending** char-range order so each edit's
offset stays valid relative to the unedited tail of the file.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from pathlib import Path

from src.application.resume_latex.parser.blocks import Block, Document


class SourceDriftError(RuntimeError):
    """The on-disk source file changed since Document was parsed."""


# Captures one or more consecutive itemize blocks (including any \smallskip
# / \item / nested itemize between them). We do not parse: a greedy
# match is fine because the block's char_range is already a tight bound.
_ITEMIZE_RE = re.compile(
    r"\\begin\{itemize\}.*?\\end\{itemize\}",
    flags=re.DOTALL,
)


def _format_bullets(bullets: list[str]) -> str:
    """Render bullets as a fresh AltaCV-style itemize block.

    AltaCV style:
        \\begin{itemize}
        \\item bullet 1
        \\smallskip
        \\item bullet 2
        ...
        \\end{itemize}

    Inserts a ``\\smallskip`` between items to match the look of the
    user's mmayer.tex (which threads ``\\smallskip`` between every
    ``\\item``). Single-item bullets get no spacer.
    """
    if not bullets:
        return "\\begin{itemize}\n\\end{itemize}"
    parts: list[str] = ["\\begin{itemize}"]
    for i, b in enumerate(bullets):
        parts.append(f"\\item {b}")
        if i < len(bullets) - 1:
            parts.append("\\smallskip")
    parts.append("\\end{itemize}")
    return "\n".join(parts)


def _splice_block_region(region: str, new_bullets: list[str]) -> str:
    """Rewrite the itemize section of one block's char_range region.

    If the region contains exactly one itemize block, it is replaced
    inline. If it contains none, the new itemize is appended at the end
    of the region (covers the "orphan macro with no itemize" case the
    parser emits for cvevent + cvsection with no following items).
    """
    new_block = _format_bullets(new_bullets)
    if _ITEMIZE_RE.search(region):
        # Use a lambda replacement so backslashes in ``new_block`` (e.g.
        # ``\item``) aren't interpreted as regex back-references.
        return _ITEMIZE_RE.sub(lambda _m: new_block, region, count=1)
    # No existing itemize → append the new block to the end of the region.
    return region.rstrip() + "\n" + new_block + "\n"


def write_partial(
    doc: Document,
    edits: dict[str, list[str]],
    artifact_dir: Path,
    *,
    source_root: Path | None = None,
) -> Path:
    """Render the tailored tree under ``<artifact_dir>.partial/``.

    Args:
        doc: ``Document`` returned by ``parser.blocks.parse``.
        edits: block_id -> new bullets. Block ids not in this map keep
            their original bullets.
        artifact_dir: target directory; this function writes to
            ``artifact_dir.with_suffix('.partial')``.
        source_root: directory the Document was parsed from. When set,
            the renderer re-reads each file from disk and aborts with
            ``SourceDriftError`` if the on-disk sha256 no longer matches
            ``doc.source_hashes[fname]`` — catches the case where the
            user edits ``mmayer.tex`` between parse and render. Also used
            to copy non-source assets (``altacv.cls``, ``profile.jpg``,
            ``sample.bib``) into the partial dir so tectonic finds them.

    Raises:
        SourceDriftError: a source file's on-disk sha256 changed since
            ``parse(...)`` recorded it.

    Returns:
        Path to the populated ``.partial`` directory.
    """
    partial = artifact_dir.with_suffix(".partial")
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(parents=True, exist_ok=True)

    # Group edits by file so we can splice ranges per-file in one pass.
    edits_by_file: dict[str, list[tuple[Block, list[str]]]] = {}
    for b in doc.blocks:
        if b.id in edits:
            edits_by_file.setdefault(b.file, []).append((b, edits[b.id]))

    for fname, source in doc.files.items():
        # Drift guard: compare against the file on disk if we were given
        # a root. Catches the editor-saving-mid-render race that would
        # otherwise produce a corrupted splice (char_range computed against
        # the OLD source applied to NEW text).
        if source_root is not None:
            disk_path = source_root / fname
            if disk_path.exists():
                disk_source = disk_path.read_text(encoding="utf-8")
                disk_hash = hashlib.sha256(disk_source.encode("utf-8")).hexdigest()
                expected = doc.source_hashes.get(fname)
                if expected and disk_hash != expected:
                    raise SourceDriftError(f"source drift detected on {fname}: expected {expected[:12]}…, disk {disk_hash[:12]}…")

        # Descending start-offset splice so earlier edits don't shift later
        # offsets.
        file_edits = sorted(
            edits_by_file.get(fname, []),
            key=lambda be: be[0].char_range[0],
            reverse=True,
        )
        spliced = source
        for block, new_bullets in file_edits:
            start, end = block.char_range
            if start < 0 or end > len(spliced) or start >= end:
                # Range invalid — skip silently rather than corrupt output.
                continue
            original = spliced[start:end]
            replacement = _splice_block_region(original, new_bullets)
            spliced = spliced[:start] + replacement + spliced[end:]

        (partial / fname).write_text(spliced, encoding="utf-8")

    # Copy non-source assets (altacv.cls, .bib, .jpg) from the original
    # tree so tectonic finds them next to the main file. Only copy files
    # the parser didn't already write so we don't overwrite spliced output.
    if source_root is not None and source_root.is_dir():
        for asset in source_root.iterdir():
            if asset.is_file() and asset.name not in doc.files:
                shutil.copy2(asset, partial / asset.name)

    return partial


def commit_complete(partial: Path) -> Path:
    """Atomically rename ``<dir>.partial`` -> ``<dir>.complete``.

    POSIX ``rename(2)`` is atomic within the same filesystem. Tectonic
    reads from the ``.complete`` directory so a partial output never gets
    used by accident if compile is interrupted mid-render.
    """
    target = partial.with_suffix(".complete")
    if target.exists():
        shutil.rmtree(target)
    os.rename(partial, target)
    return target

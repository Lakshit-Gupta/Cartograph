"""Render + compile + post-process pipeline for the LaTeX apply path.

Step 5 (write tree) → 6 (tectonic) → post-process (qpdf + exiftool) →
atomic promote ``.partial → .complete``.

The split below is purely structural — call order matches the legacy
``_send_with_latex`` exactly:

1. :func:`_write_partial_tree` - splice bullets into a partial dir, then
   overlay the variant ``main.tex`` if the picker chose a non-base
   variant (Phase 2.2).
2. :func:`_run_tectonic` - ``tectonic --untrusted`` with 30 s timeout
   and ``kill_group=True`` (CLAUDE.md hard rule #3). Returns the
   ``CompileResult`` from ``src.application.resume_latex.compile.run``.
3. :func:`_postprocess_pdf` - locate the produced PDF (tectonic may
   rename it, so we fall back to a directory scan). The qpdf-linearise
   and ``exiftool -all:all=`` metadata scrub (hard rule #4) happen
   inside ``compile.run`` itself; we only resolve the path here.
4. :func:`_promote_partial_to_complete` - atomic ``.partial → .complete``
   rename via ``commit_complete``.

Source-hash drift guard (hard rule #6): ``write_partial`` re-verifies
``document.source_hashes`` against the on-disk source before splicing.
A drift raises ``SourceDriftError`` and we abort up the call stack.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.common.logger import get_logger

if TYPE_CHECKING:
    from src.application.resume_latex.parser.blocks import Document
    from src.application.resume_latex.parser.manifest import ResumeManifest

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CompileSuccess:
    """Outcome of a successful tailored compile."""

    pdf_path: Path
    duration_ms: int | None
    tectonic_version: str | None


def _write_partial_tree(
    document: Document,
    sanitized_edits: dict[str, list[str]],
    artifact_dir: Path,
    *,
    source_root: Path,
    manifest: ResumeManifest,
    variant_label: str,
    opp_id: Any,
) -> Path:
    """Splice tailored bullets, then optionally overlay the variant main.

    Returns the ``.partial/`` directory ready for tectonic.
    """
    from src.application.resume_latex.fallback import resolve_variant_main
    from src.application.resume_latex.render import write_partial

    partial = write_partial(
        document,
        sanitized_edits,
        artifact_dir,
        source_root=source_root,
    )

    # Phase 2.2 — variant overlay. Identical semantics to the legacy
    # site: when the picker chose a non-base variant whose stub lives at
    # ``variants/<label>/main.tex``, flatten that stub and overwrite the
    # base main file in ``partial/``. The tailored splice is lost on
    # overlay — that's an accepted Phase 2.2 trade-off (user-edited
    # variant ``.tex`` is the source of truth for that lane).
    variant_main_rel = manifest.variants.get(variant_label) if manifest.variants else None
    if variant_main_rel:
        variant_path = source_root / variant_main_rel
        if variant_path.is_file():
            flat_source = resolve_variant_main(variant_path, source_root)
            (partial / manifest.main_file).write_text(flat_source, encoding="utf-8")
            _log.info(
                "resume_variant_overlay_applied",
                label=variant_label,
                variant_main=str(variant_path),
                opp_id=str(opp_id),
            )
    return partial


async def _run_tectonic(partial: Path, main_file: str) -> Any:
    """Compile the partial tree via tectonic.

    Always ``--untrusted`` + 30 s timeout + ``kill_group=True`` — those
    knobs live in ``resume_latex.compile.run`` (hard rule #3); we only
    invoke them here.
    """
    from src.application.resume_latex.compile import run as compile_run

    return await compile_run(partial / main_file)


def _postprocess_pdf(complete: Path, main_file: str) -> Path | None:
    """Resolve the produced PDF in the promoted ``.complete/`` dir.

    The qpdf linearise + ``exiftool -all:all=`` metadata scrub (hard
    rule #4) execute inside ``compile.run`` — by the time we land here
    the file is already scrubbed; we just locate it.
    """
    pdf_path = complete / main_file.replace(".tex", ".pdf")
    if pdf_path.exists():
        return pdf_path
    pdfs = list(complete.glob("*.pdf"))
    return pdfs[0] if pdfs else None


def _promote_partial_to_complete(partial: Path) -> Path:
    """Atomic ``.partial → .complete`` rename."""
    from src.application.resume_latex.render import commit_complete

    return commit_complete(partial)


async def render_and_compile(
    document: Document,
    sanitized_edits: dict[str, list[str]],
    artifact_dir: Path,
    *,
    source_root: Path,
    manifest: ResumeManifest,
    variant_label: str,
    opp_id: Any,
) -> CompileSuccess:
    """Run the full render → tectonic → promote chain.

    Returns a :class:`CompileSuccess` on success. Raises whatever the
    underlying ``write_partial`` / ``compile.run`` / ``commit_complete``
    layer raises — the caller (``pipeline.py``) catches the typed
    exceptions and drops to the fallback PDF.
    """
    partial = _write_partial_tree(
        document,
        sanitized_edits,
        artifact_dir,
        source_root=source_root,
        manifest=manifest,
        variant_label=variant_label,
        opp_id=opp_id,
    )
    result = await _run_tectonic(partial, manifest.main_file)
    complete = _promote_partial_to_complete(partial)
    pdf_path = _postprocess_pdf(complete, manifest.main_file)
    if pdf_path is None:
        # Promoted directory has no PDF — treat as a compile failure so
        # the caller routes to the fallback branch.
        from src.application.resume_latex.compile import CompileError

        raise CompileError("tectonic produced no .pdf in completed artifact dir")
    return CompileSuccess(
        pdf_path=pdf_path,
        duration_ms=result.duration_ms,
        tectonic_version=result.tectonic_version,
    )


__all__ = ["CompileSuccess", "render_and_compile"]

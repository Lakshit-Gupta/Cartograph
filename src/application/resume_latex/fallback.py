"""Fallback PDF — pre-compiled untailored resume kept on disk.

When the tailored compile path fails (sanitizer reject, tectonic timeout,
source drift, network blip during a cold cache) the applier still must
attach a PDF to the outbound email; dropping the apply silently is the
worst outcome. ``warm_fallback_pdf`` runs once at applier-worker boot to
compile and cache the untailored tree; ``get_fallback`` returns the
cached path (or ``None`` if warm-up hasn't run yet).

Phase 2.2 — A/B variants:
    ``warm_fallback_pdf`` accepts an optional ``variant_label``. When set,
    the resolver flattens any ``\\input{../../mmayer.tex}`` (or any
    ``\\input{<base>}``) inside the variant main.tex by inlining the base
    file's contents — keeping tectonic ``--untrusted`` happy (no parent
    dir reads) while letting the variant override skills/bullets after
    the include. The compiled PDF is keyed as ``fallback_<label>.pdf``
    so the apply path can pick the right one at send time.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from src.application.resume_latex.compile import CompileError, run
from src.common.logger import get_logger

_log = get_logger(__name__)

_FALLBACK_ROOT = Path("/var/lib/agent/resume_artifacts")

# Matches \input{anything} or \input{anything.tex}. Captures the path
# relative to the variant file; sender + warmup resolve it against the
# variant subdir, then inline the base file content.
_INPUT_RE = re.compile(r"\\input\{([^}]+)\}")


def _user_dir(user_id: int) -> Path:
    return _FALLBACK_ROOT / str(user_id)


def _slugify_label(label: str | None) -> str:
    """Filename-safe slug for variant labels. Empty/None → ``base``.

    Resume_variants.label is already lowercase ASCII (V011 seed), so this
    is mostly defensive against future labels with weird chars.
    """
    if not label:
        return "base"
    s = re.sub(r"[^a-z0-9_]+", "_", label.lower()).strip("_")
    return s or "base"


def get_fallback(user_id: int, variant_label: str | None = None) -> Path | None:
    """Return the cached untailored PDF for ``user_id`` (or ``None``).

    Phase 2.2: when ``variant_label`` is set, returns the per-variant
    cached PDF; falls back to the base ``fallback.pdf`` if the variant
    PDF doesn't exist (e.g. warmup failed for that variant alone).
    """
    if variant_label:
        per_variant = _user_dir(user_id) / f"fallback_{_slugify_label(variant_label)}.pdf"
        if per_variant.exists():
            return per_variant
    pdf = _user_dir(user_id) / "fallback.pdf"
    return pdf if pdf.exists() else None


def resolve_variant_main(variant_tex_path: Path, source_root: Path) -> str:
    """Flatten ``\\input{<rel>}`` references inside a variant main.tex.

    The variant stub ships with ``\\input{../../mmayer.tex}`` — clean to
    read in the repo, but tectonic's ``--untrusted`` sandbox refuses any
    file read outside the compile cwd. Resolver behaviour:
      * Each ``\\input{<rel>}`` is resolved against ``variant_tex_path.parent``
        first (the spec-form ``../../mmayer.tex``); if missing, retried
        against ``source_root``; if still missing, left in place.
      * The resolved file's contents are spliced in verbatim. No recursion
        through nested includes — the variant stubs are one-deep by spec.
      * Lines that begin with ``%`` (LaTeX line comments — leading
        whitespace allowed) are skipped during substitution so a comment
        that *mentions* ``\\input{x}`` for documentation purposes doesn't
        get flattened into a giant inlined comment.

    Returns the flattened LaTeX source as a single string, ready to write
    to ``partial/main.tex``.
    """
    raw = variant_tex_path.read_text(encoding="utf-8")

    def _replace(match: re.Match[str]) -> str:
        ref = match.group(1).strip()
        # Try local relative first (handles ../../mmayer.tex from variant subdir).
        candidate = (variant_tex_path.parent / ref).resolve()
        if not candidate.is_file():
            # Fall back to the resume root (e.g. ``\input{mmayer.tex}`` with no path).
            candidate = source_root / ref
        if not candidate.is_file():
            # Leave the original line untouched — tectonic will surface the
            # missing-file error and the fallback path stays None.
            return match.group(0)
        return candidate.read_text(encoding="utf-8")

    # Walk line by line so we can skip comments. A LaTeX comment is any
    # line whose first non-whitespace char is ``%`` (escape-aware ``\%``
    # is data, not a comment-start — handled implicitly because the
    # regex only fires on non-comment lines).
    out_lines: list[str] = []
    for line in raw.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("%"):
            out_lines.append(line)
            continue
        out_lines.append(_INPUT_RE.sub(_replace, line))
    return "".join(out_lines)


async def warm_fallback_pdf(
    user_id: int,
    resume_root: Path,
    main_file: str,
    *,
    variant_label: str | None = None,
) -> Path | None:
    """Compile the untailored tree once and cache it as a fallback PDF.

    Args:
        user_id: tenant id; PDF is keyed at
            ``/var/lib/agent/resume_artifacts/<user_id>/fallback[_<label>].pdf``.
        resume_root: directory containing the user's .tex tree.
        main_file: filename of the main .tex (per manifest.main_file or
            manifest.variants[label]).
        variant_label: optional; when set, the result lands at
            ``fallback_<label>.pdf`` so the apply path can pick the right
            fallback per variant.

    Returns the path to the cached PDF on success, ``None`` on failure
    (callers log the failure and proceed without a fallback PDF — the
    apply will attempt the tailored path; if that also fails, no PDF
    is attached and the application status becomes ``failed``).
    """
    user_root = _user_dir(user_id)
    user_root.mkdir(parents=True, exist_ok=True)

    slug = _slugify_label(variant_label)
    staging = user_root / f".fallback_stage_{slug}"
    if staging.exists():
        shutil.rmtree(staging)
    # Copy ONLY top-level assets from the resume root so the staging tree
    # is flat (altacv.cls, page1sidebar.tex, profile.jpg next to main.tex).
    # The variant subdirs are intentionally skipped — we synthesise a flat
    # main.tex from the variant content below.
    staging.mkdir(parents=True, exist_ok=True)
    # Synchronous Path iter is fine here — boot-time warmup is single-shot
    # and the resume root holds <10 files; switching to anyio.path adds
    # zero value and a heavier import.
    for item in resume_root.iterdir():  # noqa: ASYNC240 — boot-time warmup, file count tiny
        if item.is_file():
            shutil.copy2(item, staging / item.name)

    # Resolve the main file. For variants, we flatten ``\input{../../X}``
    # so tectonic --untrusted (which refuses parent-dir reads) can compile
    # the file as a sibling of the flat assets.
    variant_path = resume_root / main_file
    if not variant_path.is_file():
        _log.warning("fallback_main_missing", user_id=user_id, path=str(variant_path), variant=variant_label)
        shutil.rmtree(staging, ignore_errors=True)
        return None

    flat_source = resolve_variant_main(variant_path, resume_root)
    flat_main = staging / "main.tex"
    flat_main.write_text(flat_source, encoding="utf-8")

    try:
        result = await run(flat_main)
    except CompileError as e:
        _log.warning("fallback_compile_failed", user_id=user_id, err=str(e), variant=variant_label)
        shutil.rmtree(staging, ignore_errors=True)
        return None

    target = user_root / (f"fallback_{slug}.pdf" if variant_label else "fallback.pdf")
    shutil.copy2(result.pdf_path, target)
    shutil.rmtree(staging, ignore_errors=True)
    _log.info(
        "fallback_warmed",
        user_id=user_id,
        duration_ms=result.duration_ms,
        path=str(target),
        variant=variant_label,
    )
    return target

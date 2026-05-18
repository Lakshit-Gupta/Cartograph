"""Fallback PDF — pre-compiled untailored resume kept on disk.

When the tailored compile path fails (sanitizer reject, tectonic timeout,
source drift, network blip during a cold cache) the applier still must
attach a PDF to the outbound email; dropping the apply silently is the
worst outcome. ``warm_fallback_pdf`` runs once at applier-worker boot to
compile and cache the untailored tree; ``get_fallback`` returns the
cached path (or ``None`` if warm-up hasn't run yet).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from src.application.resume_latex.compile import CompileError, run
from src.common.logger import get_logger

_log = get_logger(__name__)

_FALLBACK_ROOT = Path("/var/lib/agent/resume_artifacts")


def _user_dir(user_id: int) -> Path:
    return _FALLBACK_ROOT / str(user_id)


def get_fallback(user_id: int) -> Path | None:
    """Return the cached untailored PDF for ``user_id`` (or ``None``)."""
    pdf = _user_dir(user_id) / "fallback.pdf"
    return pdf if pdf.exists() else None


async def warm_fallback_pdf(user_id: int, resume_root: Path, main_file: str) -> Path | None:
    """Compile the untailored tree once and cache it as ``fallback.pdf``.

    Args:
        user_id: tenant id; PDF is keyed at
            ``/var/lib/agent/resume_artifacts/<user_id>/fallback.pdf``.
        resume_root: directory containing the user's .tex tree.
        main_file: filename of the main .tex (per manifest.main_file).

    Returns the path to the cached PDF on success, ``None`` on failure
    (callers log the failure and proceed without a fallback PDF — the
    apply will attempt the tailored path; if that also fails, no PDF
    is attached and the application status becomes ``failed``).
    """
    user_root = _user_dir(user_id)
    user_root.mkdir(parents=True, exist_ok=True)

    # Stage a working copy so tectonic's intermediates don't pollute the
    # source tree.
    staging = user_root / ".fallback_stage"
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(resume_root, staging)

    main_path = staging / main_file
    if not main_path.exists():
        _log.warning("fallback_main_missing", user_id=user_id, path=str(main_path))
        return None

    try:
        result = await run(main_path)
    except CompileError as e:
        _log.warning("fallback_compile_failed", user_id=user_id, err=str(e))
        return None

    target = user_root / "fallback.pdf"
    shutil.copy2(result.pdf_path, target)
    shutil.rmtree(staging, ignore_errors=True)
    _log.info("fallback_warmed", user_id=user_id, duration_ms=result.duration_ms, path=str(target))
    return target

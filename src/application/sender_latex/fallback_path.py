"""Fallback PDF resolution for the LaTeX apply path.

When the tailored compile fails (sanitizer reject, source drift, tectonic
error, anything unexpected), we still want to send *an* email — falling
back to the per-variant boot-warmed PDF, then to the unlabelled base
PDF if the variant warmup hadn't completed yet.

The fallback path keeps ``compile_status`` truthful:

- ``tailored`` — a freshly compiled, tailored PDF.
- ``fallback`` — used the boot-warmed PDF instead.
- ``failed`` — no PDF at all (neither tailored nor fallback). The apply
  still proceeds; the email just lands without an attachment.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.common.logger import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class FallbackOutcome:
    """Resolved PDF plus the compile_status to record on the row."""

    pdf_path: Path | None
    status: str  # 'tailored' | 'fallback' | 'failed'


def resolve_pdf_or_fallback(
    tailored_pdf: Path | None,
    *,
    user_id: int,
    variant_label: str,
    opp_id: str,
) -> FallbackOutcome:
    """Pick the right PDF + status to return up the apply flow."""
    if tailored_pdf is not None:
        return FallbackOutcome(pdf_path=tailored_pdf, status="tailored")

    from src.application.resume_latex.fallback import get_fallback

    fb = get_fallback(user_id, variant_label=variant_label)
    if fb is not None:
        return FallbackOutcome(pdf_path=fb, status="fallback")

    _log.warning(
        "resume_no_fallback_available",
        opp_id=opp_id,
        user_id=user_id,
        variant=variant_label,
    )
    return FallbackOutcome(pdf_path=None, status="failed")


__all__ = ["FallbackOutcome", "resolve_pdf_or_fallback"]

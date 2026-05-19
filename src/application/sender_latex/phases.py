"""Phase helpers used by the LaTeX-apply orchestrator.

Each function corresponds to one "step" of the 8-step pipeline laid out
in :mod:`src.application.sender_latex.pipeline`. Split out so the
orchestrator stays under the 50-line + cx-10 cap. Behaviour matches
the legacy ``_send_with_latex`` body 1:1.

Layout:

- :func:`prepare_blocks` - parse the resume tree, rank top blocks, LLM
  tailor, sanitize. Returns the bundle the compile + write steps need.
- :func:`compile_with_fallback` - run the tailored compile, fall back
  to the boot-warmed PDF if anything raised. Writes the
  ``resume_compile_log`` row.
- :func:`record_application` - upsert the ``applications`` row,
  transition opp state, attach the V007 audit columns, bump the
  Prometheus counter.

The sanitiser ALWAYS runs between the LLM call (inside
:func:`prepare_blocks`) and ``render`` (inside
:func:`compile_with_fallback`) - CLAUDE.md hard rule #1.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from src.common.logger import get_logger
from src.common.metrics import applications_sent_total
from src.common.types import ApplyMethod

from .audit import CompileAudit, attach_resume_audit_to_application, log_compile_outcome
from .compile_pipeline import render_and_compile
from .fallback_path import resolve_pdf_or_fallback
from .tailor import llm_tailor_blocks
from .variants import resolve_variant, resolve_variant_db_id

_log = get_logger(__name__)

_ARTIFACT_ROOT = Path("/var/lib/agent/resume_artifacts")
_TOP_K_BLOCKS = 3
_TAILORABLE_KINDS = ("event", "section", "skills_block", "project")
_BULLET_SURFACE_CAP = 5
_DESCRIPTION_PREVIEW_CHARS = 1500


@dataclass(frozen=True, slots=True)
class PreparedBlocks:
    """Output of :func:`prepare_blocks`."""

    document: Any
    manifest: Any
    top_blocks: list[Any]
    sanitized_edits: dict[str, list[str]]
    sanitizer_reject_msg: str | None
    variant_label: str
    resume_variant_id_db: int | None
    template_name: str


@dataclass(frozen=True, slots=True)
class CompileOutcome:
    """Output of :func:`compile_with_fallback`."""

    pdf_path: Path | None
    compile_status: str
    artifact_sha256: str | None
    source_hash: str | None


def _pdf_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _sanitize_edits(raw_edits: dict[str, list[str]]) -> tuple[dict[str, list[str]], str | None]:
    """Apply sanitiser per-block. Returns (sanitised, first reject message)."""
    from src.application.resume_latex.sanitizer import SanitizerReject, escape_and_check

    out: dict[str, list[str]] = {}
    reject_msg: str | None = None
    for bid, bullets in raw_edits.items():
        try:
            out[bid] = escape_and_check(bullets)
        except SanitizerReject as e:
            reject_msg = str(e)
            _log.warning("resume_sanitizer_rejected", block_id=bid, err=str(e))
            continue
    return out, reject_msg


def collect_surface_bullets(top_blocks: list[Any], sanitized_edits: dict[str, list[str]]) -> list[str]:
    """Bullets to surface in the notifier embed. Empty on fallback."""
    out: list[str] = []
    for b in top_blocks:
        if b.id in sanitized_edits:
            out.extend(sanitized_edits[b.id])
    return out[:_BULLET_SURFACE_CAP]


async def prepare_blocks(
    opp: dict[str, Any],
    prefs: dict[str, Any],
    opp_id: UUID,
    *,
    manifest_path: Path,
    resume_root: Path,
    pick_template: Any,
) -> PreparedBlocks:
    """Parse resume tree, rank top blocks, LLM tailor, sanitize."""
    from src.application.resume_latex.parser.blocks import parse as parse_resume
    from src.application.resume_latex.parser.manifest import load as load_manifest
    from src.application.resume_latex.selector import rank as select_rank

    variant_label = await resolve_variant(opp, prefs, opp_id)
    template_name = pick_template(opp, variant_label=variant_label)
    resume_variant_id_db = await resolve_variant_db_id(variant_label)

    manifest = load_manifest(manifest_path)
    document = parse_resume(manifest, resume_root)

    tailorable = [b for b in document.blocks if b.kind in _TAILORABLE_KINDS]
    top_blocks = select_rank(tailorable, opp)[:_TOP_K_BLOCKS]

    opp_summary_for_llm = {
        "title": opp.get("title"),
        "company": opp.get("company"),
        "description": (opp.get("description") or "")[:_DESCRIPTION_PREVIEW_CHARS],
    }
    raw_edits = await llm_tailor_blocks(top_blocks, opp_summary_for_llm, variant_label)
    sanitized_edits, sanitizer_reject_msg = _sanitize_edits(raw_edits)

    return PreparedBlocks(
        document=document,
        manifest=manifest,
        top_blocks=top_blocks,
        sanitized_edits=sanitized_edits,
        sanitizer_reject_msg=sanitizer_reject_msg,
        variant_label=variant_label,
        resume_variant_id_db=resume_variant_id_db,
        template_name=template_name,
    )


async def _attempt_tailored_compile(
    blocks: PreparedBlocks,
    artifact_dir: Path,
    *,
    source_root: Path,
    opp_id: UUID,
) -> tuple[Path | None, int | None, str | None, str | None]:
    """Try render+tectonic; return (pdf, dur_ms, version, stderr)."""
    from src.application.resume_latex.compile import CompileError
    from src.application.resume_latex.render import SourceDriftError

    try:
        success = await render_and_compile(
            blocks.document,
            blocks.sanitized_edits,
            artifact_dir,
            source_root=source_root,
            manifest=blocks.manifest,
            variant_label=blocks.variant_label,
            opp_id=opp_id,
        )
        return (
            success.pdf_path,
            success.duration_ms,
            success.tectonic_version,
            blocks.sanitizer_reject_msg,
        )
    except SourceDriftError as e:
        _log.warning("resume_source_drift", err=str(e), opp_id=str(opp_id))
        return None, None, None, f"source_drift: {e}"
    except CompileError as e:
        _log.warning("resume_compile_error", err=str(e), opp_id=str(opp_id))
        return None, None, None, str(e)
    except Exception as e:
        _log.exception("resume_render_unexpected_error", err=str(e), opp_id=str(opp_id))
        return None, None, None, f"render_error: {e!r}"


async def compile_with_fallback(
    blocks: PreparedBlocks,
    opp_id: UUID,
    user_id: int,
    *,
    source_root: Path,
) -> CompileOutcome:
    """Run tailored compile, fall back to boot-warmed PDF, log audit row."""
    user_root = _ARTIFACT_ROOT / str(user_id)
    user_root.mkdir(parents=True, exist_ok=True)
    artifact_dir = user_root / str(opp_id)

    pdf_path, duration_ms, tectonic_version, stderr = await _attempt_tailored_compile(
        blocks, artifact_dir, source_root=source_root, opp_id=opp_id
    )
    fb = resolve_pdf_or_fallback(
        pdf_path,
        user_id=user_id,
        variant_label=blocks.variant_label,
        opp_id=str(opp_id),
    )
    pdf_path, compile_status = fb.pdf_path, fb.status

    source_hash = next(iter(blocks.document.source_hashes.values()), None)
    artifact_sha256 = _pdf_sha256(pdf_path) if pdf_path else None
    await log_compile_outcome(
        CompileAudit(
            opportunity_id=opp_id,
            user_id=user_id,
            status=compile_status,
            source_hash=source_hash,
            artifact_sha256=artifact_sha256,
            block_overrides=blocks.sanitized_edits or None,
            compile_duration_ms=duration_ms,
            tectonic_version=tectonic_version,
            tectonic_stderr=stderr,
        )
    )
    return CompileOutcome(
        pdf_path=pdf_path,
        compile_status=compile_status,
        artifact_sha256=artifact_sha256,
        source_hash=source_hash,
    )


async def record_application(
    opp_id: UUID,
    method: ApplyMethod,
    payload: dict[str, Any],
    *,
    resume_variant_id_db: int | None,
    artifact_sha256: str | None,
    source_hash: str | None,
    compile_status: str,
) -> int:
    """Upsert applications row, transition opp state, attach audit cols."""
    from ..sender import _transition_to_applied, _upsert_application

    application_id = await _upsert_application(opp_id, method, payload, resume_variant_id=resume_variant_id_db)
    await _transition_to_applied(opp_id, application_id, method)
    await attach_resume_audit_to_application(
        application_id,
        artifact_sha256=artifact_sha256,
        source_hash=source_hash,
        status=compile_status,
    )
    applications_sent_total.labels(method=method.value).inc()
    return application_id


__all__ = [
    "CompileOutcome",
    "PreparedBlocks",
    "collect_surface_bullets",
    "compile_with_fallback",
    "prepare_blocks",
    "record_application",
]

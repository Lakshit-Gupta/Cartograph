"""Top-level orchestrator for the LaTeX apply flow.

``send_with_latex`` runs the 8-step pipeline (see package docstring).
Each step lives in a sibling module; this file is the thin glue.

CLAUDE.md hard rules preserved:

1. Sanitiser ALWAYS runs between LLM and ``render.write_partial``
   (inside :func:`phases.prepare_blocks` ->
   :func:`phases._sanitize_edits` -> :func:`phases.compile_with_fallback`).
2. ``tectonic --untrusted`` + 30 s timeout + ``kill_group=True``
   (delegated to :func:`resume_latex.compile.run`).
3. PDF metadata scrubbed (``exiftool -all:all=`` inside
   :func:`resume_latex.compile.run`).
4. PDF NEVER posted to Discord - email attachment only
   (see :func:`dispatch.publish_notify`).
5. Source-hash drift guard intact (re-checked inside
   :func:`resume_latex.render.write_partial`).
6. ``applications`` UPSERT runs under ``current_tenant()``.
7. ``applications.resume_compile_status`` written on every success.
8. LLM cost ledger ``kind="llm_writer"`` (V001-compatible).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from src.common.types import ApplyMethod

from .dispatch import dispatch_email, publish_notify
from .phases import (
    CompileOutcome,
    PreparedBlocks,
    collect_surface_bullets,
    compile_with_fallback,
    prepare_blocks,
    record_application,
)


def _build_payload(
    blocks: PreparedBlocks,
    outcome: CompileOutcome,
    *,
    cover_md: str,
    tailored_bullets: list[str],
    target: str | None,
    apply_url: str | None,
) -> dict[str, Any]:
    """Compose the ``applications.payload`` JSONB blob."""
    return {
        "variant": blocks.variant_label,
        "variant_id": blocks.resume_variant_id_db,
        "template": blocks.template_name,
        "cover_letter_markdown": cover_md,
        "tailored_bullets": tailored_bullets,
        "target": target,
        "review_url": apply_url,
        "generated_at": datetime.now(UTC).isoformat(),
        "resume_compile_status": outcome.compile_status,
        "resume_artifact_sha256": outcome.artifact_sha256,
    }


async def _finalize_apply(
    opp: dict[str, Any],
    opp_id: UUID,
    user_id: int,
    blocks: PreparedBlocks,
    outcome: CompileOutcome,
    *,
    cover_md: str,
    tailored_bullets: list[str],
    method: ApplyMethod,
    target: str | None,
) -> int:
    """Build payload, upsert applications row, publish notify."""
    payload = _build_payload(
        blocks,
        outcome,
        cover_md=cover_md,
        tailored_bullets=tailored_bullets,
        target=target,
        apply_url=opp.get("apply_url"),
    )
    application_id = await record_application(
        opp_id,
        method,
        payload,
        resume_variant_id_db=blocks.resume_variant_id_db,
        artifact_sha256=outcome.artifact_sha256,
        source_hash=outcome.source_hash,
        compile_status=outcome.compile_status,
    )
    await publish_notify(
        application_id=application_id,
        opp=opp,
        opp_id=opp_id,
        user_id=user_id,
        method=method,
        target=target,
        cover_md=cover_md,
        tailored_bullets=tailored_bullets,
        compile_status=outcome.compile_status,
    )
    return application_id


async def send_with_latex(
    opp_id: UUID,
    opp: dict[str, Any],
    profile_dict: dict[str, Any],
    profile_summary: dict[str, Any],
    prefs: dict[str, Any],
    user_id: int,
    *,
    override_cover_markdown: str | None = None,
) -> dict[str, Any]:
    """Execute the LaTeX apply pipeline end-to-end."""
    from ..cover_letter import pick_template, write_cover
    from ..sender import _manifest_path, _resume_root

    _ = profile_dict  # reserved for future hooks (parser hot-reload etc.)
    blocks = await prepare_blocks(
        opp,
        prefs,
        opp_id,
        manifest_path=_manifest_path(),
        resume_root=_resume_root(),
        pick_template=pick_template,
    )
    outcome = await compile_with_fallback(blocks, opp_id, user_id, source_root=_resume_root())
    cover_md = override_cover_markdown or await write_cover(profile_summary, opp, blocks.variant_label)
    tailored_bullets = collect_surface_bullets(blocks.top_blocks, blocks.sanitized_edits)
    method, target = await dispatch_email(
        opp,
        cover_md=cover_md,
        tailored_bullets=tailored_bullets,
        profile_summary=profile_summary,
        pdf_path=outcome.pdf_path,
        opp_id=opp_id,
    )
    application_id = await _finalize_apply(
        opp,
        opp_id,
        user_id,
        blocks,
        outcome,
        cover_md=cover_md,
        tailored_bullets=tailored_bullets,
        method=method,
        target=target,
    )
    return {
        "application_id": application_id,
        "method": method.value,
        "cover_letter_markdown": cover_md,
        "tailored_bullets": tailored_bullets,
        "target": target,
        "resume_compile_status": outcome.compile_status,
        "resume_artifact_sha256": outcome.artifact_sha256,
    }


__all__ = ["send_with_latex"]

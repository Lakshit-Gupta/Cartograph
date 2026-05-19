"""Application sender — orchestrates tailor + cover + dispatch + followup.

Phase 1 rules:
- EMAIL: send via Resend (notifiers.email.send_email).
- ATS_FORM / EMBEDDED_FORM / IN_PLATFORM / EXTERNAL: store payload, surface
  a "review-then-click" link to the user; DO NOT auto-submit web forms.
- Every send transitions opp.state → 'applied', writes an applications row,
  and publishes NotificationTask onto Streams.NOTIFY so the notifier worker
  opens a forum thread in #✅-applied.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from src.common.db import acquire, fetch_one
from src.common.logger import get_logger
from src.common.metrics import applications_sent_total
from src.common.queue import RedisQ, Streams
from src.common.secrets import get_settings
from src.common.types import ApplyMethod, OppState
from src.notifiers.email import send_email

from .cover_letter import pick_template, write_cover
from .resume_tailor import pick_variant, tailor_bullets

# Phase 2.2 — bandit variant picker. Imported lazily inside _send_with_latex
# to keep the legacy import surface unchanged for the JSON path.

_log = get_logger(__name__)


def is_latex_enabled() -> bool:
    """Return True iff the LaTeX resume subsystem feature flag is on.

    Reads ``settings.mp_resume_latex_enabled`` (Pydantic Settings, env-loaded
    from ``MP_RESUME_LATEX_ENABLED`` or SOPS-decrypted secrets.yaml). Default
    is False — the legacy JSON-template path remains active until the user
    explicitly flips the flag and restarts the applier-worker.
    """
    return bool(getattr(get_settings(), "mp_resume_latex_enabled", False))


# Artifact storage for tailored resume trees. Lives on disk (durable),
# never tmpfs — power loss must not corrupt in-flight applies. Per-user
# subdir keeps multi-tenancy a config edit, not a code edit.
_ARTIFACT_ROOT = Path("/var/lib/agent/resume_artifacts")

_FOLLOWUPS_DDL = """
CREATE TABLE IF NOT EXISTS followups (
    id              BIGSERIAL PRIMARY KEY,
    application_id  BIGINT NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    fire_at         TIMESTAMPTZ NOT NULL,
    fired_at        TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_followups_due
    ON followups (fire_at)
    WHERE fired_at IS NULL;
"""

_MAILTO_RE = re.compile(r"mailto:([^\?\s]+)", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")


# ---------------------------------------------------------------------------
# Module-load DDL: idempotent CREATE TABLE IF NOT EXISTS for followups
# ---------------------------------------------------------------------------
_DDL_APPLIED = False


async def _ensure_followups_table() -> None:
    global _DDL_APPLIED
    if _DDL_APPLIED:
        return
    try:
        async with acquire() as conn:
            await conn.execute(_FOLLOWUPS_DDL)
        _DDL_APPLIED = True
    except Exception as e:
        _log.warning("followups_ddl_failed", err=str(e))


# ---------------------------------------------------------------------------
# Profile loaders
# ---------------------------------------------------------------------------
def _profile_dir() -> Path:
    return Path(get_settings().config_root) / "profile"


def _load_profile_dict() -> dict[str, Any]:
    path = _profile_dir() / "resume.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _log.warning("resume_json_missing", path=str(path))
        return {}


def _load_prefs() -> dict[str, Any]:
    path = _profile_dir() / "prefs.yaml"
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}


def _profile_summary(profile_dict: dict[str, Any]) -> dict[str, Any]:
    basics = profile_dict.get("basics", {}) or {}
    return {
        "name": basics.get("name"),
        "email": basics.get("email"),
        "phone": basics.get("phone"),
        "location": basics.get("location"),
        "links": basics.get("links"),
        "summary": basics.get("summary"),
        "skills": profile_dict.get("skills"),
        "projects": [
            {"name": p.get("name"), "url": p.get("url"), "summary": p.get("summary")} for p in (profile_dict.get("projects") or [])
        ],
    }


# ---------------------------------------------------------------------------
# LaTeX resume subsystem path
# ---------------------------------------------------------------------------
def _resume_root() -> Path:
    return Path(get_settings().config_root) / "profile" / "my_resume"


def _manifest_path() -> Path:
    return _resume_root() / "manifest.yaml"


async def _llm_tailor_blocks(
    blocks: list[Any],  # list[Block] — imported lazily inside _send_with_latex
    opp_summary: dict[str, Any],
    variant_label: str,
) -> dict[str, list[str]]:
    """Call the LLM to rewrite the top-K block bullets. Returns block_id -> new bullets.

    On any LLM error returns an empty dict — the caller treats that as
    "no edits" and renders the untailored tree (still a fresh compile
    that benefits from PDF metadata scrub + qpdf linearisation).
    """
    from src.common.llm import chat_json, fence_untrusted, load_prompt

    block_payload = [{"id": b.id, "kind": b.kind, "title": b.title, "bullets": b.bullets} for b in blocks]
    try:
        prompt = load_prompt("resume_tailor.txt")
    except FileNotFoundError:
        _log.warning("resume_tailor_prompt_missing")
        return {}

    user = prompt.format(
        opp_summary=fence_untrusted(json.dumps(opp_summary)),
        variant_label=variant_label,
        blocks_json=json.dumps(block_payload),
    )

    try:
        data = await chat_json(
            messages=[
                {"role": "system", "content": "You rewrite resume bullets. Plain text only. Strict JSON. Never invent facts."},
                {"role": "user", "content": user},
            ],
            # usage_kind_enum doesn't have a dedicated "resume_tailor" value
            # (V001 schema predates Stage 4). The resume tailor uses the
            # openrouter_model_writer model anyway, so log against llm_writer.
            # Add a dedicated enum value in a follow-up migration if cost
            # attribution to the tailor lane becomes important.
            kind="llm_writer",
            model=get_settings().openrouter_model_writer,
            max_tokens=1200,
            temperature=0.2,
        )
    except Exception as e:
        _log.warning("resume_tailor_llm_failed", err=str(e))
        return {}

    edits_list = data.get("edits") if isinstance(data, dict) else None
    if not isinstance(edits_list, list):
        return {}

    out: dict[str, list[str]] = {}
    for entry in edits_list:
        if not isinstance(entry, dict):
            continue
        bid = entry.get("id")
        bullets = entry.get("bullets")
        if not isinstance(bid, str) or not isinstance(bullets, list):
            continue
        cleaned = [str(b).strip() for b in bullets if str(b).strip()]
        if cleaned:
            out[bid] = cleaned
    return out


async def _log_compile_outcome(
    *,
    opportunity_id: UUID,
    user_id: int,
    source_hash: str | None,
    artifact_sha256: str | None,
    block_overrides: dict[str, list[str]] | None,
    compile_duration_ms: int | None,
    tectonic_version: str | None,
    status: str,
    tectonic_stderr: str | None,
) -> None:
    """Insert one row into resume_compile_log. Best-effort; never raises."""
    try:
        async with acquire() as conn:
            await conn.execute(
                """
                INSERT INTO resume_compile_log
                    (opportunity_id, user_id, source_hash, artifact_sha256,
                     block_overrides, compile_duration_ms, tectonic_version,
                     status, tectonic_stderr)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9)
                """,
                opportunity_id,
                user_id,
                source_hash,
                artifact_sha256,
                json.dumps(block_overrides) if block_overrides is not None else None,
                compile_duration_ms,
                tectonic_version,
                status,
                tectonic_stderr,
            )
    except Exception as e:
        _log.warning("resume_compile_log_insert_failed", err=str(e), opp_id=str(opportunity_id), status=status)


async def _attach_resume_audit_to_application(
    application_id: int,
    *,
    artifact_sha256: str | None,
    source_hash: str | None,
    status: str,
) -> None:
    """Backfill the V007 columns onto an existing applications row."""
    try:
        async with acquire() as conn:
            await conn.execute(
                """
                UPDATE applications
                   SET resume_artifact_sha256 = $2,
                       resume_source_hash     = $3,
                       resume_compile_status  = $4
                 WHERE id = $1
                """,
                application_id,
                artifact_sha256,
                source_hash,
                status,
            )
    except Exception as e:
        _log.warning("applications_resume_audit_update_failed", err=str(e), application_id=application_id)


def _pdf_sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


async def _send_with_latex(
    opp_id: UUID,
    *,
    override_cover_markdown: str | None = None,
) -> dict[str, Any]:
    """The 8-step LaTeX apply flow.

    1. Parse the resume tree (cached by Document; cheap enough to re-parse
       every apply — see CLAUDE.md "Deferred to Phase 2+: inotify hot-reload").
    2. Selector ranks blocks against the opportunity.
    3. LLM rewrites the top-3 blocks (cost-gated through common/llm.py).
    4. Sanitizer escapes specials, rejects forbidden macros.
    5. Render writes ``<artifact_dir>.partial/`` then atomic-renames to
       ``.complete/`` once compile succeeds.
    6. ``compile.run`` invokes ``tectonic --untrusted`` with a 30 s timeout,
       qpdf-linearises, exiftool-scrubs metadata.
    7. ``applications`` + ``resume_compile_log`` rows inserted. Any failure
       falls back to the boot-warmed untailored PDF; still send the email.
    8. Resend posts the email with the PDF attached. PDF NEVER goes via
       Discord (hard rule #5).
    """
    # Lazy imports to keep the legacy import surface unchanged when the
    # flag is off (the LaTeX path may pull in tectonic-only modules in
    # future; today this is purely organisational).
    from src.application.resume_latex.compile import CompileError
    from src.application.resume_latex.compile import run as compile_run
    from src.application.resume_latex.fallback import get_fallback, resolve_variant_main
    from src.application.resume_latex.parser.blocks import parse as parse_resume
    from src.application.resume_latex.parser.manifest import load as load_manifest
    from src.application.resume_latex.render import (
        SourceDriftError,
        commit_complete,
        write_partial,
    )
    from src.application.resume_latex.sanitizer import SanitizerReject, escape_and_check
    from src.application.resume_latex.selector import rank as select_rank
    from src.application.resume_latex.variant_picker import (
        pick_variant_async,
        variant_id_for_label,
    )

    await _ensure_followups_table()

    opp = await _load_opp(opp_id)
    if opp is None:
        raise ValueError(f"opportunity not found: {opp_id}")

    profile_dict = _load_profile_dict()
    profile_summary = _profile_summary(profile_dict)
    prefs = _load_prefs()
    user_id = 1  # Phase 1 — single tenant; multi-tenant lands in Phase 4.

    # Phase 2.2 — bandit-picked variant. Falls through to the legacy
    # keyword-vote picker when no active variants exist in the DB (V011
    # not yet run, table empty, or single-variant config). The legacy
    # picker is also the floor for the prefs-driven default.
    try:
        bandit_label = await pick_variant_async(opp)
    except Exception as e:
        _log.warning("variant_picker_failed_falling_back_to_keyword_vote", err=str(e), opp_id=str(opp_id))
        bandit_label = ""
    variant_label = bandit_label or pick_variant(opp) or ((prefs.get("apply") or {}).get("resume_variant_default") or "backend")
    template_name = pick_template(opp, variant_label=variant_label)

    # Look up the DB id so we can record it on the applications row.
    # None when V011 isn't applied or the label is missing from the seed —
    # the apply still proceeds; we just log it as legacy.
    try:
        resume_variant_id_db = await variant_id_for_label(variant_label)
    except Exception as e:
        _log.warning("variant_id_lookup_failed", err=str(e), label=variant_label)
        resume_variant_id_db = None

    # 1. Parse the resume tree.
    manifest = load_manifest(_manifest_path())
    document = parse_resume(manifest, _resume_root())

    # 2. Select the top-K tailorable blocks. We cap K=3 to bound LLM cost.
    # `cvevent` blocks are the strongest tailoring target. We also let
    # `section`/`skills_block`/`project` flow through if they outrank events
    # on keyword vote — the selector is keyword-agnostic by design.
    tailorable = [b for b in document.blocks if b.kind in ("event", "section", "skills_block", "project")]
    top_blocks = select_rank(tailorable, opp)[:3]

    opp_summary_for_llm = {
        "title": opp.get("title"),
        "company": opp.get("company"),
        "description": (opp.get("description") or "")[:1500],
    }

    # 3. LLM tailors bullets (cost-gated).
    raw_edits = await _llm_tailor_blocks(top_blocks, opp_summary_for_llm, variant_label)

    # 4. Sanitizer enforces the macro denylist + escape.
    sanitized_edits: dict[str, list[str]] = {}
    sanitizer_reject_msg: str | None = None
    for bid, bullets in raw_edits.items():
        try:
            sanitized_edits[bid] = escape_and_check(bullets)
        except SanitizerReject as e:
            sanitizer_reject_msg = str(e)
            _log.warning("resume_sanitizer_rejected", block_id=bid, err=str(e))
            # Skip this block but keep the others — partial tailoring is
            # better than full fallback when only one block tripped a
            # forbidden macro.
            continue

    # 5. Render to <artifact_dir>.partial. The artifact dir is the durable
    # /var/lib/agent/resume_artifacts/<user_id>/<opp_id> path.
    user_root = _ARTIFACT_ROOT / str(user_id)
    user_root.mkdir(parents=True, exist_ok=True)
    artifact_dir = user_root / str(opp_id)

    pdf_path: Path | None = None
    compile_status: str = "failed"
    compile_duration_ms: int | None = None
    tectonic_version: str | None = None
    tectonic_stderr: str | None = sanitizer_reject_msg
    source_hash = next(iter(document.source_hashes.values()), None)

    try:
        partial = write_partial(
            document,
            sanitized_edits,
            artifact_dir,
            source_root=_resume_root(),
        )

        # Phase 2.2 — variant overlay. The base render produced
        # partial/<main_file> from the unmodified base tree (mmayer.tex
        # plus its sidebars + tailored bullet splices). When the picker
        # chose a non-base variant whose stub lives at
        # `variants/<label>/main.tex`, we flatten that stub's
        # `\input{../../mmayer.tex}` reference and overwrite the base
        # main file in `partial/` with the resolved variant source.
        # Tectonic still compiles `partial/<main_file>` so the existing
        # asset layout (altacv.cls, page1sidebar.tex etc.) keeps working.
        #
        # Important: the variant overlay loses the tailored splice
        # because the stubs reference mmayer.tex verbatim. Phase 2.2
        # accepts this — the user-edited variant `.tex` is the source
        # of truth for that lane. Once a variant has hand-tuned bullets,
        # rerunning the tailorer per-variant becomes Phase 2.3 work.
        variant_main_rel = manifest.variants.get(variant_label) if manifest.variants else None
        if variant_main_rel:
            variant_path = _resume_root() / variant_main_rel
            if variant_path.is_file():
                flat_source = resolve_variant_main(variant_path, _resume_root())
                (partial / manifest.main_file).write_text(flat_source, encoding="utf-8")
                _log.info(
                    "resume_variant_overlay_applied",
                    label=variant_label,
                    variant_main=str(variant_path),
                    opp_id=str(opp_id),
                )

        # 6. Compile.
        result = await compile_run(partial / manifest.main_file)
        compile_duration_ms = result.duration_ms
        tectonic_version = result.tectonic_version
        complete = commit_complete(partial)
        pdf_path = complete / manifest.main_file.replace(".tex", ".pdf")
        if not pdf_path.exists():
            # tectonic may have renamed the PDF — fall back to scanning.
            pdfs = list(complete.glob("*.pdf"))
            pdf_path = pdfs[0] if pdfs else None
        compile_status = "tailored" if pdf_path is not None else "failed"
    except SourceDriftError as e:
        tectonic_stderr = f"source_drift: {e}"
        _log.warning("resume_source_drift", err=str(e), opp_id=str(opp_id))
    except CompileError as e:
        tectonic_stderr = str(e)
        _log.warning("resume_compile_error", err=str(e), opp_id=str(opp_id))
    except Exception as e:
        tectonic_stderr = f"render_error: {e!r}"
        _log.exception("resume_render_unexpected_error", err=str(e), opp_id=str(opp_id))

    # Compile fallback: untailored PDF pre-warmed at applier boot.
    # Prefer the per-variant cached PDF (Phase 2.2) when available;
    # ``get_fallback`` automatically degrades to the unlabelled base
    # PDF if the variant warmup hadn't completed yet.
    if pdf_path is None:
        fb = get_fallback(user_id, variant_label=variant_label)
        if fb is not None:
            pdf_path = fb
            compile_status = "fallback"
        else:
            _log.warning("resume_no_fallback_available", opp_id=str(opp_id), user_id=user_id, variant=variant_label)

    artifact_sha256 = _pdf_sha256(pdf_path) if pdf_path else None

    # 7. Log compile outcome.
    await _log_compile_outcome(
        opportunity_id=opp_id,
        user_id=user_id,
        source_hash=source_hash,
        artifact_sha256=artifact_sha256,
        block_overrides=sanitized_edits or None,
        compile_duration_ms=compile_duration_ms,
        tectonic_version=tectonic_version,
        status=compile_status,
        tectonic_stderr=tectonic_stderr,
    )

    # 8. Build cover, dispatch.
    if override_cover_markdown:
        cover_md = override_cover_markdown
    else:
        cover_md = await write_cover(profile_summary, opp, variant_label)

    # Surface the tailored bullets in the embed for the user even though
    # the email already carries them inside the PDF. Empty list when
    # we fell back to the untailored PDF.
    tailored_bullets: list[str] = []
    for b in top_blocks:
        if b.id in sanitized_edits:
            tailored_bullets.extend(sanitized_edits[b.id])
    tailored_bullets = tailored_bullets[:5]

    method_raw = opp.get("apply_method") or ApplyMethod.EXTERNAL.value
    method = ApplyMethod(str(method_raw))
    target: str | None = None

    if method == ApplyMethod.EMAIL:
        target = _extract_email_target(opp.get("apply_url"), opp.get("description"))
        if not target:
            _log.warning("email_target_missing", opp_id=str(opp_id))
            method = ApplyMethod.EXTERNAL
        else:
            subject = f"{opp.get('title', 'Application')} — {profile_summary.get('name', 'Applicant')}"
            html = _render_email_html(cover_md, tailored_bullets, opp, profile_summary)
            reply_to = profile_summary.get("email")
            attachments = [pdf_path] if pdf_path is not None else None
            try:
                sent_ok = await send_email(
                    to=target,
                    subject=subject,
                    html=html,
                    reply_to=reply_to,
                    attachments=attachments,
                )
            except Exception as e:
                _log.exception("send_email_failed", err=str(e), to=target)
                sent_ok = False
            if not sent_ok:
                _log.warning("send_email_returned_false", to=target)

    if method != ApplyMethod.EMAIL:
        target = opp.get("apply_url")

    payload = {
        "variant": variant_label,
        "variant_id": resume_variant_id_db,
        "template": template_name,
        "cover_letter_markdown": cover_md,
        "tailored_bullets": tailored_bullets,
        "target": target,
        "review_url": opp.get("apply_url"),
        "generated_at": datetime.now(UTC).isoformat(),
        "resume_compile_status": compile_status,
        "resume_artifact_sha256": artifact_sha256,
    }

    application_id = await _upsert_application(
        opp_id,
        method,
        payload,
        resume_variant_id=resume_variant_id_db,
    )
    await _transition_to_applied(opp_id, application_id, method)
    await _attach_resume_audit_to_application(
        application_id,
        artifact_sha256=artifact_sha256,
        source_hash=source_hash,
        status=compile_status,
    )
    applications_sent_total.labels(method=method.value).inc()

    queue = await RedisQ.connect()
    notify_kind = "applied" if method == ApplyMethod.EMAIL else "manual_apply_ready"
    thread_title = f"{opp.get('title', '?')} @ {opp.get('company', '?')}"
    # CLAUDE.md hard rule #5: PDF is NEVER sent through Discord. We pass
    # the bullets / cover markdown so the notifier can build a text embed
    # — but no "resume_pdf_path" field. If a future notifier wants the
    # tailored bullets, that's what they get; the PDF stays on disk.
    await queue.publish(
        Streams.NOTIFY,
        {
            "kind": notify_kind,
            "user_id": user_id,
            "payload": {
                "application_id": application_id,
                "opportunity_id": str(opp_id),
                "thread_title": thread_title,
                "method": method.value,
                "target": target,
                "review_url": opp.get("apply_url"),
                "company": opp.get("company"),
                "title": opp.get("title"),
                "cover_letter_markdown": cover_md,
                "tailored_bullets": tailored_bullets,
                "resume_compile_status": compile_status,
            },
        },
    )

    return {
        "application_id": application_id,
        "method": method.value,
        "cover_letter_markdown": cover_md,
        "tailored_bullets": tailored_bullets,
        "target": target,
        "resume_compile_status": compile_status,
        "resume_artifact_sha256": artifact_sha256,
    }


# ---------------------------------------------------------------------------
# Opp loader (DB row → minimal opp-like dict)
# ---------------------------------------------------------------------------
async def _load_opp(opp_id: UUID) -> dict[str, Any] | None:
    rec = await fetch_one(
        """
        SELECT id, source_id, canonical_url, title, company, description,
               comp_min, comp_max, comp_currency, comp_period,
               location, remote_type, category, posted_at, expires_at,
               apply_url, apply_method, state
        FROM opportunities
        WHERE id = $1
        """,
        opp_id,
    )
    if rec is None:
        return None
    return dict(rec)


# ---------------------------------------------------------------------------
# Apply target resolution
# ---------------------------------------------------------------------------
def _extract_email_target(apply_url: str | None, description: str | None) -> str | None:
    """Pull a mailto address out of apply_url, falling back to description."""
    for src in (apply_url or "", description or ""):
        m = _MAILTO_RE.search(src)
        if m:
            return m.group(1).strip()
    for src in (apply_url or "", description or ""):
        m = _EMAIL_RE.search(src)
        if m:
            return m.group(0).strip()
    return None


def _render_email_html(cover_md: str, bullets: list[str], opp: dict[str, Any], profile_summary: dict[str, Any]) -> str:
    bullets_html = "".join(f"<li>{b}</li>" for b in bullets)
    name = profile_summary.get("name") or ""
    links = profile_summary.get("links") or {}
    portfolio = links.get("portfolio") or links.get("github") or ""
    cover_html = cover_md.replace("\n\n", "</p><p>").replace("\n", "<br/>")
    return (
        f'<div style="font-family:system-ui,sans-serif;font-size:14px;line-height:1.5">'
        f"<p>{cover_html}</p>"
        f"<hr/>"
        f"<p><strong>Relevant highlights:</strong></p>"
        f"<ul>{bullets_html}</ul>"
        f"<p>— {name}<br/>{portfolio}</p>"
        f"</div>"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def send_application(
    opp_id: UUID,
    *,
    override_cover_markdown: str | None = None,
) -> dict[str, Any]:
    """Tailor + send (or queue for manual review) + record + notify.

    When ``override_cover_markdown`` is supplied (e.g. a user-edited freelance
    proposal from the Discord modal), it is used verbatim and the LLM cover
    writer is skipped.

    Branches on ``is_latex_enabled()``. The LaTeX path lives in
    ``_send_with_latex``; the legacy JSON-template path is below.
    """
    if is_latex_enabled():
        try:
            return await _send_with_latex(opp_id, override_cover_markdown=override_cover_markdown)
        except Exception as e:
            # Hard rule: never silently swallow an apply. If the LaTeX
            # path raises before it can reach its own fallback (e.g. the
            # parser crashes on a malformed manifest), drop to the legacy
            # JSON path so the user's click still produces an email.
            _log.exception("latex_apply_failed_falling_back_to_json", err=str(e), opp_id=str(opp_id))

    await _ensure_followups_table()

    opp = await _load_opp(opp_id)
    if opp is None:
        raise ValueError(f"opportunity not found: {opp_id}")

    profile_dict = _load_profile_dict()
    profile_summary = _profile_summary(profile_dict)
    prefs = _load_prefs()

    variant_label = pick_variant(opp) or ((prefs.get("apply") or {}).get("resume_variant_default") or "backend")
    template_name = pick_template(opp, variant_label=variant_label)

    bullets = await tailor_bullets(profile_dict, opp, variant_label)
    if override_cover_markdown:
        cover_md = override_cover_markdown
    else:
        cover_md = await write_cover(profile_summary, opp, variant_label)

    method_raw = opp.get("apply_method") or ApplyMethod.EXTERNAL.value
    method = ApplyMethod(str(method_raw))
    target: str | None = None

    if method == ApplyMethod.EMAIL:
        target = _extract_email_target(opp.get("apply_url"), opp.get("description"))
        if not target:
            _log.warning("email_target_missing", opp_id=str(opp_id))
            method = ApplyMethod.EXTERNAL  # downgrade so we surface for manual handling
        else:
            subject = f"{opp.get('title', 'Application')} — {profile_summary.get('name', 'Applicant')}"
            html = _render_email_html(cover_md, bullets, opp, profile_summary)
            reply_to = profile_summary.get("email")
            sent_ok = False
            try:
                sent_ok = await send_email(to=target, subject=subject, html=html, reply_to=reply_to)
            except Exception as e:
                _log.exception("send_email_failed", err=str(e), to=target)
            if not sent_ok:
                _log.warning("send_email_returned_false", to=target)

    if method != ApplyMethod.EMAIL:
        target = opp.get("apply_url")

    payload = {
        "variant": variant_label,
        "template": template_name,
        "cover_letter_markdown": cover_md,
        "tailored_bullets": bullets,
        "target": target,
        "review_url": opp.get("apply_url"),
        "generated_at": datetime.now(UTC).isoformat(),
    }

    application_id = await _upsert_application(opp_id, method, payload)
    await _transition_to_applied(opp_id, application_id, method)
    applications_sent_total.labels(method=method.value).inc()

    queue = await RedisQ.connect()
    notify_kind = "applied" if method == ApplyMethod.EMAIL else "manual_apply_ready"
    thread_title = f"{opp.get('title', '?')} @ {opp.get('company', '?')}"
    await queue.publish(
        Streams.NOTIFY,
        {
            "kind": notify_kind,
            "user_id": 1,
            "payload": {
                "application_id": application_id,
                "opportunity_id": str(opp_id),
                "thread_title": thread_title,
                "method": method.value,
                "target": target,
                "review_url": opp.get("apply_url"),
                "company": opp.get("company"),
                "title": opp.get("title"),
                "cover_letter_markdown": cover_md,
                "tailored_bullets": bullets,
            },
        },
    )

    return {
        "application_id": application_id,
        "method": method.value,
        "cover_letter_markdown": cover_md,
        "tailored_bullets": bullets,
        "target": target,
    }


async def _upsert_application(
    opp_id: UUID,
    method: ApplyMethod,
    payload: dict[str, Any],
    *,
    resume_variant_id: int | None = None,
) -> int:
    """Insert or update the applications row.

    ``resume_variant_id`` (Phase 2.2) is the FK into ``resume_variants``.
    Stays ``None`` for the legacy JSON path and for any LaTeX apply that
    runs before V011 is applied — the column is nullable + FK ON DELETE
    SET NULL, so a NULL is always a safe value. The COALESCE on UPDATE
    preserves a previously-set variant id when a later send omits it.
    """
    rec = await fetch_one(
        """
        INSERT INTO applications (user_id, opportunity_id, method, payload, resume_variant_id)
        VALUES (1, $1, $2::apply_method_enum, $3::jsonb, $4)
        ON CONFLICT (user_id, opportunity_id) DO UPDATE
            SET sent_at           = NOW(),
                method            = $2::apply_method_enum,
                payload           = $3::jsonb,
                resume_variant_id = COALESCE($4, applications.resume_variant_id)
        RETURNING id
        """,
        opp_id,
        method.value,
        json.dumps(payload, default=str),
        resume_variant_id,
    )
    if rec is None:
        raise RuntimeError("applications insert returned no row")
    return int(rec["id"])


async def _transition_to_applied(opp_id: UUID, application_id: int, method: ApplyMethod) -> None:
    async with acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT state FROM opportunities WHERE id = $1 FOR UPDATE",
            opp_id,
        )
        if row is None:
            return
        from_state = row["state"]
        if from_state == OppState.APPLIED.value:
            return
        await conn.execute(
            "UPDATE opportunities SET state = $1::opp_state_enum, last_seen = NOW() WHERE id = $2",
            OppState.APPLIED.value,
            opp_id,
        )
        await conn.execute(
            """
                INSERT INTO opportunity_transitions
                    (opportunity_id, from_state, to_state, trigger, metadata)
                VALUES ($1, $2::opp_state_enum, $3::opp_state_enum, 'send_application', $4::jsonb)
                """,
            opp_id,
            from_state,
            OppState.APPLIED.value,
            json.dumps({"application_id": application_id, "method": method.value}),
        )


async def queue_followup(application_id: int, days: int = 4) -> int:
    """Schedule a followup row; scheduler reads `WHERE fired_at IS NULL AND fire_at <= NOW()`."""
    await _ensure_followups_table()
    fire_at = datetime.now(UTC) + timedelta(days=days)
    rec = await fetch_one(
        """
        INSERT INTO followups (application_id, fire_at)
        VALUES ($1, $2)
        RETURNING id
        """,
        application_id,
        fire_at,
    )
    if rec is None:
        raise RuntimeError("followups insert returned no row")
    fid = int(rec["id"])
    _log.info("followup_queued", application_id=application_id, followup_id=fid, fire_at=fire_at.isoformat())
    return fid


__all__ = ["queue_followup", "send_application"]

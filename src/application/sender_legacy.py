"""Legacy JSON-template apply path.

This is the pre-LaTeX send flow. It stays active until
``MP_RESUME_LATEX_ENABLED`` is flipped on **and** the LaTeX path
succeeds for a clean 7-day window (see CLAUDE.md "Hard rule #10 —
``MP_RESUME_LATEX_ENABLED`` feature flag. Staged rollout").

Until then it remains the primary apply path and the LaTeX fallback
when the LaTeX pipeline raises before reaching its own fallback PDF.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from src.common.logger import get_logger
from src.common.metrics import applications_sent_total
from src.common.queue import RedisQ, Streams
from src.common.types import ApplyMethod
from src.notifiers.email import send_email

from .cover_letter import pick_template, write_cover
from .resume_tailor import pick_variant, tailor_bullets

_log = get_logger(__name__)


async def _send_legacy_resend_email(
    target: str,
    *,
    opp: dict[str, Any],
    profile_summary: dict[str, Any],
    cover_md: str,
    bullets: list[str],
) -> None:
    """Send one email via Resend for the legacy (no-PDF) path."""
    from .sender import _render_email_html

    subject = f"{opp.get('title', 'Application')} - {profile_summary.get('name', 'Applicant')}"
    html = _render_email_html(cover_md, bullets, opp, profile_summary)
    reply_to = profile_summary.get("email")
    sent_ok = False
    try:
        sent_ok = await send_email(to=target, subject=subject, html=html, reply_to=reply_to)
    except Exception as e:
        _log.exception("send_email_failed", err=str(e), to=target)
    if not sent_ok:
        _log.warning("send_email_returned_false", to=target)


async def _send_email_for_legacy(
    opp: dict[str, Any],
    *,
    cover_md: str,
    bullets: list[str],
    profile_summary: dict[str, Any],
    opp_id: UUID,
) -> tuple[ApplyMethod, str | None]:
    """Resolve method+target and (for EMAIL) Resend.send_email.

    Mirrors legacy behaviour exactly - including the EMAIL -> EXTERNAL
    downgrade when no mailto is discoverable in the opp.
    """
    from .sender import _extract_email_target

    method = ApplyMethod(str(opp.get("apply_method") or ApplyMethod.EXTERNAL.value))
    target: str | None = None

    if method == ApplyMethod.EMAIL:
        target = _extract_email_target(opp.get("apply_url"), opp.get("description"))
        if not target:
            _log.warning("email_target_missing", opp_id=str(opp_id))
            method = ApplyMethod.EXTERNAL
        else:
            await _send_legacy_resend_email(
                target,
                opp=opp,
                profile_summary=profile_summary,
                cover_md=cover_md,
                bullets=bullets,
            )

    if method != ApplyMethod.EMAIL:
        target = opp.get("apply_url")
    return method, target


async def _publish_notify_legacy(
    *,
    application_id: int,
    opp: dict[str, Any],
    opp_id: UUID,
    method: ApplyMethod,
    target: str | None,
    cover_md: str,
    bullets: list[str],
) -> None:
    """Streams.NOTIFY publish — legacy payload (no resume_compile_status)."""
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


async def send_with_json_template(
    opp_id: UUID,
    opp: dict[str, Any],
    profile_dict: dict[str, Any],
    profile_summary: dict[str, Any],
    prefs: dict[str, Any],
    *,
    override_cover_markdown: str | None = None,
) -> dict[str, Any]:
    """Pre-LaTeX apply flow. JSON resume template + tailored bullets."""
    from .sender import _transition_to_applied, _upsert_application

    variant_label = pick_variant(opp) or ((prefs.get("apply") or {}).get("resume_variant_default") or "backend")
    template_name = pick_template(opp, variant_label=variant_label)

    bullets = await tailor_bullets(profile_dict, opp, variant_label)
    cover_md = override_cover_markdown or await write_cover(profile_summary, opp, variant_label)

    method, target = await _send_email_for_legacy(
        opp,
        cover_md=cover_md,
        bullets=bullets,
        profile_summary=profile_summary,
        opp_id=opp_id,
    )

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

    await _publish_notify_legacy(
        application_id=application_id,
        opp=opp,
        opp_id=opp_id,
        method=method,
        target=target,
        cover_md=cover_md,
        bullets=bullets,
    )

    return {
        "application_id": application_id,
        "method": method.value,
        "cover_letter_markdown": cover_md,
        "tailored_bullets": bullets,
        "target": target,
    }


__all__ = ["send_with_json_template"]

"""Email dispatch + notifier publish for the LaTeX apply path.

Split out of ``pipeline.py`` so the orchestrator stays under the 300-line
cap. Two functions:

- :func:`dispatch_email` - resolves the apply method and (when EMAIL)
  sends via Resend with the PDF attached. Downgrades EMAIL to EXTERNAL
  silently when no mailto address is discoverable in the opportunity.
- :func:`publish_notify` - pushes the notifier event onto
  ``Streams.NOTIFY``. **Never** includes the PDF path (CLAUDE.md hard
  rule #5: PDF stays on disk, never reaches Discord).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from src.common.logger import get_logger
from src.common.queue import RedisQ, Streams
from src.common.types import ApplyMethod
from src.notifiers.email import send_email

_log = get_logger(__name__)


async def _send_resend_email(
    target: str,
    *,
    opp: dict[str, Any],
    profile_summary: dict[str, Any],
    cover_md: str,
    tailored_bullets: list[str],
    pdf_path: Path | None,
) -> None:
    """Send one email via Resend. Logs on failure; never raises upward."""
    from ..sender import _render_email_html

    subject = f"{opp.get('title', 'Application')} - {profile_summary.get('name', 'Applicant')}"
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


async def dispatch_email(
    opp: dict[str, Any],
    *,
    cover_md: str,
    tailored_bullets: list[str],
    profile_summary: dict[str, Any],
    pdf_path: Path | None,
    opp_id: UUID,
) -> tuple[ApplyMethod, str | None]:
    """Resolve method+target and (for EMAIL) send via Resend.

    Returns the final method (may be downgraded to EXTERNAL when no
    mailto target is found) and the resolved target string.
    """
    from ..sender import _extract_email_target

    method = ApplyMethod(str(opp.get("apply_method") or ApplyMethod.EXTERNAL.value))
    target: str | None = None

    if method == ApplyMethod.EMAIL:
        target = _extract_email_target(opp.get("apply_url"), opp.get("description"))
        if not target:
            _log.warning("email_target_missing", opp_id=str(opp_id))
            method = ApplyMethod.EXTERNAL
        else:
            await _send_resend_email(
                target,
                opp=opp,
                profile_summary=profile_summary,
                cover_md=cover_md,
                tailored_bullets=tailored_bullets,
                pdf_path=pdf_path,
            )

    if method != ApplyMethod.EMAIL:
        target = opp.get("apply_url")
    return method, target


async def publish_notify(
    *,
    application_id: int,
    opp: dict[str, Any],
    opp_id: UUID,
    user_id: int,
    method: ApplyMethod,
    target: str | None,
    cover_md: str,
    tailored_bullets: list[str],
    compile_status: str,
) -> None:
    """Publish onto Streams.NOTIFY. No PDF field - hard rule #5."""
    queue = await RedisQ.connect()
    notify_kind = "applied" if method == ApplyMethod.EMAIL else "manual_apply_ready"
    thread_title = f"{opp.get('title', '?')} @ {opp.get('company', '?')}"
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


__all__ = ["dispatch_email", "publish_notify"]

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

_log = get_logger(__name__)

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
            {"name": p.get("name"), "url": p.get("url"), "summary": p.get("summary")}
            for p in (profile_dict.get("projects") or [])
        ],
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


def _render_email_html(cover_md: str, bullets: list[str], opp: dict[str, Any],
                      profile_summary: dict[str, Any]) -> str:
    bullets_html = "".join(f"<li>{b}</li>" for b in bullets)
    name = profile_summary.get("name") or ""
    links = profile_summary.get("links") or {}
    portfolio = links.get("portfolio") or links.get("github") or ""
    cover_html = cover_md.replace("\n\n", "</p><p>").replace("\n", "<br/>")
    return (
        f"<div style=\"font-family:system-ui,sans-serif;font-size:14px;line-height:1.5\">"
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
    """
    await _ensure_followups_table()

    opp = await _load_opp(opp_id)
    if opp is None:
        raise ValueError(f"opportunity not found: {opp_id}")

    profile_dict = _load_profile_dict()
    profile_summary = _profile_summary(profile_dict)
    prefs = _load_prefs()

    variant_label = pick_variant(opp) or (
        (prefs.get("apply") or {}).get("resume_variant_default") or "backend"
    )
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
            subject = f"{opp.get('title','Application')} — {profile_summary.get('name','Applicant')}"
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
    thread_title = f"{opp.get('title','?')} @ {opp.get('company','?')}"
    await queue.publish(Streams.NOTIFY, {
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
    })

    return {
        "application_id": application_id,
        "method": method.value,
        "cover_letter_markdown": cover_md,
        "tailored_bullets": bullets,
        "target": target,
    }


async def _upsert_application(opp_id: UUID, method: ApplyMethod,
                              payload: dict[str, Any]) -> int:
    rec = await fetch_one(
        """
        INSERT INTO applications (user_id, opportunity_id, method, payload)
        VALUES (1, $1, $2::apply_method_enum, $3::jsonb)
        ON CONFLICT (user_id, opportunity_id) DO UPDATE
            SET sent_at = NOW(),
                method  = $2::apply_method_enum,
                payload = $3::jsonb
        RETURNING id
        """,
        opp_id, method.value, json.dumps(payload, default=str),
    )
    if rec is None:
        raise RuntimeError("applications insert returned no row")
    return int(rec["id"])


async def _transition_to_applied(opp_id: UUID, application_id: int,
                                  method: ApplyMethod) -> None:
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
            OppState.APPLIED.value, opp_id,
        )
        await conn.execute(
            """
                INSERT INTO opportunity_transitions
                    (opportunity_id, from_state, to_state, trigger, metadata)
                VALUES ($1, $2::opp_state_enum, $3::opp_state_enum, 'send_application', $4::jsonb)
                """,
            opp_id, from_state, OppState.APPLIED.value,
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
        application_id, fire_at,
    )
    if rec is None:
        raise RuntimeError("followups insert returned no row")
    fid = int(rec["id"])
    _log.info("followup_queued", application_id=application_id, followup_id=fid,
              fire_at=fire_at.isoformat())
    return fid


__all__ = ["queue_followup", "send_application"]

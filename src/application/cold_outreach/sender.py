"""Cold-outreach orchestrator.

`run_one_cycle(user_id)` drains the user's pending target_companies queue:

    1. Pick the oldest target_company that has not been emailed in the
       past 14 days (cap module enforces).
    2. Resolve a contact via Apollo, then Hunter, then NullProvider.
    3. Run cap.allow_send to gate daily / warmup / dedupe.
    4. Draft body + subject via drafter.draft_intro.
    5. Sanitise + send via notifiers.email.send_email (NO attachments).
    6. INSERT outbound_messages row.
    7. Publish NOTIFY message so the bot posts to the cold-outreach channel.

Refuses cold-email sends whenever `cold_outreach_enabled=False`. Belt and
suspenders — cap.allow_send also checks the flag, but we short-circuit
before even fetching contacts to keep the failure mode cheap.

The cold-outreach worker calls run_one_cycle in a loop with sleep between
attempts; it does NOT consume a Redis stream because the trigger is a
daily cron, not an event.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from src.application.cold_outreach.base import Contact, OutboundProvider
from src.application.cold_outreach.cap import allow_send
from src.application.cold_outreach.drafter import Draft, draft_intro
from src.application.cold_outreach.null_provider import NullProvider
from src.application.cold_outreach.sanitizer import scrub_text, subject_hash
from src.common.db import acquire, fetch_one
from src.common.logger import get_logger
from src.common.metrics import applications_sent_total
from src.common.queue import RedisQ, Streams
from src.common.secrets import get_settings
from src.notifiers.email import send_email

_log = get_logger(__name__)


@dataclass(frozen=True)
class SendOutcome:
    """Per-attempt result. `sent=True` only when Resend returned 2xx and
    the outbound_messages row was inserted."""

    sent: bool
    reason: str
    target_company_id: int | None = None
    recipient_hash: str | None = None


def _recipient_hash(email: str) -> str:
    return hashlib.sha256(email.lower().encode("utf-8")).hexdigest()[:10]


def _make_providers() -> list[OutboundProvider]:
    """Build the ordered provider chain. Apollo first because it has richer
    title metadata; Hunter is the fallback. NullProvider always anchors so
    callers can iterate without an empty-list special case."""
    s = get_settings()
    providers: list[OutboundProvider] = []
    if s.apollo_api_key:
        from src.application.cold_outreach.apollo import ApolloProvider

        providers.append(ApolloProvider(s.apollo_api_key))
    if s.hunter_api_key:
        from src.application.cold_outreach.hunter import HunterProvider

        providers.append(HunterProvider(s.hunter_api_key))
    providers.append(NullProvider())
    return providers


async def _find_one_contact(domain: str) -> Contact | None:
    """First provider that returns at least one valid contact wins."""
    for p in _make_providers():
        contacts = await p.find_contacts(domain, limit=1)
        if contacts:
            return contacts[0]
    return None


async def _pick_target_company(user_id: int) -> dict | None:
    """Return the oldest target_company row that hasn't been cold-emailed
    in the last 14 days. NULL domain rows are skipped because we have
    nothing for the provider to look up."""
    rec = await fetch_one(
        """
        SELECT tc.id, tc.name, tc.domain, tc.mission_summary, tc.why_target
        FROM target_companies tc
        WHERE tc.user_id = $1
          AND tc.domain IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM outbound_messages om
              WHERE om.target_company_id = tc.id
                AND om.sent_at >= NOW() - INTERVAL '14 days'
          )
        ORDER BY tc.added_at ASC
        LIMIT 1
        """,
        user_id,
    )
    if rec is None:
        return None
    return {
        "id": int(rec["id"]),
        "name": rec["name"],
        "domain": rec["domain"],
        "mission_summary": rec["mission_summary"] or "",
        "why_target": rec["why_target"] or "",
    }


async def _load_profile(user_id: int) -> tuple[str, list[str]]:
    rec = await fetch_one(
        """
        SELECT COALESCE(headline,'') AS headline, COALESCE(skills,'{}') AS skills
        FROM profiles
        WHERE user_id = $1
        LIMIT 1
        """,
        user_id,
    )
    if rec is None:
        return "", []
    skills_raw = rec["skills"] or []
    skills = [str(s) for s in skills_raw if s]
    return str(rec["headline"] or ""), skills


async def _insert_outbound(
    *,
    user_id: int,
    target_company_id: int,
    contact: Contact,
    draft: Draft,
    s_hash: str,
    message_id: str | None,
) -> int:
    async with acquire() as conn:
        rec = await conn.fetchrow(
            """
            INSERT INTO outbound_messages
                (user_id, target_company_id, recipient_email, recipient_name,
                 subject, subject_hash, body_markdown, thread_id, resend_message_id)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            RETURNING id
            """,
            user_id,
            target_company_id,
            contact.email,
            contact.name,
            draft.subject,
            s_hash,
            draft.body,
            message_id,
            message_id,
        )
        return int(rec["id"]) if rec else 0


async def run_one_cycle(user_id: int = 1, *, q: RedisQ | None = None) -> SendOutcome:
    """Attempt to send ONE cold email. Returns the outcome metadata."""
    s = get_settings()
    if not s.cold_outreach_enabled:
        return SendOutcome(sent=False, reason="feature_flag_off")

    tc = await _pick_target_company(user_id)
    if tc is None:
        return SendOutcome(sent=False, reason="no_target_company_eligible")

    domain = scrub_text(tc["domain"], max_len=120).lower()
    if not domain:
        return SendOutcome(sent=False, reason="target_missing_domain", target_company_id=tc["id"])

    contact = await _find_one_contact(domain)
    if contact is None:
        return SendOutcome(sent=False, reason="no_contact_resolved", target_company_id=tc["id"])

    # Generate a Message-ID we control so the Gmail watcher can attribute
    # the reply chain back to outbound_messages via In-Reply-To headers.
    msg_id = f"<co-{uuid.uuid4().hex}@{s.resend_from_email.split('@')[-1] or 'localhost'}>"

    headline, skills = await _load_profile(user_id)
    draft = await draft_intro(
        profile_headline=headline,
        profile_skills=skills,
        company_name=tc["name"],
        mission_summary=tc["mission_summary"],
        why_target=tc["why_target"],
        contact=contact,
    )
    if draft is None:
        return SendOutcome(
            sent=False,
            reason="draft_failed",
            target_company_id=tc["id"],
            recipient_hash=_recipient_hash(contact.email),
        )

    s_hash = subject_hash(draft.subject)

    decision = await allow_send(
        user_id=user_id,
        recipient_email=contact.email,
        subject_hash=s_hash,
    )
    if not decision.ok:
        return SendOutcome(
            sent=False,
            reason=f"cap_{decision.reason}",
            target_company_id=tc["id"],
            recipient_hash=_recipient_hash(contact.email),
        )

    # Body is plain markdown — wrap in minimal HTML for Resend. Text
    # alternative is the same body verbatim.
    html_body = f"<p>{draft.body.replace(chr(10), '<br/>')}</p>"
    ok = await send_email(
        to=contact.email,
        subject=draft.subject,
        html=html_body,
        text=draft.body,
        headers={"Message-ID": msg_id},
    )
    if not ok:
        return SendOutcome(
            sent=False,
            reason="resend_failed",
            target_company_id=tc["id"],
            recipient_hash=_recipient_hash(contact.email),
        )

    row_id = await _insert_outbound(
        user_id=user_id,
        target_company_id=tc["id"],
        contact=contact,
        draft=draft,
        s_hash=s_hash,
        message_id=msg_id,
    )

    applications_sent_total.labels(method="cold_outreach").inc()
    _log.info(
        "cold_outreach_sent",
        outbound_id=row_id,
        target_company_id=tc["id"],
        recipient_hash=_recipient_hash(contact.email),
        provider=contact.source,
        subject_chars=len(draft.subject),
        body_words=len(draft.body.split()),
    )

    if q is not None:
        await q.publish(
            Streams.NOTIFY,
            {
                "kind": "cold_outreach_sent",
                "user_id": user_id,
                "outbound_id": row_id,
                "target_company_id": tc["id"],
                "company": tc["name"],
                "subject": draft.subject,
                "recipient_hash": _recipient_hash(contact.email),
                "provider": contact.source,
            },
        )

    return SendOutcome(
        sent=True,
        reason="ok",
        target_company_id=tc["id"],
        recipient_hash=_recipient_hash(contact.email),
    )

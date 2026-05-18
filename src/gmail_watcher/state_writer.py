"""Apply classifier output to DB + downstream streams.

mark_rejected / mark_interview / mark_offer
  → find matching `applications` row (by company+role, or by In-Reply-To /
    References headers if present), UPDATE response_status + response_at,
    AND transition the opportunity state — the V004 trigger logs the transition.

create_thread_response → publish NotificationTask(kind=tracker_update) onto NOTIFY.
surface_to_user        → publish onto ALERTS with the 2-sentence summary.
ignore                 → no-op.

All DB writes use src.common.db.execute / fetch_one.
"""

from __future__ import annotations

from email.message import Message
from typing import Any

from src.common.db import execute, fetch_one
from src.common.logger import get_logger
from src.common.metrics import outcome_events_total
from src.common.queue import RedisQ, Streams
from src.common.types import NotificationTask, OppState

_log = get_logger(__name__)


# next_action → (response_status text, new opp state)
_OUTCOME_MAP: dict[str, tuple[str, OppState]] = {
    "mark_rejected": ("rejected", OppState.REJECTED),
    "mark_interview": ("interview", OppState.INTERVIEW),
    "mark_offer": ("offer", OppState.OFFER),
}


# Singleton Redis publisher — initialised lazily on first publish.
_redis: RedisQ | None = None


async def _get_redis() -> RedisQ:
    global _redis
    if _redis is None:
        _redis = await RedisQ.connect()
    return _redis


def _msg_id_headers(msg: Message) -> tuple[str | None, list[str]]:
    """Returns (in_reply_to, references_list)."""
    in_reply = (msg.get("In-Reply-To") or "").strip() or None
    refs_raw = msg.get("References") or ""
    refs = [r.strip() for r in refs_raw.split() if r.strip()]
    return in_reply, refs


async def _find_application(
    *,
    company: str | None,
    role: str | None,
    in_reply_to: str | None,
    references: list[str],
) -> tuple[int, str] | None:
    """Return (application_id, opportunity_id) — by header reference, else fuzzy."""
    # 1. Header reference (apply sender records its outbound Message-ID in payload).
    candidate_msg_ids = [m for m in [in_reply_to, *references] if m]
    if candidate_msg_ids:
        rec = await fetch_one(
            """
            SELECT a.id, a.opportunity_id::text AS opportunity_id
            FROM applications a
            WHERE a.payload ? 'message_id'
              AND a.payload->>'message_id' = ANY($1::text[])
            ORDER BY a.sent_at DESC
            LIMIT 1
            """,
            candidate_msg_ids,
        )
        if rec:
            return int(rec["id"]), str(rec["opportunity_id"])

    # 2. Fuzzy: company (trigram) + role (substring) on opportunities.
    if not (company and role):
        return None
    rec = await fetch_one(
        """
        SELECT a.id, a.opportunity_id::text AS opportunity_id
        FROM applications a
        JOIN opportunities o ON o.id = a.opportunity_id
        WHERE ($1 = '' OR o.company % $1)
          AND ($2 = '' OR o.title ILIKE '%' || $2 || '%')
        ORDER BY a.sent_at DESC
        LIMIT 1
        """,
        (company or "").strip(),
        (role or "").strip(),
    )
    if rec:
        return int(rec["id"]), str(rec["opportunity_id"])
    return None


async def _apply_outcome(
    application_id: int,
    opportunity_id: str,
    response_status: str,
    new_state: OppState,
) -> None:
    await execute(
        """
        UPDATE applications
           SET response_status = $1,
               response_at     = NOW()
         WHERE id = $2
           AND (response_status IS NULL OR response_status <> $1)
        """,
        response_status,
        application_id,
    )
    # State machine trigger logs the transition row and rejects illegal moves.
    try:
        await execute(
            "UPDATE opportunities SET state = $1 WHERE id = $2::uuid AND state <> $1",
            new_state.value,
            opportunity_id,
        )
    except Exception as e:
        # Illegal transition (e.g. already 'offer' but we got a rejection) — just log.
        _log.info(
            "opp_state_transition_skipped",
            opp=opportunity_id,
            target=new_state.value,
            err=str(e),
        )


async def handle_classification(
    msg: Message,
    classification: dict[str, Any],
) -> None:
    """Apply one classifier verdict to DB + streams. Never raises."""
    try:
        next_action = str(classification.get("next_action") or "ignore")
        label = str(classification.get("label") or "unrelated")
        company = classification.get("extracted_company")
        role = classification.get("extracted_role")
        summary = str(classification.get("summary_2_sentences") or "").strip()
        confidence = float(classification.get("confidence") or 0.0)

        outcome_events_total.labels(type=label).inc()

        if next_action == "ignore":
            _log.debug("email_ignored", label=label, conf=confidence)
            return

        in_reply, refs = _msg_id_headers(msg)
        subject = (msg.get("Subject") or "").strip()
        sender = (msg.get("From") or "").strip()
        message_id = (msg.get("Message-ID") or "").strip() or None

        if next_action in _OUTCOME_MAP:
            response_status, new_state = _OUTCOME_MAP[next_action]
            found = await _find_application(
                company=company,
                role=role,
                in_reply_to=in_reply,
                references=refs,
            )
            if not found:
                _log.warning(
                    "outcome_no_match",
                    label=label,
                    company=company,
                    role=role,
                    in_reply_to=in_reply,
                )
                # Surface to user so they can manually link / fix.
                q = await _get_redis()
                await q.publish(
                    Streams.ALERTS,
                    {
                        "kind": "outcome_unmatched",
                        "label": label,
                        "summary": summary,
                        "from": sender,
                        "subject": subject,
                        "company": company,
                        "role": role,
                    },
                )
                return
            app_id, opp_id = found
            await _apply_outcome(app_id, opp_id, response_status, new_state)
            # Tell notifier to post in #📬-responses.
            q = await _get_redis()
            task = NotificationTask(
                kind="tracker_update",
                user_id=1,
                payload={
                    "opportunity_id": opp_id,
                    "application_id": app_id,
                    "response_status": response_status,
                    "label": label,
                    "summary": summary,
                    "from": sender,
                    "subject": subject,
                    "message_id": message_id,
                },
            )
            await q.publish(Streams.NOTIFY, task.model_dump(mode="json"))
            return

        if next_action == "create_thread_response":
            q = await _get_redis()
            task = NotificationTask(
                kind="tracker_update",
                user_id=1,
                payload={
                    "label": label,
                    "summary": summary,
                    "from": sender,
                    "subject": subject,
                    "message_id": message_id,
                    "company": company,
                    "role": role,
                },
            )
            await q.publish(Streams.NOTIFY, task.model_dump(mode="json"))
            return

        if next_action == "surface_to_user":
            q = await _get_redis()
            await q.publish(
                Streams.ALERTS,
                {
                    "kind": "email_surface",
                    "label": label,
                    "summary": summary,
                    "from": sender,
                    "subject": subject,
                    "company": company,
                    "role": role,
                },
            )
            return

        _log.info("email_unknown_action", next_action=next_action, label=label)
    except Exception as e:
        _log.exception("state_writer_failed", err=str(e))

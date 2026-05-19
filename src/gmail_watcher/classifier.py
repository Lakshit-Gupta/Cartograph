"""LLM-based email classifier.

Strips signatures + quoted text, builds a fenced prompt, calls chat_json.
On any failure returns the 'unrelated/ignore' fallback so callers can keep going.

Phase 2.1 addition (cold-outreach lane):
    `resolve_lane(msg)` probes the In-Reply-To / References headers against
    `outbound_messages.thread_id` and returns "outbound_messages" when the
    incoming reply chains back to a cold email we sent. Returns "applications"
    when the message-id matches a tracked application, or "unknown" when
    neither is found. Used by the gmail watcher to route mark_rejected /
    mark_replied verdicts into the correct table without breaking the
    existing applications path.

    `apply_outbound_outcome(...)` updates outbound_messages.response_status
    + response_at when a cold-outreach reply is matched. Called by the
    watcher AFTER `classify()` returns a verdict.
"""

from __future__ import annotations

import re
from email.message import Message
from typing import Any, Literal

from src.common.db import execute, fetch_one
from src.common.llm import chat_json, fence_untrusted, load_prompt
from src.common.logger import get_logger

_log = get_logger(__name__)

# Mapping from classifier `next_action` to outbound_messages.response_status.
# The set is intentionally narrower than _OUTCOME_MAP in state_writer.py
# because cold-outreach has no "interview" / "offer" semantics — the
# recipient either replied (positive or negative) or didn't.
_OUTBOUND_STATUS_MAP: dict[str, str] = {
    "mark_rejected": "rejected",
    "mark_interview": "replied",  # any positive reply counts as a hit
    "mark_offer": "replied",
    "create_thread_response": "replied",
    "surface_to_user": "replied",
}

LaneResult = Literal["applications", "outbound_messages", "unknown"]

# Common signature / quoted-reply boundaries.
_SIG_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^-- ?\s*$", re.MULTILINE),  # standard sig delim
    re.compile(r"^_{5,}\s*$", re.MULTILINE),  # ____ separator
    re.compile(r"^On .+wrote:\s*$", re.MULTILINE),  # gmail reply chain
    re.compile(r"^From:\s.+\nSent:\s.+\nTo:", re.MULTILINE),  # outlook reply
    re.compile(r"^Sent from my (iPhone|Android|Pixel).*$", re.MULTILINE | re.IGNORECASE),
)

_FALLBACK: dict[str, Any] = {
    "label": "unrelated",
    "confidence": 0.0,
    "next_action": "ignore",
    "summary_2_sentences": "",
    "extracted_company": None,
    "extracted_role": None,
}


def _decode_payload(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, AttributeError):
        return payload.decode("utf-8", errors="replace")


def _extract_text_body(msg: Message) -> str:
    """Best-effort text body extraction. Prefers text/plain over text/html."""
    if msg.is_multipart():
        text_plain: list[str] = []
        text_html: list[str] = []
        for part in msg.walk():
            ctype = part.get_content_type()
            if part.get("Content-Disposition", "").lower().startswith("attachment"):
                continue
            if ctype == "text/plain":
                text_plain.append(_decode_payload(part))
            elif ctype == "text/html":
                text_html.append(_decode_payload(part))
        if text_plain:
            return "\n".join(text_plain)
        if text_html:
            return _html_to_text("\n".join(text_html))
        return ""
    body = _decode_payload(msg)
    if (msg.get_content_type() or "").lower() == "text/html":
        return _html_to_text(body)
    return body


def _html_to_text(html: str) -> str:
    from selectolax.parser import HTMLParser  # lazy

    try:
        tree = HTMLParser(html)
        text = tree.text(separator="\n")
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _strip_signatures_and_quotes(body: str) -> str:
    earliest: int | None = None
    for pat in _SIG_PATTERNS:
        m = pat.search(body)
        if m and (earliest is None or m.start() < earliest):
            earliest = m.start()
    if earliest is not None:
        body = body[:earliest]
    # Collapse > quoted blocks.
    body = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith(">"))
    return re.sub(r"\n{3,}", "\n\n", body).strip()


async def classify(msg: Message) -> dict[str, Any]:
    """Return the classifier JSON. Never raises — falls back to unrelated/ignore."""
    try:
        sender = (msg.get("From") or "").strip()
        subject = (msg.get("Subject") or "").strip()
        body = _extract_text_body(msg) or ""
        body = _strip_signatures_and_quotes(body)
        body = body[:4000]

        prompt = load_prompt("email_classifier.txt")
        user_content = f"From: {fence_untrusted(sender)}\nSubject: {fence_untrusted(subject)}\nBody:\n{fence_untrusted(body)}\n"
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_content},
        ]
        obj = await chat_json(
            messages=messages,
            kind="llm_classifier",
            schema_hint="object",
            temperature=0.0,
            max_tokens=400,
        )
        if not isinstance(obj, dict):
            return dict(_FALLBACK)
        # Normalise — keep the shape exact even if model omits a field.
        result = dict(_FALLBACK)
        result.update({k: obj.get(k, result[k]) for k in result})
        # Coerce confidence to float in [0,1].
        try:
            result["confidence"] = max(0.0, min(1.0, float(result["confidence"])))
        except (TypeError, ValueError):
            result["confidence"] = 0.0
        return result
    except Exception as e:
        _log.warning("classifier_failed", err=str(e))
        return dict(_FALLBACK)


# ---------------------------------------------------------------------------
# Phase 2.1 — cold-outreach lane resolution.
# ---------------------------------------------------------------------------


def _message_id_candidates(msg: Message) -> list[str]:
    """Pull every Message-ID this reply chains back to."""
    in_reply = (msg.get("In-Reply-To") or "").strip() or None
    refs_raw = msg.get("References") or ""
    refs = [r.strip() for r in refs_raw.split() if r.strip()]
    return [m for m in [in_reply, *refs] if m]


async def resolve_lane(msg: Message) -> tuple[LaneResult, int | None]:
    """Identify which outbound lane (if any) the reply belongs to.

    Returns:
        ("outbound_messages", outbound_id) when the In-Reply-To / References
            chain matches an `outbound_messages.thread_id`.
        ("applications", application_id) when the chain matches an
            `applications.payload->>'message_id'`.
        ("unknown", None) otherwise.

    Probes outbound_messages FIRST because:
      (a) cold-outreach is the newer lane and we want to be sure replies
          land in the right table;
      (b) the index `idx_outbound_thread_id` is partial and tiny.

    Never raises. On DB error returns ("unknown", None) so the caller can
    still apply the applications fallback path.
    """
    candidates = _message_id_candidates(msg)
    if not candidates:
        return "unknown", None
    try:
        rec = await fetch_one(
            """
            SELECT id
            FROM outbound_messages
            WHERE thread_id = ANY($1::text[])
            ORDER BY sent_at DESC
            LIMIT 1
            """,
            candidates,
        )
        if rec is not None:
            return "outbound_messages", int(rec["id"])

        rec = await fetch_one(
            """
            SELECT id
            FROM applications
            WHERE payload ? 'message_id'
              AND payload->>'message_id' = ANY($1::text[])
            ORDER BY sent_at DESC
            LIMIT 1
            """,
            candidates,
        )
        if rec is not None:
            return "applications", int(rec["id"])
    except Exception as e:
        _log.warning("resolve_lane_db_failed", err=str(e))
    return "unknown", None


async def apply_outbound_outcome(outbound_id: int, classification: dict[str, Any]) -> bool:
    """Update outbound_messages.response_status from a classifier verdict.

    Returns True when a row was updated. Never raises.
    """
    next_action = str(classification.get("next_action") or "ignore")
    status = _OUTBOUND_STATUS_MAP.get(next_action)
    if status is None:
        return False
    try:
        await execute(
            """
            UPDATE outbound_messages
               SET response_status = $1,
                   response_at     = NOW()
             WHERE id = $2
               AND (response_status IS NULL OR response_status = 'pending')
            """,
            status,
            outbound_id,
        )
        return True
    except Exception as e:
        _log.warning("apply_outbound_outcome_failed", outbound_id=outbound_id, err=str(e))
        return False

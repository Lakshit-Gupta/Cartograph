"""Phase 2.3 — follow-up automation.

Daily 13:00 IST cron (`src/workers/scheduler.py::daily_followup_scan`)
calls into this module to:

1. Find applications older than the window (default 4 days) that have
   neither received a recorded inbound reply nor been followed up yet.
2. Draft an 80-word follow-up via the LLM writer model (cost-gated
   through `src.common.llm.chat_json`).
3. Insert a `followups` row at status='draft' — UNIQUE(application_id)
   enforces idempotency so a second cron run in the same day no-ops.
4. Publish `kind=followup_ready` onto `Streams.NOTIFY` so the Discord
   notifier worker surfaces the draft with Send / Edit / Skip buttons.

The send path lives in `send_followup` — invoked from the applier worker
when the user clicks Send on the Discord embed. It threads the reply via
Resend's `In-Reply-To` / `References` headers (CLAUDE.md hard rule:
without threading the follow-up looks like spam, reference is the
canonical Message-ID convention defined by `_RFC5322_SPEC_NUMBER`).

Feature-flagged via `settings.mp_followup_enabled` (default False). When
the flag is off the cron still runs but `find_eligible_applications`
short-circuits and returns `[]` — exactly the same shape as "no rows
old enough yet". This lets us deploy code with the flag off and flip it
via SOPS edit + scheduler restart, matching the
MP_RESUME_LATEX_ENABLED rollout pattern.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.common.db import acquire, fetch_one
from src.common.llm import chat_json, fence_untrusted, load_prompt
from src.common.logger import get_logger
from src.common.queue import RedisQ, Streams
from src.common.secrets import get_settings
from src.notifiers.email import send_email

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Module constants — replace magic numbers
# ---------------------------------------------------------------------------
# RFC 5322 governs the Message-ID / In-Reply-To / References header syntax
# we synthesise in `_build_threaded_headers`.
_RFC5322_SPEC_NUMBER = 5322

# Seconds in a day — used to compute `days_silent` from a wall-clock delta.
_SECONDS_PER_DAY = 86400

# Description truncation cap for the LLM prompt's opp_summary block.
# Keeps the prompt under the writer model's context window with headroom.
_OPP_DESCRIPTION_CHAR_CAP = 1200

# Truncation cap for the original cover-letter markdown fed back into the
# follow-up prompt. The full cover may be far longer than the model needs.
_ORIGINAL_COVER_CHAR_CAP = 1500

# LLM writer call ceiling — 80-word follow-up + JSON envelope fits well
# inside 400 output tokens.
_LLM_WRITER_MAX_TOKENS = 400
_LLM_WRITER_TEMPERATURE = 0.3

# Followup draft word cap — matches the public `draft_followup(max_words=)`
# default. Keep in sync if the prompt changes.
_DEFAULT_MAX_WORDS = 80


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ApplicationRow:
    """Minimal projection of an `applications` row + its joined `opportunity`.

    Carries everything `draft_followup` / `record_draft` / the cron / the
    notifier embed need. A frozen dataclass; tests construct instances
    directly without a DB hit.
    """

    application_id: int
    user_id: int
    opportunity_id: str
    sent_at: datetime
    method: str
    payload: dict[str, Any]
    title: str | None
    company: str | None
    description: str | None
    apply_url: str | None
    days_silent: int

    @property
    def email_target(self) -> str | None:
        """The address we originally emailed. Stored in payload['target']
        by sender.py when method=email."""
        return (self.payload or {}).get("target") or None

    @property
    def original_cover_markdown(self) -> str:
        return (self.payload or {}).get("cover_letter_markdown") or ""

    @property
    def original_message_id(self) -> str | None:
        """Resend's outbound Message-ID, if sender.py recorded it.

        sender.py does NOT currently capture this (send_email returns
        bool only), so this is best-effort — when present we build a
        properly threaded reply per the spec referenced by
        `_RFC5322_SPEC_NUMBER`; when absent the follow-up is a fresh
        thread with the same subject prefixed `Re: `.
        """
        return _extract_original_message_id(self.payload)


def _extract_original_message_id(payload: dict[str, Any] | None) -> str | None:
    """Pull the original Resend Message-ID out of an applications.payload."""
    p = payload or {}
    return p.get("resend_message_id") or p.get("message_id") or None


# ---------------------------------------------------------------------------
# Eligibility scan
# ---------------------------------------------------------------------------
_ELIGIBILITY_SQL = """
SELECT a.id              AS application_id,
       a.user_id          AS user_id,
       a.opportunity_id   AS opportunity_id,
       a.sent_at          AS sent_at,
       a.method::text     AS method,
       a.payload          AS payload,
       o.title            AS title,
       o.company          AS company,
       o.description      AS description,
       o.apply_url        AS apply_url
  FROM applications a
  JOIN opportunities o ON o.id = a.opportunity_id
 WHERE a.method = 'email'
   AND a.sent_at <= $1::timestamptz - ($2 || ' days')::interval
   AND NOT EXISTS (
        SELECT 1 FROM opportunity_transitions t
         WHERE t.opportunity_id = a.opportunity_id
           AND t.to_state IN ('interview','offer','rejected','withdrawn')
   )
   AND NOT EXISTS (
        SELECT 1 FROM followups f
         WHERE f.application_id = a.id
   )
 ORDER BY a.sent_at ASC
 LIMIT $3
"""


async def _query_candidates(*, now_ts: datetime, window_days: int, cap: int) -> list[Any]:
    """Pure SQL fetch of applications older than `window_days` that have
    no recorded inbound transition and no existing followup row.

    We request `cap + 1` rows so the caller can detect overflow.
    Returns [] on any DB error (logged at WARN).
    """
    try:
        async with acquire() as conn:
            return await conn.fetch(_ELIGIBILITY_SQL, now_ts, str(window_days), cap + 1)
    except Exception as e:
        _log.warning("followup_eligibility_query_failed", err=str(e))
        return []


def _clip_to_cap(rows: list[Any], cap: int) -> list[Any]:
    """Oldest-first ordering comes from SQL; clip to `cap` and log overflow."""
    if len(rows) > cap:
        _log.info("followup_overflow", eligible=len(rows), cap=cap)
        return rows[:cap]
    return rows


def _decode_payload(raw_payload: Any) -> dict[str, Any]:
    """asyncpg may hand back JSONB as a Python str or dict; normalise both."""
    if isinstance(raw_payload, str):
        try:
            decoded = json.loads(raw_payload)
        except Exception:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    if isinstance(raw_payload, dict):
        return raw_payload
    return {}


def _row_to_application(row: Any, *, now_ts: datetime) -> ApplicationRow:
    """Map an asyncpg Record → ApplicationRow."""
    sent_at = row["sent_at"]
    days_silent = max(0, int((now_ts - sent_at).total_seconds() // _SECONDS_PER_DAY))
    return ApplicationRow(
        application_id=int(row["application_id"]),
        user_id=int(row["user_id"]),
        opportunity_id=str(row["opportunity_id"]),
        sent_at=sent_at,
        method=str(row["method"]),
        payload=_decode_payload(row["payload"]),
        title=row["title"],
        company=row["company"],
        description=row["description"],
        apply_url=row["apply_url"],
        days_silent=days_silent,
    )


def _resolve_int(override: int | None, default: int) -> int:
    """Pick `override` when supplied, fallback to `default`. Coerce to int."""
    chosen = default if override is None else override
    return int(chosen)


def _resolve_now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


async def find_eligible_applications(
    *,
    window_days: int | None = None,
    max_count: int | None = None,
    now: datetime | None = None,
) -> list[ApplicationRow]:
    """Thin orchestrator returning today's follow-up candidates.

    See `_query_candidates` for the gating SQL contract.
    Defaults resolve from settings; pass `now` for deterministic tests.
    Returns [] when the feature flag is off.
    """
    settings = get_settings()
    if not settings.mp_followup_enabled:
        _log.debug("followup_flag_off")
        return []

    window = _resolve_int(window_days, settings.followup_window_days)
    cap = _resolve_int(max_count, settings.followup_daily_cap)
    now_ts = _resolve_now(now)

    rows = await _query_candidates(now_ts=now_ts, window_days=window, cap=cap)
    rows = _clip_to_cap(rows, cap)
    return [_row_to_application(r, now_ts=now_ts) for r in rows]


# ---------------------------------------------------------------------------
# LLM draft
# ---------------------------------------------------------------------------
def _profile_summary_for_followup() -> dict[str, Any]:
    """Minimal profile shape the prompt expects. Loaded from disk lazily
    so tests don't need the config/profile/ tree present."""
    try:
        from src.application.sender import _load_profile_dict, _profile_summary
    except Exception:
        return {}
    return _profile_summary(_load_profile_dict())


def _word_count(text: str) -> int:
    return len([w for w in text.strip().split() if w])


def _build_opp_summary(application: ApplicationRow) -> dict[str, Any]:
    """Trim the opportunity description so the prompt stays inside the
    writer model's context window with headroom."""
    return {
        "title": application.title,
        "company": application.company,
        "description": (application.description or "")[:_OPP_DESCRIPTION_CHAR_CAP],
    }


def _format_followup_user_prompt(prompt: str, application: ApplicationRow, profile_summary: dict[str, Any]) -> str | None:
    """Render the user-prompt template. Returns None on any format error."""
    try:
        return prompt.format(
            profile_summary=fence_untrusted(json.dumps(profile_summary, default=str)),
            opp_summary=fence_untrusted(json.dumps(_build_opp_summary(application), default=str)),
            original_cover_markdown=fence_untrusted(application.original_cover_markdown[:_ORIGINAL_COVER_CHAR_CAP]),
            days_silent=application.days_silent,
        )
    except Exception as e:
        _log.warning("followup_prompt_format_failed", err=str(e))
        return None


async def _call_writer(user_prompt: str) -> dict[str, Any] | None:
    """Call the LLM writer with the rendered prompt. Returns None on error."""
    settings = get_settings()
    try:
        return await chat_json(
            messages=[
                {
                    "role": "system",
                    "content": "You write follow-up emails. Plain text, under 80 words. Strict JSON. Never invent facts.",
                },
                {"role": "user", "content": user_prompt},
            ],
            kind="llm_writer",
            model=settings.openrouter_model_writer,
            max_tokens=_LLM_WRITER_MAX_TOKENS,
            temperature=_LLM_WRITER_TEMPERATURE,
        )
    except Exception as e:
        _log.warning("followup_llm_failed", err=str(e))
        return None


async def draft_followup(application: ApplicationRow, *, max_words: int = _DEFAULT_MAX_WORDS) -> str:
    """Call the LLM writer to produce an 80-word follow-up paragraph.

    On any LLM error returns a short hand-written fallback so the cron
    still records a draft row — the user can always click Edit before
    Send. We never want the cron to silently skip a row because an LLM
    call timed out.

    The output is also enforced to ``max_words`` words at the Python
    boundary — the prompt asks for it, but provider drift can blow past
    the limit. We truncate at word boundaries, never mid-word.
    """
    try:
        prompt = load_prompt("followup.txt")
    except FileNotFoundError:
        _log.warning("followup_prompt_missing")
        return _hand_fallback(application, max_words=max_words)

    user_prompt = _format_followup_user_prompt(prompt, application, _profile_summary_for_followup())
    if user_prompt is None:
        return _hand_fallback(application, max_words=max_words)

    data = await _call_writer(user_prompt)
    body = ""
    if isinstance(data, dict):
        body = str(data.get("body") or "").strip()
    if not body:
        return _hand_fallback(application, max_words=max_words)

    if _word_count(body) > max_words:
        body = _truncate_words(body, max_words)
    return body


def _hand_fallback(application: ApplicationRow, *, max_words: int) -> str:
    """A minimal, fact-free follow-up used when the LLM is unavailable."""
    role = application.title or "the role"
    company = application.company or "your team"
    text = (
        f"Following up on my application for {role} at {company} from {application.days_silent} days ago. "
        f"Happy to share more on my fit, or jump on a quick 15-minute call this week if useful. "
        f"Either way, thanks for your time."
    )
    if _word_count(text) > max_words:
        text = _truncate_words(text, max_words)
    return text


def _truncate_words(text: str, max_words: int) -> str:
    words = text.split()
    return " ".join(words[:max_words]).rstrip(",;:.") + "."


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
async def record_draft(application_id: int, body: str, *, user_id: int = 1) -> int | None:
    """Insert a draft followup row.

    Idempotent via UNIQUE(application_id) — second cron run today hits the
    conflict and returns None. The cron treats that as "already enqueued
    today, skip" which is exactly what the contract wants.

    Returns the new followup_id on insert, None on conflict.
    """
    rec = await fetch_one(
        """
        INSERT INTO followups (user_id, application_id, body_markdown, status)
        VALUES ($1, $2, $3, 'draft')
        ON CONFLICT (application_id) DO NOTHING
        RETURNING id
        """,
        user_id,
        application_id,
        body,
    )
    if rec is None:
        return None
    return int(rec["id"])


async def update_draft_body(followup_id: int, body: str) -> bool:
    """User edited the draft via the modal — persist + mark status='edited'."""
    try:
        async with acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE followups
                   SET body_markdown = $2,
                       status        = 'edited'
                 WHERE id = $1
                   AND status IN ('draft','edited')
                RETURNING id
                """,
                followup_id,
                body,
            )
        return row is not None
    except Exception as e:
        _log.warning("followup_update_body_failed", err=str(e), followup_id=followup_id)
        return False


async def mark_skipped(followup_id: int) -> bool:
    try:
        async with acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE followups
                   SET status = 'skipped'
                 WHERE id = $1
                   AND status IN ('draft','edited')
                RETURNING id
                """,
                followup_id,
            )
        return row is not None
    except Exception as e:
        _log.warning("followup_mark_skipped_failed", err=str(e), followup_id=followup_id)
        return False


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------
async def _load_followup(followup_id: int) -> dict[str, Any] | None:
    rec = await fetch_one(
        """
        SELECT f.id, f.user_id, f.application_id, f.body_markdown, f.status,
               a.opportunity_id, a.payload, a.method::text AS method,
               o.title, o.company
          FROM followups f
          JOIN applications a   ON a.id = f.application_id
          JOIN opportunities o  ON o.id = a.opportunity_id
         WHERE f.id = $1
        """,
        followup_id,
    )
    if rec is None:
        return None
    out = dict(rec)
    out["payload"] = _decode_payload(out.get("payload"))
    return out


def _build_threaded_headers(original_message_id: str | None) -> dict[str, str] | None:
    """If the original Message-ID is on file, return the threading headers
    per the spec referenced by `_RFC5322_SPEC_NUMBER` that point Resend
    at the existing thread."""
    if not original_message_id:
        return None
    # The Message-ID we store may or may not include the angle brackets.
    # Normalise: strip then re-wrap, so threading clients see the canonical
    # `<id@domain>` shape every time.
    mid = original_message_id.strip()
    if not mid.startswith("<"):
        mid = f"<{mid}>"
    if not mid.endswith(">"):
        mid = f"{mid}>"
    return {
        "In-Reply-To": mid,
        "References": mid,
    }


def _compose_subject(title: str | None, company: str | None) -> str:
    """Build the `Re: ...` subject line for an outbound follow-up."""
    title_part = title or "your role"
    if company:
        return f"Re: {title_part} — {company}"
    return f"Re: {title_part}"


async def _send_via_resend(send_kwargs: dict[str, Any], followup_id: int) -> bool:
    """`notifiers.email.send_email` wrapper that funnels all failure modes
    into a single bool return + a structured log line.

    `send_kwargs` carries `to / subject / html / reply_to / headers` —
    everything `notifiers.email.send_email` accepts on its email-path.
    """
    try:
        return await send_email(**send_kwargs)
    except Exception as e:
        _log.exception(
            "followup_send_email_failed",
            err=str(e),
            followup_id=followup_id,
            to=send_kwargs.get("to"),
        )
        return False


async def send_followup(followup_id: int) -> bool:
    """Send the draft via Resend, threaded to the original message.

    Hard gate on ``settings.mp_followup_enabled`` — even if the cron
    drafted a row before the flag was flipped off, we must not send.

    Returns True on success (status='sent' persisted), False otherwise
    (status='failed' persisted unless we got there via the flag gate, in
    which case the row stays at its existing status for retry).
    """
    settings = get_settings()
    if not settings.mp_followup_enabled:
        _log.info("followup_send_blocked_flag_off", followup_id=followup_id)
        return False

    row = await _load_followup(followup_id)
    if not _is_sendable(row, followup_id):
        return False
    assert row is not None  # narrowed by _is_sendable

    prepared = _prepare_send_inputs(row, followup_id)
    if prepared is None:
        await _mark_failed(followup_id)
        return False

    sent_ok = await _send_via_resend(prepared["send_kwargs"], followup_id=followup_id)
    if not sent_ok:
        await _mark_failed(followup_id)
        return False

    # We don't get the new Resend Message-ID back from send_email (it
    # returns bool). Once the email-helper grows a `send_email_with_id`
    # variant we can persist the new id here for further chained
    # follow-ups (Phase 2.3+).
    generated_id = f"followup-{followup_id}-{uuid.uuid4().hex[:8]}"
    await _mark_sent(followup_id, resend_message_id=generated_id)
    _log.info(
        "followup_sent",
        followup_id=followup_id,
        application_id=row["application_id"],
        opportunity_id=str(row["opportunity_id"]),
        threaded=bool(prepared["send_kwargs"]["headers"]),
        sender=prepared["sender_name"] or "(unset)",
    )
    return True


def _is_sendable(row: dict[str, Any] | None, followup_id: int) -> bool:
    """Pre-flight checks before doing any prep work."""
    if row is None:
        _log.warning("followup_send_row_missing", followup_id=followup_id)
        return False
    if row["status"] in ("sent", "skipped"):
        _log.info("followup_send_already_terminal", followup_id=followup_id, status=row["status"])
        return False
    return True


def _prepare_send_inputs(row: dict[str, Any], followup_id: int) -> dict[str, Any] | None:
    """Build the kwargs `_send_via_resend` needs, or None if the row is
    missing a body / target (caller marks failed)."""
    body = (row.get("body_markdown") or "").strip()
    if not body:
        _log.warning("followup_send_empty_body", followup_id=followup_id)
        return None

    payload = row.get("payload") or {}
    target = payload.get("target")
    if not target:
        _log.warning("followup_send_no_target", followup_id=followup_id)
        return None

    profile = _profile_summary_for_followup()
    headers = _build_threaded_headers(_extract_original_message_id(payload))
    return {
        "send_kwargs": {
            "to": target,
            "subject": _compose_subject(row.get("title"), row.get("company")),
            "html": _render_followup_html(body, profile),
            "reply_to": profile.get("email") or None,
            "headers": headers,
        },
        "sender_name": profile.get("name") or "",
    }


def _render_followup_html(body: str, profile: dict[str, Any]) -> str:
    name = profile.get("name") or ""
    links = profile.get("links") or {}
    portfolio = links.get("portfolio") or links.get("github") or ""
    body_html = body.replace("\n\n", "</p><p>").replace("\n", "<br/>")
    sig = f"<p>— {name}<br/>{portfolio}</p>" if (name or portfolio) else ""
    return f'<div style="font-family:system-ui,sans-serif;font-size:14px;line-height:1.5"><p>{body_html}</p>{sig}</div>'


async def _mark_sent(followup_id: int, *, resend_message_id: str) -> None:
    try:
        async with acquire() as conn:
            await conn.execute(
                """
                UPDATE followups
                   SET status            = 'sent',
                       sent_at           = NOW(),
                       resend_message_id = $2
                 WHERE id = $1
                """,
                followup_id,
                resend_message_id,
            )
    except Exception as e:
        _log.warning("followup_mark_sent_failed", err=str(e), followup_id=followup_id)


async def _mark_failed(followup_id: int) -> None:
    try:
        async with acquire() as conn:
            await conn.execute(
                "UPDATE followups SET status = 'failed' WHERE id = $1",
                followup_id,
            )
    except Exception as e:
        _log.warning("followup_mark_failed_update_failed", err=str(e), followup_id=followup_id)


# ---------------------------------------------------------------------------
# Cron entrypoint — called from src/workers/scheduler.py
# ---------------------------------------------------------------------------
async def _draft_one(row: ApplicationRow) -> str | None:
    """Wrap `draft_followup` so an unexpected exception becomes None
    instead of bubbling out of the cron loop."""
    try:
        return await draft_followup(row)
    except Exception as e:
        _log.exception("followup_draft_unexpected_error", err=str(e), application_id=row.application_id)
        return None


def _build_followup_notify(row: ApplicationRow, *, followup_id: int, body: str) -> dict[str, Any]:
    """Shape the Streams.NOTIFY payload for a ready draft."""
    return {
        "kind": "followup_ready",
        "user_id": row.user_id,
        "payload": {
            "followup_id": followup_id,
            "application_id": row.application_id,
            "opportunity_id": row.opportunity_id,
            "title": row.title,
            "company": row.company,
            "days_silent": row.days_silent,
            "body_markdown": body,
            "target": row.email_target,
        },
    }


async def _emit_followup_ready(q: RedisQ, row: ApplicationRow, *, followup_id: int, body: str) -> bool:
    """Publish `followup_ready` for one draft. Returns True on success."""
    notify = _build_followup_notify(row, followup_id=followup_id, body=body)
    try:
        await q.publish(Streams.NOTIFY, notify)
        return True
    except Exception as e:
        _log.warning("followup_publish_failed", err=str(e), followup_id=followup_id)
        return False


async def daily_followup_scan(q: RedisQ) -> dict[str, int]:
    """One pass of the 13:00 IST cron.

    Returns a small stats dict for observability + tests:
        {"eligible": N, "drafted": M, "published": K, "skipped_conflict": S}

    M == K when everything publishes cleanly; conflict_skipped > 0 when
    the cron ran twice in the same day (UNIQUE collision — exactly what
    we want).
    """
    rows = await find_eligible_applications()
    stats = {"eligible": len(rows), "drafted": 0, "published": 0, "skipped_conflict": 0}
    if not rows:
        return stats

    for row in rows:
        body = await _draft_one(row)
        if body is None:
            continue

        fid = await record_draft(row.application_id, body, user_id=row.user_id)
        if fid is None:
            stats["skipped_conflict"] += 1
            continue
        stats["drafted"] += 1

        if await _emit_followup_ready(q, row, followup_id=fid, body=body):
            stats["published"] += 1

    _log.info("followup_scan_done", **stats)
    return stats


__all__ = [
    "ApplicationRow",
    "daily_followup_scan",
    "draft_followup",
    "find_eligible_applications",
    "mark_skipped",
    "record_draft",
    "send_followup",
    "update_draft_body",
]

"""Daily cap + warmup ramp + dedupe guards.

The cap module is the SINGLE choke point between the orchestrator and
`outbound_messages`. Every cold email passes through `allow_send()` before
the row is INSERTed. If any check fails, we refuse the send and the caller
logs the reason.

Three independent checks (in order):

1. **Feature flag** — `cold_outreach_enabled` must be True.
2. **Warmup ramp** — sent_today must be below the ramp ceiling. The ramp
   starts at `cold_outreach_warmup_start` (default 5) and grows linearly
   to `cold_outreach_daily_cap` (default 10) over `cold_outreach_warmup_days`
   (default 5). Day 0 = warmup start. Day >= warmup_days = full daily cap.
3. **Dedupe** — recipient must NOT have received a cold email from this user
   in the past 14 days; subject_hash must NOT have been used in the past 30
   days (Resend spam heuristic).

Additionally `allow_send` refuses to email a recipient whose address appears
in `applications.payload->>'to'` (cross-lane dedupe — never cold-email a
person we already applied to through a job listing).
"""

from __future__ import annotations

from dataclasses import dataclass

from src.common.db import fetch_one
from src.common.logger import get_logger
from src.common.secrets import get_settings

_log = get_logger(__name__)


@dataclass(frozen=True)
class CapDecision:
    """Result of `allow_send`. `ok` is the only field consumers gate on;
    `reason` is structured for log + metrics correlation."""

    ok: bool
    reason: str
    sent_today: int = 0
    ramp_ceiling: int = 0


def _ramp_ceiling(sent_count_today: int, *, day_index: int | None = None) -> int:
    """Return the max cold emails allowed today.

    `day_index` is exposed as a hook for tests; production code passes None
    and the function derives the day from `_warmup_day_index` against
    outbound_messages.

    Linear ramp: start → start+slope*day → daily_cap. Capped at daily_cap.
    """
    s = get_settings()
    start = max(1, int(s.cold_outreach_warmup_start))
    cap = max(start, int(s.cold_outreach_daily_cap))
    days = max(1, int(s.cold_outreach_warmup_days))
    if day_index is None:
        # Without a day index assume Day 0. The orchestrator passes the real
        # index — this default is purely defensive for unit tests of the
        # warmup math.
        day_index = 0
    if day_index >= days:
        return cap
    # Linear interpolation. We round down so the ramp never overshoots:
    # day=0 -> start, day=days -> cap.
    slope = (cap - start) / days
    return min(cap, int(start + slope * day_index))


async def _warmup_day_index(user_id: int) -> int:
    """How many calendar days since this user's first cold email.

    Day 0 = first send. Returns the integer day index based on UTC dates.
    Negative result is clamped to 0 (defensive).
    """
    rec = await fetch_one(
        """
        SELECT MIN(sent_at)::date AS first_sent
        FROM outbound_messages
        WHERE user_id = $1
        """,
        user_id,
    )
    if not rec or rec["first_sent"] is None:
        return 0
    # Days between today (UTC) and first_sent.
    from datetime import UTC, datetime

    today = datetime.now(UTC).date()
    delta = (today - rec["first_sent"]).days
    return max(0, int(delta))


async def _sent_today(user_id: int) -> int:
    rec = await fetch_one(
        """
        SELECT COUNT(*) AS n
        FROM outbound_messages
        WHERE user_id = $1
          AND sent_at >= NOW() - INTERVAL '24 hours'
        """,
        user_id,
    )
    return int(rec["n"] if rec else 0)


async def _recipient_recently_emailed(user_id: int, recipient: str, *, days: int = 14) -> bool:
    rec = await fetch_one(
        """
        SELECT 1
        FROM outbound_messages
        WHERE user_id = $1
          AND lower(recipient_email) = lower($2)
          AND sent_at >= NOW() - ($3 || ' days')::interval
        LIMIT 1
        """,
        user_id,
        recipient,
        str(days),
    )
    return rec is not None


async def _subject_recently_used(subject_hash: str, *, days: int = 30) -> bool:
    rec = await fetch_one(
        """
        SELECT 1
        FROM outbound_messages
        WHERE subject_hash = $1
          AND sent_at >= NOW() - ($2 || ' days')::interval
        LIMIT 1
        """,
        subject_hash,
        str(days),
    )
    return rec is not None


async def _recipient_already_in_applications(user_id: int, recipient: str) -> bool:
    """True when we've already sent an inbound-listing application to this
    address; cross-lane dedupe prevents double-touching the same person.
    """
    rec = await fetch_one(
        """
        SELECT 1
        FROM applications
        WHERE user_id = $1
          AND lower(payload->>'to') = lower($2)
        LIMIT 1
        """,
        user_id,
        recipient,
    )
    return rec is not None


async def allow_send(*, user_id: int, recipient_email: str, subject_hash: str) -> CapDecision:
    """Adjudicate a single cold-email send.

    Returns CapDecision(ok=False, reason=...) on any failed check; the
    orchestrator stops before drafting or sending.
    """
    s = get_settings()

    if not s.cold_outreach_enabled:
        return CapDecision(ok=False, reason="feature_flag_off")

    if await _recipient_already_in_applications(user_id, recipient_email):
        return CapDecision(ok=False, reason="recipient_in_applications")

    if await _recipient_recently_emailed(user_id, recipient_email):
        return CapDecision(ok=False, reason="recipient_recent_14d")

    if await _subject_recently_used(subject_hash):
        return CapDecision(ok=False, reason="subject_recent_30d")

    sent = await _sent_today(user_id)
    day_idx = await _warmup_day_index(user_id)
    ceiling = _ramp_ceiling(sent, day_index=day_idx)
    if sent >= ceiling:
        return CapDecision(
            ok=False,
            reason="daily_cap_reached",
            sent_today=sent,
            ramp_ceiling=ceiling,
        )

    return CapDecision(ok=True, reason="ok", sent_today=sent, ramp_ceiling=ceiling)

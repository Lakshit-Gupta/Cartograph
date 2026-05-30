"""Parse Internshala listing-card date text → absolute datetimes.

Internshala renders two date-ish signals on each listing card:

  * an application deadline — ``Apply By 30 Jun' 26`` — which becomes the
    opportunity's ``expires_at`` (the moment after which the listing can no
    longer be applied to).
  * a relative posted age — ``Posted 3 days ago`` — which becomes
    ``posted_at``.

The browser-discovery worker uses both to drop expired / stale cards before
they reach Postgres (see ``report.passes_validity``). Parsing lives here, in
one pure, corpus-tested place, so the card parser stays a thin DOM→fields map.

Both functions are **fail-open**: anything they cannot confidently parse
returns ``None`` and the caller keeps the card (Internshala's listing only
surfaces open internships by default, so a missing date is not evidence of
expiry). ``now`` is injected so the relative parser is deterministic and
testable; callers pass ``datetime.now(timezone.utc)``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

# Month name → number, keyed on the lowercased first three letters so both the
# abbreviation ("Jun") and the full name ("December") resolve off one table.
_MONTHS: dict[str, int] = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

# "30 Jun' 26", "1 Jan' 2027", "5 Aug'25" — day, month word, optional
# apostrophe, 2- or 4-digit year. The optional "Apply By" lead-in is ignored
# (the regex just searches for the date anywhere in the string).
_APPLY_BY_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-z]{3,9})\s*'?\s*(\d{2,4})",
)

# Relative-age units → days-per-unit. Hours/minutes collapse to 0 (same day).
_REL_UNIT_DAYS: dict[str, int] = {
    "day": 1,
    "week": 7,
    "month": 30,
}
_REL_RE = re.compile(r"(\d+)\s+(day|week|month)s?\s+ago", re.IGNORECASE)

# Phrases that mean "posted within the current day" → age 0.
_TODAY_MARKERS: tuple[str, ...] = ("today", "just now", "hour ago", "hours ago", "minute ago", "minutes ago")


def parse_apply_by(raw: str | None, *, now: datetime) -> datetime | None:
    """Parse an "Apply By DD Mon' YY" deadline → an inclusive end-of-day datetime.

    The returned datetime is 23:59:59 on the apply-by date (same tzinfo as
    ``now``) so the whole deadline day still counts as valid: a card whose
    deadline is *today* is kept, one whose deadline was *yesterday* is dropped.
    A 2-digit year is read as ``2000 + YY``. Returns ``None`` for empty /
    unparseable input or an impossible date (e.g. ``32 Jun``).
    """
    if not raw:
        return None
    match = _APPLY_BY_RE.search(raw)
    if match is None:
        return None
    day = int(match.group(1))
    month = _MONTHS.get(match.group(2)[:3].lower())
    if month is None:
        return None
    year = int(match.group(3))
    if year < 100:
        year += 2000
    try:
        return datetime(year, month, day, 23, 59, 59, tzinfo=now.tzinfo)
    except ValueError:
        return None


def parse_posted_relative(raw: str | None, *, now: datetime) -> datetime | None:
    """Parse a relative "Posted X ago" string → an absolute posted_at.

    Handles ``Posted N days/weeks/months ago`` (months ≈ 30 days), the
    ``today`` / ``just now`` / ``an hour ago`` family (-> ``now``), and
    ``yesterday`` (-> ``now`` - 1 day). Returns ``None`` for anything else.
    """
    if not raw:
        return None
    text = raw.strip().lower()
    if not text:
        return None

    if "yesterday" in text:
        return now - timedelta(days=1)
    if any(marker in text for marker in _TODAY_MARKERS):
        return now

    match = _REL_RE.search(text)
    if match is None:
        return None
    qty = int(match.group(1))
    days = _REL_UNIT_DAYS[match.group(2).lower()]
    return now - timedelta(days=qty * days)


__all__ = ["parse_apply_by", "parse_posted_relative"]

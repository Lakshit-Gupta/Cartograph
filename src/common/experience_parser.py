"""Parse an Internshala jobs-card experience requirement → minimum years.

Jobs listing cards carry a required-experience field ("Fresher", "0-2 years",
"1-3 years", "Experience: 3-5 yrs", …). The jobs discovery worker keeps a job
only when this MINIMUM is within the user's `max_experience_years` cap, so this
module collapses the text to the smallest number of years the role demands.

Pure + corpus-tested. Returns ``None`` for anything it can't confidently read;
the worker's gate (`passes_experience`) then fails open and keeps the card —
a missing/garbled experience string is not grounds to drop an otherwise valid
job.
"""

from __future__ import annotations

import re

# Phrases that mean "no prior experience needed" → 0 years, checked before the
# numeric regex so "Fresher (0-1 years)" resolves to 0 not 0-via-range.
_ZERO_MARKERS: tuple[str, ...] = ("fresher", "no experience")

# Leading integer of an experience figure, tolerating a range upper bound
# ("1-3"), a plus ("5+"), and the year unit in its common spellings. The
# captured group is always the MINIMUM (the first number).
_YEARS_RE = re.compile(r"(\d+)\s*(?:\+|-\s*\d+)?\s*(?:years?|yrs?)\b", re.IGNORECASE)


def parse_experience_years_min(raw: str | None) -> int | None:
    """Return the minimum years of experience a jobs card requires, or None.

    "Fresher" / "No experience" → 0. "0-2 years" → 0, "1-3 years" → 1,
    "2-4 years" → 2, "5+ years" → 5, "Experience: 3-5 yrs" → 3, "1 year" → 1.
    Empty / non-numeric / unrecognised input → None (caller fails open).
    """
    if not raw:
        return None
    lowered = raw.lower()
    if any(marker in lowered for marker in _ZERO_MARKERS):
        return 0
    match = _YEARS_RE.search(lowered)
    if match is None:
        return None
    return int(match.group(1))


__all__ = ["parse_experience_years_min"]

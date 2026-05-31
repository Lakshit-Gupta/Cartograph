"""Pure (IO-free) jobs gates — strict salary floor + experience cap.

These are the two jobs-specific predicates the discovery cycle applies after
parsing a card (the third gate, `passes_validity`, is reused unchanged from the
internship `report` module). Side-effect-free so they unit-test without a
browser, Redis, or Postgres.
"""

from __future__ import annotations

import re

from src.common.currency import to_inr_per_month
from src.common.types import Opportunity


def _matches_any(text: str, terms: list[str]) -> bool:
    """True if any term appears in `text` as a whole word (word-boundary).

    Word-boundary, NEVER substring — substring kills legit roles ("ml" -> "html",
    "ai" -> "email"). Multi-word terms ("full stack") match as a phrase.
    """
    for term in terms:
        t = term.strip().lower()
        if t and re.search(rf"\b{re.escape(t)}\b", text):
            return True
    return False


def passes_keywords(opp: Opportunity, include_terms: list[str], exclude_terms: list[str]) -> bool:
    """Field-relevance gate on the job TITLE (jobs have no category dropdown).

    Exclude wins: if the title matches any `exclude_terms` (e.g. sales, marketing,
    mechanical, medical) the job is dropped. Otherwise, when `include_terms` is
    non-empty the title MUST match at least one (backend / python / ML / ...) —
    this is the positive field filter that keeps off-field roles out. Empty
    include list = no positive requirement (keep all non-excluded).
    """
    title = (opp.title or "").lower()
    if exclude_terms and _matches_any(title, exclude_terms):
        return False
    # No include list = no positive requirement; else the title must match one.
    return not include_terms or _matches_any(title, include_terms)


def passes_salary_floor(opp: Opportunity, floor_inr: float) -> bool:
    """True when the opp's MINIMUM salary normalises to >= `floor_inr` INR/month.

    STRICT lower bound — unlike the internship floor (which clears on the range
    upper bound), a job clears only if its `comp_min` is at or above the floor.
    A "₹8L-13L" job is DROPPED (its min, ₹8L, is below 12 LPA) even though its
    max clears. A null comp_min or an unconvertible currency is sub-floor and
    dropped. Currency + period conversion (USD->INR 83x, year->/12) is delegated
    to `to_inr_per_month`, so a foreign-currency salary is converted before the
    comparison.
    """
    native = opp.comp_min
    if native is None:
        return False
    monthly = to_inr_per_month(native, opp.comp_currency, opp.comp_period)
    if monthly is None:
        return False
    return monthly >= floor_inr


def passes_experience(opp: Opportunity, max_years: int) -> bool:
    """True when the job's required minimum experience is within the cap.

    Fail-open: a card with no parseable experience (`years_experience_min is
    None`) is KEPT — a missing requirement is not grounds to drop an otherwise
    qualifying job. Otherwise keep iff the minimum required years <= `max_years`.
    """
    if opp.years_experience_min is None:
        return True
    return opp.years_experience_min <= max_years


__all__ = ["passes_experience", "passes_keywords", "passes_salary_floor"]

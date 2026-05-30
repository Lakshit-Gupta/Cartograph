"""Normalise Internshala stipend strings → INR per month.

Single source of truth for stipend → comp_min_inr conversion. Replaces the
ad-hoc regex scattered through `tier1_selectors/internshala.py`. Backed by a
regression corpus in `tests/fixtures/stipend_strings.json` (≥100 real strings);
any parser change must keep that corpus green.

The browser-discovery worker calls `parse_stipend(card_stipend_text)` on every
listing card, then rejects anything whose `comp_min_inr_per_month` falls below
`INTERNSHALA_COMP_FLOOR_INR` (default ₹30,000) before the card ever reaches
`stream:rank`. Unparseable / non-numeric / zero stipends return ``None`` so the
caller can drop the card outright (they are sub-floor anyway).

All currency + period → INR/month math is delegated to `src.common.currency`;
this module only does the *string parsing* (amount, range, currency symbol,
period suffix, lakh/crore/k scaling).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.common.currency import to_inr_per_month
from src.common.logger import get_logger

_log = get_logger("stipend_parser")

# Tokens that signal "no numeric stipend" regardless of any digits that might
# follow. Matched case-insensitively against the whole trimmed string. "Unpaid"
# is the critical one — it must never be read as 0-then-passed.
_NON_NUMERIC_MARKERS: tuple[str, ...] = (
    "unpaid",
    "negotiable",
    "performance based",
    "performance-based",
    "competitive",
    "as per industry",
    "as per company",
    "not disclosed",
    "to be decided",
    "tbd",
)

# Currency detection. ₹ / Rs / Rs. / INR → INR; $ / USD → USD. Default is INR
# for bare Indian stipends (Internshala is INR-native).
_USD_RE = re.compile(r"(?:US\$|\$|\bUSD\b)", re.IGNORECASE)
_INR_RE = re.compile(r"(?:₹|\bRs\.?\b|\bINR\b)", re.IGNORECASE)

# Period detection. Order matters only for readability — each alternative is
# anchored so "/yr" and "/year" both resolve to "year", etc. LPA / "p.a." imply
# a yearly figure even without a slash.
_PERIOD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("year", re.compile(r"(?:/\s*(?:year|yr|annum|annual|ann)\b|per\s+year|per\s+annum|p\.?\s*a\.?|\bL?PA\b)", re.IGNORECASE)),
    ("month", re.compile(r"(?:/\s*(?:month|mo|mon)\b|per\s+month|p\.?\s*m\.?|monthly)", re.IGNORECASE)),
    ("week", re.compile(r"(?:/\s*(?:week|wk)\b|per\s+week|weekly)", re.IGNORECASE)),
    ("day", re.compile(r"(?:/\s*(?:day)\b|per\s+day|daily)", re.IGNORECASE)),
    ("hour", re.compile(r"(?:/\s*(?:hour|hr)\b|per\s+hour|hourly)", re.IGNORECASE)),
)

# Scale suffixes attached to a number. `lakh`/`L`/`LPA` → 1e5 (and force year);
# `crore`/`Cr` → 1e7 (force year); `k`/`K` → 1e3 (period from the rest of the
# string, default month). Detected at the whole-string level because Internshala
# never mixes scales within one range.
_LAKH_RE = re.compile(r"(?:\blakhs?\b|\bL\b|\bLPA\b|(?<=\d)\s*L\b|(?<=\d)\s*lpa\b)", re.IGNORECASE)
_CRORE_RE = re.compile(r"(?:\bcrores?\b|\bCr\b|(?<=\d)\s*cr\b)", re.IGNORECASE)
_K_RE = re.compile(r"(?<=\d)\s*[kK]\b")

# A single number: optional Indian/Western digit grouping, optional decimal.
# Matches "15,000", "2,00,000", "15000", "2.5", "10000.50".
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Range separators Internshala uses: ASCII hyphen, en-dash, em-dash, "to".
_RANGE_SPLIT_RE = re.compile(r"\s*(?:-|–|—|\bto\b)\s*", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ParsedStipend:
    """Normalised stipend.

    ``comp_min_native`` / ``comp_max_native`` are the raw parsed numbers in the
    native currency (max == min for a single value). ``native_period`` is one of
    ``month`` / ``year`` / ``week`` / ``day`` / ``hour``. The ``*_inr_per_month``
    fields are the native amounts pushed through
    :func:`src.common.currency.to_inr_per_month` and are what the comp floor is
    compared against.
    """

    comp_min_inr_per_month: float | None
    comp_max_inr_per_month: float | None
    comp_min_native: float | None
    comp_max_native: float | None
    native_currency: str
    native_period: str
    raw: str


def _detect_currency(text: str) -> str:
    """₹/Rs/INR → INR, $/USD → USD. Default INR (Internshala is INR-native)."""
    if _USD_RE.search(text):
        return "USD"
    # ₹ / Rs / INR or no symbol at all → INR.
    return "INR"


def _detect_period(text: str, *, forced: str | None) -> str:
    """Resolve the period suffix. ``forced`` wins (lakh/crore imply year)."""
    if forced is not None:
        return forced
    for period, pattern in _PERIOD_PATTERNS:
        if pattern.search(text):
            return period
    # Internshala internships are monthly by default when no period is written.
    return "month"


def _parse_numbers(text: str, *, scale: float) -> tuple[float, float] | None:
    """Extract the min/max numeric amounts (post-scale), or None if absent.

    Splits on a range separator first so "10,000 - 15,000" yields (10000, 15000)
    rather than concatenating. A single value yields (v, v). Returns ``None``
    when no digits are present.
    """
    parts = _RANGE_SPLIT_RE.split(text)
    values: list[float] = []
    for part in parts:
        match = _NUMBER_RE.search(part)
        if match is None:
            continue
        cleaned = match.group(0).replace(",", "")
        try:
            values.append(float(cleaned) * scale)
        except ValueError:  # pragma: no cover — regex guarantees a number
            continue

    if not values:
        return None
    lo = min(values)
    hi = max(values)
    return lo, hi


def parse_stipend(raw: str) -> ParsedStipend | None:
    """Parse an Internshala stipend string into INR-per-month bounds.

    Returns ``None`` for unparseable / non-numeric input ("Negotiable",
    "Unpaid", "Performance based", "Competitive", empty / whitespace), and for a
    declared zero stipend ("Stipend: 0") — a zero stipend is sub-floor anyway,
    so the caller drops the card either way.

    Handled numeric formats: single value ("Rs 15,000"), hyphen and en-dash
    ranges ("Rs 10,000-15,000"), Indian digit grouping ("2,00,000"), k-suffix
    ("15k"), lakh ("2.5 LPA", "Rs 2.5L", "2.5 lakh"), crore ("1Cr"). For
    lakh/crore the native period is forced to ``year``.
    """
    if raw is None:
        return None

    text = raw.strip()
    if not text:
        return None

    lowered = text.lower()
    if any(marker in lowered for marker in _NON_NUMERIC_MARKERS):
        return None

    # Scale + forced-period detection (crore before lakh so "Cr" never trips the
    # single-letter lakh pattern).
    scale = 1.0
    forced_period: str | None = None
    if _CRORE_RE.search(text):
        scale = 1e7
        forced_period = "year"
    elif _LAKH_RE.search(text):
        scale = 1e5
        forced_period = "year"
    elif _K_RE.search(text):
        scale = 1e3

    numbers = _parse_numbers(text, scale=scale)
    if numbers is None:
        return None

    comp_min_native, comp_max_native = numbers

    # A declared zero stipend is sub-floor; treat as "no stipend" so the caller
    # drops the card rather than publishing a ₹0 opportunity.
    if comp_max_native <= 0:
        return None

    currency = _detect_currency(text)
    period = _detect_period(text, forced=forced_period)

    comp_min_inr = to_inr_per_month(comp_min_native, currency, period)
    comp_max_inr = to_inr_per_month(comp_max_native, currency, period)

    if comp_min_inr is None or comp_max_inr is None:
        # currency.to_inr_per_month only returns None on unknown currency, which
        # can't happen here (we only ever pass INR/USD), but guard anyway so a
        # future currency widening can't silently emit a half-populated record.
        _log.warning("stipend_inr_conversion_failed", raw=raw, currency=currency, period=period)
        return None

    return ParsedStipend(
        comp_min_inr_per_month=comp_min_inr,
        comp_max_inr_per_month=comp_max_inr,
        comp_min_native=comp_min_native,
        comp_max_native=comp_max_native,
        native_currency=currency,
        native_period=period,
        raw=raw,
    )


__all__ = ["ParsedStipend", "parse_stipend"]

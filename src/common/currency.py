"""Tiny currency converter — every comp number lands in INR.

Used by:
  - ranker_worker (writes opportunities.comp_min_inr at score time)
  - auto-apply engine (compares INR floor from prefs against the column)

Rates are static snapshots — internships + freelance gigs don't fluctuate
within a quarter-percent of these rates, and Phase 1 doesn't justify a
live FX API. Update the table when rates drift > 5% (audit weekly via
`/admin currency-status` if it ever ships).

Currencies are case-folded to upper before lookup. Anything unknown
returns None so callers can decide whether to fall back on the native
comp_min or refuse to score.
"""

from __future__ import annotations

# INR per 1 unit of foreign currency. Snapshot: 2026-05-29.
_RATES_INR_PER: dict[str, float] = {
    "INR": 1.0,
    "USD": 83.0,
    "EUR": 90.0,
    "GBP": 105.0,
    "AUD": 55.0,
    "CAD": 61.0,
    "SGD": 62.0,
    "AED": 22.6,
    "JPY": 0.55,
    "CHF": 92.0,
}


def to_inr(amount: float | None, currency: str | None) -> float | None:
    """Normalize `amount` in `currency` to INR. Returns None on missing
    inputs or unknown currency."""
    if amount is None or currency is None:
        return None
    code = currency.strip().upper()
    rate = _RATES_INR_PER.get(code)
    if rate is None:
        return None
    return float(amount) * rate


def known_currencies() -> list[str]:
    """Snapshot of currencies we'll convert. For diagnostics."""
    return sorted(_RATES_INR_PER.keys())


__all__ = ["known_currencies", "to_inr"]

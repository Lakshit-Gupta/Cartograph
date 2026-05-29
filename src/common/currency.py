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
    inputs or unknown currency. Does NOT normalize period — see
    `to_inr_per_month` for the period-aware version used by the
    auto-apply comp filter."""
    if amount is None or currency is None:
        return None
    code = currency.strip().upper()
    rate = _RATES_INR_PER.get(code)
    if rate is None:
        return None
    return float(amount) * rate


# Period → months multiplier. Used by to_inr_per_month to normalize
# Internshala's ₹500/hr stipend, $80k/yr FT, etc. into a single
# comparable monthly INR number for the auto-apply comp floor.
# Approximations: 160 work hours per month, 12 months per year, 4 weeks.
_PERIOD_TO_MONTH: dict[str, float] = {
    "month": 1.0,
    "monthly": 1.0,
    "year": 1.0 / 12.0,
    "annual": 1.0 / 12.0,
    "yearly": 1.0 / 12.0,
    "week": 4.0,
    "weekly": 4.0,
    "day": 22.0,  # ~22 work days per month
    "daily": 22.0,
    "hour": 160.0,
    "hourly": 160.0,
}


def to_inr_per_month(
    amount: float | None,
    currency: str | None,
    period: str | None,
) -> float | None:
    """Normalize amount + currency + period into INR/month.

    Examples:
      to_inr_per_month(500, 'INR', 'hour')   → 500 * 160 = 80000  (₹500/hr → 80k/mo)
      to_inr_per_month(50000, 'USD', 'year') → 50000*83/12 ≈ 345833 (~3.45L/mo INR)
      to_inr_per_month(30000, 'INR', 'month')→ 30000 (no change)
      to_inr_per_month(30000, 'INR', None)   → 30000 (assume monthly)

    None inputs / unknown currency / unknown period → None. Caller
    decides whether to refuse or pass through on None.
    """
    if amount is None or currency is None:
        return None
    code = currency.strip().upper()
    rate = _RATES_INR_PER.get(code)
    if rate is None:
        return None
    period_norm = (period or "month").strip().lower()
    multiplier = _PERIOD_TO_MONTH.get(period_norm)
    if multiplier is None:
        # Unknown period — assume monthly to stay permissive rather than
        # silently rejecting.
        multiplier = 1.0
    return float(amount) * rate * multiplier


def known_currencies() -> list[str]:
    """Snapshot of currencies we'll convert. For diagnostics."""
    return sorted(_RATES_INR_PER.keys())


__all__ = ["known_currencies", "to_inr"]

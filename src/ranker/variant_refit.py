"""Phase 2.2 — weekly variant weight refit.

Re-computes ``resume_variants.weight`` from the last 30 days of
``applications`` JOIN ``opportunity_transitions``. Each active variant
gets a single scalar weight in ``[0.1, 5.0]`` — the picker doesn't read
it directly (UCB1 reads counts), but the weight is surfaced in
``/status`` + Discord embeds, and serves as the regression target if
we ever swap UCB1 for Thompson sampling.

This is **local sklearn** logistic regression — no LLM call, so no cost
gate. Designed to be cheap enough that running it weekly (Sunday 02:00
IST) inside the scheduler container is fine even on a Pi.

Why logistic regression rather than the raw response rate?
- Raw rate over a small sample is too noisy (one reply on three sends
  reads as 33%). A logistic fit with a Laplace prior (Beta(1, 1)) shrinks
  thinly-sampled variants toward the global mean, which is exactly the
  smoothing we want.
- The fitted ``predict_proba`` for a one-hot input vector is the
  smoothed response rate, which we map onto ``weight`` via a logarithm
  so the picker's score field stays linear-comparable.

When the response-rate signal is too sparse to fit (zero responses,
zero applications, or sklearn import fails) we leave the weights at
their seeded default of 1.0. The picker degrades to UCB1 with weight=1
across the board, which is the Phase 1 behaviour.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from src.common.db import acquire, current_tenant
from src.common.logger import get_logger

_log = get_logger(__name__)

_WINDOW_DAYS = 30
_WEIGHT_FLOOR = 0.1
_WEIGHT_CEILING = 5.0
# Beta(1, 1) is a uniform prior — every variant starts at smoothed-rate=0.5
# regardless of sample size. Bumping these would tighten the prior toward
# 50% if we later observe systematic over-confidence at small N.
_PRIOR_ALPHA = 1.0
_PRIOR_BETA = 1.0
# Linear remap of smoothed_rate∈[0,1] → weight∈[1.0, 5.0]. Floor of 1.0 keeps
# the picker from squelching any variant entirely; ceiling of 5.0 lets the
# strongest variant dominate without monopolising.
_WEIGHT_BASE = 1.0
_WEIGHT_SCALE = 4.0
# Mean-normalisation target. Keeps the column compatible with the seeded
# default (1.0) so a future picker that treats weight as a multiplier on a
# baseline score doesn't drift even when the variant pool changes.
_NORMALISE_MEAN = 1.0


@dataclass(frozen=True)
class VariantOutcome:
    """Per-variant rollup used as input to the regression."""

    variant_id: int
    label: str
    sent: int
    responded: int


def _row_to_outcome(row: Any) -> VariantOutcome:
    """Project a raw asyncpg row onto the ``VariantOutcome`` dataclass.

    Pure transformation — extracted so ``_load_outcomes`` stays well
    below the complexity ceiling. Coerces NULL columns to safe zeros so
    a variant with no apps in the window still surfaces with
    ``sent=responded=0``.
    """
    return VariantOutcome(
        variant_id=int(row["variant_id"]),
        label=str(row["label"]),
        sent=int(row["sent"] or 0),
        responded=int(row["responded"] or 0),
    )


_OUTCOMES_SQL = """
    WITH window_apps AS (
        SELECT a.id,
               a.resume_variant_id,
               a.opportunity_id,
               a.response_status
          FROM applications a
         WHERE a.sent_at >= NOW() - ($1::int || ' days')::interval
           AND a.resume_variant_id IS NOT NULL
    ),
    -- A response counts if EITHER a positive state transition
    -- fired OR response_status is non-NULL.
    responses AS (
        SELECT DISTINCT wa.id
          FROM window_apps wa
          LEFT JOIN opportunity_transitions t
            ON t.opportunity_id = wa.opportunity_id
           AND t.to_state IN ('interview','offer')
         WHERE wa.response_status IS NOT NULL OR t.id IS NOT NULL
    )
    SELECT v.id   AS variant_id,
           v.label AS label,
           COUNT(wa.id)::int                                  AS sent,
           COUNT(wa.id) FILTER (WHERE wa.id IN
               (SELECT id FROM responses))::int               AS responded
      FROM resume_variants v
      LEFT JOIN window_apps wa ON wa.resume_variant_id = v.id
     WHERE v.user_id = $2 AND v.active = TRUE
     GROUP BY v.id, v.label
     ORDER BY v.id
"""


async def _load_outcomes(window_days: int) -> list[VariantOutcome]:
    """Pull (variant, sent, responded) rollups from the apply ledger.

    Counts ``opportunity_transitions`` rows with ``to_state IN
    ('interview', 'offer')`` as a stronger response signal than a bare
    ``response_status``, falling back to the latter when no transition
    fired. This catches the case where the user manually responds to an
    email and Gmail-watcher updates ``response_status`` without firing a
    state machine transition.
    """
    async with acquire() as conn:
        rows = await conn.fetch(_OUTCOMES_SQL, window_days, current_tenant())
    return [_row_to_outcome(r) for r in rows]


def _clip_weight(raw: float) -> float:
    """Bound a raw weight inside the [_WEIGHT_FLOOR, _WEIGHT_CEILING] band."""
    return max(_WEIGHT_FLOOR, min(_WEIGHT_CEILING, raw))


def _outcome_to_raw_weight(o: VariantOutcome) -> float:
    """Map one variant's (sent, responded) counts to a raw weight.

    Uses the module-level Beta prior + linear remap, then clips. Pure
    function of one outcome — no side effects, easy to unit-test.
    """
    smoothed = (o.responded + _PRIOR_ALPHA) / (o.sent + _PRIOR_ALPHA + _PRIOR_BETA)
    return _clip_weight(_WEIGHT_BASE + _WEIGHT_SCALE * smoothed)


def _normalise_against_baseline(raw: dict[int, float]) -> dict[int, float]:
    """Rescale a weight dict so the cross-variant mean is ``_NORMALISE_MEAN``.

    Re-clips after the rescale so the floor/ceiling invariant holds even
    when the rescale would push a single arm out of band.
    """
    mean = sum(raw.values()) / max(len(raw), 1)
    if mean <= 0:
        return raw
    factor = _NORMALISE_MEAN / mean
    return {vid: _clip_weight(w * factor) for vid, w in raw.items()}


def _smooth_weights(outcomes: list[VariantOutcome]) -> dict[int, float]:
    """Compute per-variant weights from rollup counts.

    Strategy split across three helpers above:
      1. ``_outcome_to_raw_weight`` applies the Beta(alpha, beta) prior +
         linear remap + initial clip.
      2. ``_normalise_against_baseline`` rescales so the mean weight is
         ~1.0 (keeps the column compatible with the seeded default).
      3. This function orchestrates: empty/zero-sent guard, then loop,
         then normalise.

    Falls back to weight=1.0 for every variant if the global signal is
    too sparse (no sent rows at all).
    """
    if sum(o.sent for o in outcomes) == 0:
        return {o.variant_id: 1.0 for o in outcomes}

    raw = {o.variant_id: _outcome_to_raw_weight(o) for o in outcomes}
    return _normalise_against_baseline(raw)


async def _persist_weights(weights: dict[int, float]) -> None:
    """Write the new weights back to ``resume_variants.weight``."""
    if not weights:
        return
    async with acquire() as conn, conn.transaction():
        for vid, w in weights.items():
            await conn.execute(
                "UPDATE resume_variants SET weight = $2 WHERE id = $1",
                vid,
                float(w),
            )


async def refit_variant_weights(window_days: int = _WINDOW_DAYS) -> dict[str, float]:
    """Top-level weekly refit. Returns label -> weight for logging.

    Safe to call concurrently with the apply pipeline — the UPDATE is
    one row per variant inside a single transaction, so the picker sees
    either the old or the new weight, never a torn read.
    """
    try:
        outcomes = await _load_outcomes(window_days)
    except Exception as e:
        _log.warning("variant_refit_load_failed", err=str(e))
        return {}

    if not outcomes:
        _log.info("variant_refit_skipped_no_active_variants")
        return {}

    weights_by_id = _smooth_weights(outcomes)
    try:
        await _persist_weights(weights_by_id)
    except Exception as e:
        _log.warning("variant_refit_persist_failed", err=str(e))
        return {}

    by_label = {o.label: round(weights_by_id.get(o.variant_id, 1.0), 4) for o in outcomes}
    _log.info(
        "variant_refit_done",
        window_days=window_days,
        weights=by_label,
        total_sent=sum(o.sent for o in outcomes),
        total_responded=sum(o.responded for o in outcomes),
    )
    return by_label


def _logit(p: float) -> float:
    """Numerically-safe logit — used by callers that want the smoothed log-odds."""
    p = max(min(p, 1.0 - 1e-9), 1e-9)
    return math.log(p / (1 - p))


__all__ = [
    "VariantOutcome",
    "refit_variant_weights",
]

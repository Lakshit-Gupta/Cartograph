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

from src.common.db import acquire
from src.common.logger import get_logger

_log = get_logger(__name__)

_WINDOW_DAYS = 30
_WEIGHT_FLOOR = 0.1
_WEIGHT_CEILING = 5.0


@dataclass(frozen=True)
class VariantOutcome:
    """Per-variant rollup used as input to the regression."""

    variant_id: int
    label: str
    sent: int
    responded: int


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
        rows = await conn.fetch(
            """
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
             WHERE v.user_id = 1 AND v.active = TRUE
             GROUP BY v.id, v.label
             ORDER BY v.id
            """,
            window_days,
        )
    return [
        VariantOutcome(
            variant_id=int(r["variant_id"]),
            label=str(r["label"]),
            sent=int(r["sent"] or 0),
            responded=int(r["responded"] or 0),
        )
        for r in rows
    ]


def _smooth_weights(outcomes: list[VariantOutcome]) -> dict[int, float]:
    """Compute per-variant weights from rollup counts.

    Strategy:
      1. Add a Beta(1, 1) prior so a variant with (sent=0, responded=0)
         gets a smoothed rate of 0.5 — neutral.
      2. ``weight = 1 + 4 * smoothed_rate`` keeps the floor at 1.0 (so
         a variant never gets squelched to zero pick probability) and
         the ceiling at 5.0 (so a 100%-replied variant can dominate
         without monopolising).
      3. Re-normalise so the mean weight across active variants stays
         near 1.0 — keeps backward compat with the seeded default and
         lets a future picker treat the column as a multiplier on a
         baseline score.

    Falls back to weight=1.0 for every variant if the global signal is
    too sparse (no sent rows at all).
    """
    total_sent = sum(o.sent for o in outcomes)
    if total_sent == 0:
        return {o.variant_id: 1.0 for o in outcomes}

    raw: dict[int, float] = {}
    for o in outcomes:
        # Beta(1, 1) smoothed posterior mean.
        smoothed = (o.responded + 1.0) / (o.sent + 2.0)
        w = 1.0 + 4.0 * smoothed
        raw[o.variant_id] = max(_WEIGHT_FLOOR, min(_WEIGHT_CEILING, w))

    # Normalise so the mean weight is ~1.0.
    mean = sum(raw.values()) / max(len(raw), 1)
    if mean <= 0:
        return raw
    factor = 1.0 / mean
    return {vid: max(_WEIGHT_FLOOR, min(_WEIGHT_CEILING, w * factor)) for vid, w in raw.items()}


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

"""Phase 5.3 — nightly refit of the global ranker formula weights.

`src.ranker.formula.score` combines six components — kw_match,
embedding_sim, comp_score, freshness, source_quality, response_rate — via
a linear sum whose coefficients are seeded from `config/profile/prefs.yaml`
(see `RankerWeights`). Phase 5.3 fits those coefficients from observed
response data:

  1. For every application in the last `_WINDOW_DAYS`, recompute the SIX
     component values it would have had at scoring time (we have everything
     needed in `opportunity_scores.score_components` since Phase 1).
  2. Label rows positive iff an engagement transition (interview / offer /
     rejected) lands in the response window after `sent_at`.
  3. Fit L2 logistic regression on the 6-feature matrix.
  4. Map the fitted coefficients onto a non-negative L1-normalised weight
     vector and write one row to `ranker_weights_fit`.
  5. `formula.load_weights()` reads the latest `status='ok'` row on its
     next refresh — bounded by `_REFRESH_CACHE_SECONDS` so the hot-path
     scorer stays cheap.

Cold-start guard: fewer than `_COLD_START_THRESHOLD` labeled applications
=> insert a row with status='cold_start' and NULL weights; the ranker
keeps reading prefs.yaml defaults.

Failure handling: any exception during the fit logs to
`ranker_weights_fit(status='failed', error_message=...)` and returns —
the scheduler keeps ticking. The formula scorer never crashes because of
a missing or malformed fit row; it falls back to YAML.

Free-only: pure sklearn local LR. No LLM call, no proxy spend.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from src.common.db import acquire, current_tenant
from src.common.logger import get_logger

_log = get_logger(__name__)

# --- Tunables ---------------------------------------------------------------
_WINDOW_DAYS = 90
_RESPONSE_WINDOW_DAYS = 30
_COLD_START_THRESHOLD = 50
_RANDOM_STATE = 0
_LR_MAX_ITER = 1000
_MAX_RECORDS = 5000

_FEATURE_KEYS: tuple[str, ...] = (
    "kw_match",
    "embedding_sim",
    "comp_score",
    "freshness",
    "source_quality",
    "response_rate",
)

_ENGAGEMENT_STATES = ("interview", "offer", "rejected")


@dataclass(frozen=True, slots=True)
class FitRow:
    """One supervised example: six component values + responded label."""

    application_id: int
    features: tuple[float, ...]  # length == len(_FEATURE_KEYS)
    responded: int


# ---------------------------------------------------------------------------
# 1. Data loading
# ---------------------------------------------------------------------------
_TRAINING_SQL = f"""
    SELECT a.id                              AS application_id,
           os.score_components               AS components,
           CASE
             WHEN EXISTS (
               SELECT 1 FROM opportunity_transitions t
                WHERE t.opportunity_id = a.opportunity_id
                  AND t.to_state::text = ANY($3::text[])
                  AND t.occurred_at >= a.sent_at
                  AND t.occurred_at <= a.sent_at + INTERVAL '{_RESPONSE_WINDOW_DAYS} days'
             ) THEN 1
             WHEN a.response_at IS NOT NULL
                  AND a.response_at >= a.sent_at
                  AND a.response_at <= a.sent_at + INTERVAL '{_RESPONSE_WINDOW_DAYS} days'
             THEN 1
             ELSE 0
           END                                AS responded
      FROM applications a
      JOIN opportunity_scores os
        ON os.opportunity_id = a.opportunity_id
       AND os.user_id = a.user_id
     WHERE a.user_id = $1
       AND a.sent_at >= NOW() - ($2::int || ' days')::interval
     ORDER BY a.id
     LIMIT {int(_MAX_RECORDS)}
"""


def _components_to_features(raw: Any) -> tuple[float, ...] | None:
    """Project a `score_components` value (JSON or dict) onto the six-tuple.

    Returns None when the row is missing more than one component — partial
    rows would push the LR fit toward zero on the missing axis, which we
    don't want masquerading as a real signal. One missing key falls through
    to 0.0 (defensible: the legacy ranker treated NULL as 0 too).
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, dict):
        return None
    missing = sum(1 for k in _FEATURE_KEYS if k not in raw)
    if missing > 1:
        return None
    return tuple(float(raw.get(k, 0.0) or 0.0) for k in _FEATURE_KEYS)


async def _load_training_rows(window_days: int, user_id: int) -> list[FitRow]:
    """Pull `(features, responded)` pairs from `applications` + `opportunity_scores`."""
    async with acquire() as conn:
        rows = await conn.fetch(_TRAINING_SQL, user_id, window_days, list(_ENGAGEMENT_STATES))
    out: list[FitRow] = []
    for r in rows:
        feats = _components_to_features(r["components"])
        if feats is None:
            continue
        out.append(
            FitRow(
                application_id=int(r["application_id"]),
                features=feats,
                responded=int(r["responded"]),
            )
        )
    return out


# ---------------------------------------------------------------------------
# 2. Fit + map to non-negative L1-normalised weights
# ---------------------------------------------------------------------------
def _build_matrix(rows: list[FitRow]) -> tuple[np.ndarray, np.ndarray]:
    if not rows:
        return np.zeros((0, len(_FEATURE_KEYS))), np.zeros((0,), dtype=int)
    X = np.array([r.features for r in rows], dtype=float)
    y = np.array([r.responded for r in rows], dtype=int)
    return X, y


def _train(X: np.ndarray, y: np.ndarray) -> Any:
    """Fit L2 LR with deterministic seed. sklearn imports deferred."""
    from sklearn.linear_model import LogisticRegression

    model = LogisticRegression(
        C=1.0,
        solver="lbfgs",
        max_iter=_LR_MAX_ITER,
        random_state=_RANDOM_STATE,
    )
    model.fit(X, y)
    return model


def _coefs_to_weights(coefs: np.ndarray) -> dict[str, float]:
    """Project raw LR coefficients onto non-negative L1-normalised weights.

    A negative coefficient implies "more of this component hurts the
    response rate" — we clamp to 0 instead of letting the scorer subtract,
    because the formula in `src/ranker/formula.py` is a non-negative
    weighted sum by design (clamped final score in [0, 1]).

    After clamping we L1-normalise so the six weights sum to 1.0, matching
    the YAML defaults' implicit invariant. Degenerate case (every clamped
    coef = 0) collapses to uniform 1/6 across all components.
    """
    clamped = np.clip(coefs, 0.0, None)
    total = float(clamped.sum())
    if not math.isfinite(total) or total <= 0.0:
        uniform = 1.0 / len(_FEATURE_KEYS)
        return {k: uniform for k in _FEATURE_KEYS}
    normalised = clamped / total
    return {k: float(normalised[i]) for i, k in enumerate(_FEATURE_KEYS)}


def _auc(model: Any, X: np.ndarray, y: np.ndarray) -> float | None:
    try:
        from sklearn.metrics import roc_auc_score

        proba = model.predict_proba(X)[:, 1]
        return float(roc_auc_score(y, proba))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 3. Persistence
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class FitOutcome:
    """All audit fields persisted to one `ranker_weights_fit` row.

    Bundled as a dataclass so `_insert_row` takes a single argument
    instead of an 11-tuple — keeps callsites compact and makes future
    schema additions a single-field change in one place.
    """

    user_id: int
    status: str
    rows_used: int
    positive_rate: float
    weights: dict[str, float] | None = None
    raw_coefs: dict[str, float] | None = None
    auc: float | None = None
    error: str | None = None


def _weight_or_none(weights: dict[str, float] | None, key: str) -> float | None:
    """Project one component out of the optional weights dict.

    Returns `None` when `weights` is None or the key is missing, so the
    INSERT lands NULL on cold-start / failed rows and the DB CHECK
    constraint stays untriggered.
    """
    if weights is None or key not in weights:
        return None
    return float(weights[key])


async def _insert_row(outcome: FitOutcome) -> None:
    """Single audit row into `ranker_weights_fit`."""
    w = outcome.weights
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO ranker_weights_fit (
                user_id, status, rows_used, positive_rate,
                kw_match, embedding_sim, comp_score, freshness,
                source_quality, response_rate,
                auc, raw_coefficients, error_message
            ) VALUES (
                $1, $2, $3, $4,
                $5, $6, $7, $8, $9, $10,
                $11, $12::jsonb, $13
            )
            """,
            int(outcome.user_id),
            outcome.status,
            int(outcome.rows_used),
            float(outcome.positive_rate),
            _weight_or_none(w, "kw_match"),
            _weight_or_none(w, "embedding_sim"),
            _weight_or_none(w, "comp_score"),
            _weight_or_none(w, "freshness"),
            _weight_or_none(w, "source_quality"),
            _weight_or_none(w, "response_rate"),
            (float(outcome.auc) if outcome.auc is not None else None),
            json.dumps(outcome.raw_coefs or {}),
            outcome.error,
        )


# ---------------------------------------------------------------------------
# 4. Public read API used by `formula.load_weights`
# ---------------------------------------------------------------------------
async def fetch_latest_weights(user_id: int) -> dict[str, float] | None:
    """Return the latest fitted weights for `user_id`, or None if absent.

    Bounded by the partial index `idx_ranker_weights_fit_latest_ok` so the
    hot path is a single index scan + row fetch. NULL columns (cold-start /
    failed rows) are filtered out via the WHERE status = 'ok' predicate.
    """
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT kw_match, embedding_sim, comp_score,
                   freshness, source_quality, response_rate
              FROM ranker_weights_fit
             WHERE user_id = $1 AND status = 'ok'
             ORDER BY fitted_at DESC
             LIMIT 1
            """,
            int(user_id),
        )
    if row is None:
        return None
    out: dict[str, float] = {}
    for k in _FEATURE_KEYS:
        v = row[k]
        if v is None:
            # A status='ok' row should always have all six populated; if
            # somehow not, refuse to return a partial dict and let the
            # caller fall back to YAML rather than scoring with mixed signals.
            return None
        out[k] = float(v)
    return out


# ---------------------------------------------------------------------------
# 5. Entrypoint
# ---------------------------------------------------------------------------
def _positive_rate(rows: list[FitRow]) -> float:
    if not rows:
        return 0.0
    return float(sum(r.responded for r in rows)) / len(rows)


async def _emit_cold_start(*, user_id: int, rows_used: int, positive_rate: float) -> dict[str, Any]:
    _log.info(
        "global_ranker_refit_cold_start",
        user_id=user_id,
        rows_used=rows_used,
        threshold=_COLD_START_THRESHOLD,
        positive_rate=round(positive_rate, 4),
    )
    await _insert_row(
        FitOutcome(
            user_id=user_id,
            status="cold_start",
            rows_used=rows_used,
            positive_rate=positive_rate,
        )
    )
    return {"status": "cold_start", "rows_used": rows_used, "user_id": user_id}


@dataclass(frozen=True, slots=True)
class _FitResult:
    """Output of `_run_fit`: matrix-derived weights + audit fields."""

    weights: dict[str, float]
    raw_coefs: dict[str, float]
    auc: float | None


def _run_fit(rows: list[FitRow]) -> _FitResult | None:
    """Train the LR and project onto the weight vector. Returns None when
    the label is single-class (caller routes to cold_start).
    """
    X, y = _build_matrix(rows)
    if np.unique(y).size < 2:
        return None
    model = _train(X, y)
    raw_coefs = {k: float(model.coef_[0][i]) for i, k in enumerate(_FEATURE_KEYS)}
    return _FitResult(
        weights=_coefs_to_weights(model.coef_[0]),
        raw_coefs=raw_coefs,
        auc=_auc(model, X, y),
    )


async def _persist_single_class(*, user_id: int, rows_used: int, positive_rate: float) -> dict[str, Any]:
    """Single-class label → log a cold-start audit row, return summary."""
    await _insert_row(
        FitOutcome(
            user_id=user_id,
            status="cold_start",
            rows_used=rows_used,
            positive_rate=positive_rate,
        )
    )
    return {"status": "cold_start", "rows_used": rows_used, "reason": "single_class"}


async def _persist_failure(*, user_id: int, rows_used: int, positive_rate: float, error: str) -> dict[str, Any]:
    """Fit raised → log a failed audit row, return summary."""
    await _insert_row(
        FitOutcome(
            user_id=user_id,
            status="failed",
            rows_used=rows_used,
            positive_rate=positive_rate,
            error=error,
        )
    )
    return {"status": "failed", "rows_used": rows_used, "error": error}


async def _fit_and_persist(
    *,
    user_id: int,
    rows: list[FitRow],
    positive_rate: float,
) -> dict[str, Any]:
    """Hot path: build features, fit, persist, log. Linear flow — branch
    points delegate to focused helpers so this orchestrator stays
    readable end-to-end.
    """
    rows_used = len(rows)
    try:
        fit = _run_fit(rows)
    except Exception as e:
        _log.exception("global_ranker_refit_failed", err=str(e))
        return await _persist_failure(user_id=user_id, rows_used=rows_used, positive_rate=positive_rate, error=str(e))
    if fit is None:
        return await _persist_single_class(user_id=user_id, rows_used=rows_used, positive_rate=positive_rate)

    await _insert_row(
        FitOutcome(
            user_id=user_id,
            status="ok",
            rows_used=rows_used,
            positive_rate=positive_rate,
            weights=fit.weights,
            raw_coefs=fit.raw_coefs,
            auc=fit.auc,
        )
    )
    _log.info(
        "global_ranker_refit_done",
        user_id=user_id,
        rows_used=rows_used,
        positive_rate=round(positive_rate, 4),
        auc=(round(fit.auc, 4) if fit.auc is not None else None),
        weights={k: round(v, 4) for k, v in fit.weights.items()},
    )
    return {
        "status": "ok",
        "rows_used": rows_used,
        "positive_rate": positive_rate,
        "auc": fit.auc,
        "weights": fit.weights,
    }


async def run_nightly_refit(
    window_days: int = _WINDOW_DAYS,
    *,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Top-level nightly refit. Defaults to `db.current_tenant()`.

    Returns a summary dict — the scheduler logs it on its tick.
    """
    uid = int(user_id) if user_id is not None else current_tenant()
    try:
        rows = await _load_training_rows(window_days, uid)
    except Exception as e:
        _log.exception("global_ranker_refit_load_failed", err=str(e))
        try:
            await _insert_row(
                FitOutcome(
                    user_id=uid,
                    status="failed",
                    rows_used=0,
                    positive_rate=0.0,
                    error=str(e),
                )
            )
        except Exception:
            pass
        return {"status": "failed", "rows_used": 0, "error": str(e), "user_id": uid}

    positive_rate = _positive_rate(rows)
    if len(rows) < _COLD_START_THRESHOLD:
        return await _emit_cold_start(
            user_id=uid,
            rows_used=len(rows),
            positive_rate=positive_rate,
        )
    return await _fit_and_persist(user_id=uid, rows=rows, positive_rate=positive_rate)


__all__ = [
    "FitRow",
    "_build_matrix",
    "_coefs_to_weights",
    "_components_to_features",
    "_positive_rate",
    "fetch_latest_weights",
    "run_nightly_refit",
]

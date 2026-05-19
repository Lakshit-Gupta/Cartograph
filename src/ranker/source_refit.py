"""Phase 2.4 — weekly source response-rate refit.

Pulls every ``applications`` row from the last 90 days, joins the
``opportunity_transitions`` ledger to label each row ``responded=1`` if
any *engagement* transition fired within 30 days of ``sent_at``, then
fits an L2-regularised logistic regression over one-hot-encoded
``(source_id, opp.category)`` plus posted-age and log-comp numerics.

The regression's intercept is removed and the per-source coefficient is
mapped via min-max scaling onto the multiplier range ``[0.5, 2.0]``,
which gets UPSERTed into ``sources.ranking_weight``. The existing
``src.ranker.formula.score`` already reads that column (``source_quality``
input), so no formula change is needed — the next opp scored will pick
up the new weight automatically.

Cold-start safe: when fewer than 50 labeled applications exist, the
job emits ``status='cold_start'`` and returns without writing any
weight. The ranker keeps using its seeded ``ranking_weight=1.0`` baseline.

Idempotent: ``LogisticRegression(random_state=0)`` plus deterministic
SQL ordering means a second run on identical data writes identical
weights.

No LLM call → no cost ledger entry. Pure local sklearn.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from src.common.db import acquire
from src.common.logger import get_logger

_log = get_logger(__name__)

# --- Tunables ---------------------------------------------------------------
_WINDOW_DAYS = 90  # training window
_RESPONSE_WINDOW_DAYS = 30  # outcome label window after sent_at
_COLD_START_THRESHOLD = 50  # min labeled apps before we attempt a fit
_WEIGHT_FLOOR = 0.5
_WEIGHT_CEILING = 2.0
_RANDOM_STATE = 0
_LR_MAX_ITER = 1000  # sklearn LogisticRegression iteration cap
_MAX_RECORDS = 1000  # safety cap on rows pulled per refit run

# Transitions that count as "the recruiter reacted" — we optimise for
# engagement, not acceptance. A bounce-rejection is still a positive
# signal that the source delivered to a real human.
_ENGAGEMENT_STATES = ("interview", "offer", "rejected")


@dataclass(frozen=True, slots=True)
class TrainingRow:
    """One supervised example: one application + its label + features."""

    application_id: int
    source_id: int
    category: str
    posted_at_age_days: float
    comp_min: float | None
    responded: int


# ---------------------------------------------------------------------------
# 1. Data loading
# ---------------------------------------------------------------------------
def _window_clause(window_days: int) -> str:
    """Pure SQL fragment scoped to the training window.

    ``window_days`` is bound positionally on ``$1`` at fetch time, so the
    fragment references the placeholder rather than the literal — keeps
    statement caching on asyncpg happy. Splitting this out trims the
    branch count in the calling fetch helper.
    """
    _ = int(window_days)  # validate type, value bound on $1
    return "WHERE a.sent_at >= NOW() - ($1::int || ' days')::interval\nORDER BY a.id"


def _applications_query(max_records: int = _MAX_RECORDS) -> str:
    """Compose the full SELECT used by ``_fetch_applications_rows``.

    The label column is computed inline via an ``EXISTS`` subquery onto
    ``opportunity_transitions``; this keeps the result set single-row-per
    -application even when an opp has multiple state transitions.
    """
    return f"""
        SELECT a.id                                       AS application_id,
               o.source_id                                AS source_id,
               o.category                                 AS category,
               COALESCE(EXTRACT(EPOCH FROM (a.sent_at - o.posted_at)) / 86400.0, 0.0)
                                                          AS posted_at_age_days,
               o.comp_min                                 AS comp_min,
               CASE
                 WHEN EXISTS (
                   SELECT 1 FROM opportunity_transitions t
                    WHERE t.opportunity_id = a.opportunity_id
                      AND t.to_state::text = ANY($2::text[])
                      AND t.occurred_at >= a.sent_at
                      AND t.occurred_at <= a.sent_at + INTERVAL '{_RESPONSE_WINDOW_DAYS} days'
                 ) THEN 1
                 WHEN a.response_at IS NOT NULL
                      AND a.response_at >= a.sent_at
                      AND a.response_at <= a.sent_at + INTERVAL '{_RESPONSE_WINDOW_DAYS} days'
                 THEN 1
                 ELSE 0
               END                                        AS responded
          FROM applications a
          JOIN opportunities o ON o.id = a.opportunity_id
         {_window_clause(_WINDOW_DAYS)}
         LIMIT {int(max_records)}
    """


async def _fetch_applications_rows(
    window_days: int,
    max_records: int = _MAX_RECORDS,
) -> list[dict[str, Any]]:
    """Single async DB roundtrip returning raw application+label rows.

    The SQL is composed via ``_applications_query`` so the label window,
    record cap, and ordering live in one place. The caller is responsible
    for translating row dicts → ``TrainingRow``.
    """
    async with acquire() as conn:
        rows = await conn.fetch(
            _applications_query(max_records),
            window_days,
            list(_ENGAGEMENT_STATES),
        )
    return [dict(r) for r in rows]


def _build_training_rows(rows: list[dict[str, Any]]) -> list[TrainingRow]:
    """Flatten raw DB row dicts into the typed ``TrainingRow`` shape.

    Pure transformation — no DB or sklearn dependency. Keeps the load
    helper short by isolating the per-column coercion.
    """
    return [
        TrainingRow(
            application_id=int(r["application_id"]),
            source_id=int(r["source_id"]),
            category=str(r["category"] or "unknown"),
            posted_at_age_days=float(r["posted_at_age_days"] or 0.0),
            comp_min=(float(r["comp_min"]) if r["comp_min"] is not None else None),
            responded=int(r["responded"]),
        )
        for r in rows
    ]


async def _load_training_data(window_days: int = _WINDOW_DAYS) -> list[TrainingRow]:
    """Pull labeled applications from the last ``window_days``.

    Composes ``_fetch_applications_rows`` (one async fetch) with
    ``_build_training_rows`` (pure row → dataclass). The supervised
    label is computed inline in SQL — see ``_applications_query`` for
    the engagement-window guard.

    Ordering by ``application_id`` gives deterministic feature matrix
    construction → deterministic regression → idempotent weights.
    """
    rows = await _fetch_applications_rows(window_days)
    return _build_training_rows(rows)


# ---------------------------------------------------------------------------
# 2. Label helper (pure, mockable, surfaced for unit tests)
# ---------------------------------------------------------------------------
def _label_response(
    application_id: int,
    sent_at_iso: str,
    transitions: list[dict[str, Any]],
    response_at_iso: str | None = None,
    window_days: int = _RESPONSE_WINDOW_DAYS,
) -> int:
    """Pure label function used by the test harness.

    A row is positive iff ANY engagement transition (or a non-null
    response_at) lands in the window
    ``[sent_at, sent_at + window_days]``. Transitions earlier than
    ``sent_at`` are ignored — they predate the apply and can't be
    attributed to it.

    Args carry ISO strings to keep the helper free of DB types.
    """
    from datetime import datetime, timedelta

    sent_at = datetime.fromisoformat(sent_at_iso)
    cutoff = sent_at + timedelta(days=window_days)

    for t in transitions:
        if t.get("application_id") not in (None, application_id):
            continue
        if t.get("to_state") not in _ENGAGEMENT_STATES:
            continue
        occurred_at = datetime.fromisoformat(str(t["occurred_at"]))
        if sent_at <= occurred_at <= cutoff:
            return 1

    if response_at_iso:
        response_at = datetime.fromisoformat(response_at_iso)
        if sent_at <= response_at <= cutoff:
            return 1

    return 0


# ---------------------------------------------------------------------------
# 3. Feature engineering
# ---------------------------------------------------------------------------
def _build_features(
    rows: list[TrainingRow],
) -> tuple[np.ndarray, np.ndarray, list[int]]:
    """One-hot encode (source_id, category) + numeric (age, log_comp).

    Returns ``(X, y, source_index)`` where ``source_index[i]`` is the
    ``source_id`` corresponding to feature column ``i`` in the source
    one-hot block. The downstream coefficient mapper uses this to
    walk back from coef[i] -> source_id.

    Feature layout (column order matters for idempotence):
      [ source_id one-hot ... | category one-hot ... | age | log_comp ]
    """
    if not rows:
        return np.zeros((0, 0)), np.zeros((0,), dtype=int), []

    # Sorted lists -> deterministic column ordering.
    source_ids = sorted({r.source_id for r in rows})
    categories = sorted({r.category for r in rows})
    src_idx = {sid: i for i, sid in enumerate(source_ids)}
    cat_idx = {c: i for i, c in enumerate(categories)}

    n = len(rows)
    n_src = len(source_ids)
    n_cat = len(categories)
    width = n_src + n_cat + 2  # age + log_comp

    X = np.zeros((n, width), dtype=float)
    y = np.zeros((n,), dtype=int)

    for i, r in enumerate(rows):
        X[i, src_idx[r.source_id]] = 1.0
        X[i, n_src + cat_idx[r.category]] = 1.0
        X[i, n_src + n_cat] = float(r.posted_at_age_days)
        # log1p so missing/zero comp degrades gracefully to 0.
        X[i, n_src + n_cat + 1] = math.log1p(float(r.comp_min or 0.0))
        y[i] = int(r.responded)

    return X, y, source_ids


# ---------------------------------------------------------------------------
# 4. Fit + map to weights
# ---------------------------------------------------------------------------
def _train_logistic(X: np.ndarray, y: np.ndarray, random_state: int = _RANDOM_STATE) -> Any:
    """Pure sklearn fit — moderate L2, capped iterations, fixed seed.

    Returns the fitted ``LogisticRegression``. sklearn imports are deferred
    so module import stays cheap on Pi cold start.
    """
    from sklearn.linear_model import LogisticRegression

    model = LogisticRegression(
        C=1.0,
        solver="lbfgs",
        max_iter=_LR_MAX_ITER,
        random_state=random_state,
    )
    model.fit(X, y)
    return model


def _extract_source_coefs(model: Any, source_index: list[int]) -> dict[int, float]:
    """Pull the source-one-hot block out of the model's coefficient vector.

    The one-hot block is the leading ``len(source_index)`` columns of
    ``X`` by construction (see ``_build_features``), so we slice the
    coefficient row at that boundary.
    """
    raw_coefs = model.coef_[0][: len(source_index)]
    return {sid: float(raw_coefs[i]) for i, sid in enumerate(source_index)}


def _to_weights(coefs: dict[int, float]) -> dict[int, float]:
    """Min-max scale coefs into ``[_WEIGHT_FLOOR, _WEIGHT_CEILING]``.

    Degenerate case — every coefficient identical — collapses to a
    neutral 1.0 across the board rather than to the floor.
    """
    if not coefs:
        return {}
    arr = np.array(list(coefs.values()), dtype=float)
    lo, hi = float(arr.min()), float(arr.max())
    if math.isclose(lo, hi):
        return {sid: 1.0 for sid in coefs}
    span = _WEIGHT_CEILING - _WEIGHT_FLOOR
    weights: dict[int, float] = {}
    for sid, c in coefs.items():
        normalised = (c - lo) / (hi - lo)  # 0..1
        w = _WEIGHT_FLOOR + normalised * span
        weights[sid] = max(_WEIGHT_FLOOR, min(_WEIGHT_CEILING, w))
    return weights


def _fit(
    X: np.ndarray,
    y: np.ndarray,
    source_index: list[int],
) -> tuple[dict[int, float], dict[int, float], float | None]:
    """Train logistic regression, extract per-source coefs, map to weights.

    Returns ``(weights, coefs, auc)``:
      - ``weights[source_id]`` is the multiplier in [0.5, 2.0] to UPSERT.
      - ``coefs[source_id]`` is the raw coefficient (audited).
      - ``auc`` is the training-set AUC (None if y is single-class).

    Note: training-set AUC will optimistically overstate generalisation;
    we log it as a sanity gauge, not a model-quality metric. The audit
    row is enough to spot regressions.
    """
    if X.shape[0] == 0 or X.shape[1] == 0 or not source_index:
        return {}, {}, None

    classes = np.unique(y)
    if classes.size < 2:
        # No variance in label — we can't fit a meaningful model. Return
        # neutral weights (1.0) so the downstream UPDATE is a no-op.
        weights = {sid: 1.0 for sid in source_index}
        coefs = {sid: 0.0 for sid in source_index}
        return weights, coefs, None

    model = _train_logistic(X, y)
    coefs = _extract_source_coefs(model, source_index)
    weights = _to_weights(coefs)

    try:
        from sklearn.metrics import roc_auc_score

        proba = model.predict_proba(X)[:, 1]
        auc: float | None = float(roc_auc_score(y, proba))
    except Exception:
        auc = None

    return weights, coefs, auc


# ---------------------------------------------------------------------------
# 5. Persistence — write to sources + the audit row
# ---------------------------------------------------------------------------
async def _write_weights(weights: dict[int, float]) -> int:
    """UPSERT ``sources.ranking_weight`` via a single VALUES CTE.

    Even at the ``_MAX_RECORDS`` ceiling we keep one statement, one
    transaction, no per-row roundtrip. Sources not present in the fit
    keep their existing weight (no overwrite to 1.0 — that'd nuke seeded
    values).
    """
    if not weights:
        return 0
    pairs = [(int(sid), float(w)) for sid, w in weights.items()]
    async with acquire() as conn, conn.transaction():
        await conn.execute(
            """
            UPDATE sources AS s
               SET ranking_weight = v.w
              FROM (SELECT * FROM UNNEST($1::bigint[], $2::real[]) AS t(id, w)) AS v
             WHERE s.id = v.id
            """,
            [p[0] for p in pairs],
            [p[1] for p in pairs],
        )
    return len(pairs)


async def _log_run(
    rows_used: int,
    positive_rate: float,
    auc: float | None,
    coefs: dict[int, float],
    weights: dict[int, float],
    writes: int,
    status: str,
    error: str | None = None,
) -> None:
    """Append one audit row to ``source_refit_log``."""
    summary: dict[str, dict[str, float]] = {}
    for sid in sorted(set(coefs) | set(weights)):
        summary[str(sid)] = {
            "coef": round(float(coefs.get(sid, 0.0)), 6),
            "weight": round(float(weights.get(sid, 1.0)), 6),
        }
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO source_refit_log
                (rows_used, positive_rate, auc, coefficient_summary,
                 weight_writes, status, error_message)
            VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7)
            """,
            int(rows_used),
            float(positive_rate),
            (float(auc) if auc is not None else None),
            json.dumps(summary),
            int(writes),
            status,
            error,
        )


# ---------------------------------------------------------------------------
# 6. Entrypoint — scheduler calls this
# ---------------------------------------------------------------------------
def _compute_metrics(
    rows: list[TrainingRow],
) -> tuple[dict[int, float], dict[int, float], float | None]:
    """Build features + fit + map onto weights.

    Wraps ``_build_features`` and ``_fit`` so callers don't need to thread
    the intermediate feature matrix through their branches. Returns the
    same ``(weights, coefs, auc)`` triple as ``_fit``.
    """
    X, y, source_index = _build_features(rows)
    return _fit(X, y, source_index)


def _positive_rate(rows: list[TrainingRow]) -> float:
    """Mean of the ``responded`` label across ``rows``; 0.0 for empty input."""
    if not rows:
        return 0.0
    return float(sum(r.responded for r in rows)) / len(rows)


async def _emit_cold_start(rows_used: int, positive_rate: float) -> dict[str, Any]:
    """Log + audit + return the cold-start summary.

    Single-branch wrapper so the orchestrator stays linear when row
    count falls below ``_COLD_START_THRESHOLD``.
    """
    _log.info(
        "source_refit_cold_start",
        rows_used=rows_used,
        threshold=_COLD_START_THRESHOLD,
        positive_rate=round(positive_rate, 4),
    )
    await _log_run(rows_used, positive_rate, None, {}, {}, 0, "cold_start")
    return {
        "status": "cold_start",
        "rows_used": rows_used,
        "positive_rate": positive_rate,
        "weight_writes": 0,
    }


async def _fit_and_write(
    rows: list[TrainingRow],
    positive_rate: float,
) -> dict[str, Any]:
    """Hot path — feature build → fit → persist weights → audit.

    Catches any fit/persistence error inline so the caller never has to
    branch on exception state. On success returns the ``status='ok'``
    summary that ``run_weekly_refit`` propagates to the scheduler.
    """
    rows_used = len(rows)
    try:
        weights, coefs, auc = _compute_metrics(rows)
        writes = await _write_weights(weights)
    except Exception as e:
        _log.exception("source_refit_fit_failed", err=str(e))
        await _log_run(rows_used, positive_rate, None, {}, {}, 0, "failed", error=str(e))
        return {"status": "failed", "rows_used": rows_used, "error": str(e)}

    await _log_run(rows_used, positive_rate, auc, coefs, weights, writes, "ok")
    _log.info(
        "source_refit_done",
        rows_used=rows_used,
        positive_rate=round(positive_rate, 4),
        auc=(round(auc, 4) if auc is not None else None),
        weight_writes=writes,
    )
    return {
        "status": "ok",
        "rows_used": rows_used,
        "positive_rate": positive_rate,
        "auc": auc,
        "weight_writes": writes,
        "weights": {int(k): round(float(v), 6) for k, v in weights.items()},
    }


async def _run_or_cold_start(rows: list[TrainingRow]) -> dict[str, Any]:
    """Single branch point: cold-start audit or full fit-and-write.

    Delegates to ``_emit_cold_start`` when row count is below the
    threshold, else hands off to ``_fit_and_write``. The branch table
    stays trivial so this orchestrator's complexity is bounded.
    """
    rows_used = len(rows)
    positive_rate = _positive_rate(rows)
    if rows_used < _COLD_START_THRESHOLD:
        return await _emit_cold_start(rows_used, positive_rate)
    return await _fit_and_write(rows, positive_rate)


async def run_weekly_refit(window_days: int = _WINDOW_DAYS) -> dict[str, Any]:
    """Top-level — load → label → fit → write → audit. Returns summary.

    Failure modes are caught and logged; the scheduler keeps running.
    The summary dict is what the cron logs as its tick result.
    """
    try:
        rows = await _load_training_data(window_days)
    except Exception as e:
        _log.exception("source_refit_load_failed", err=str(e))
        try:
            await _log_run(0, 0.0, None, {}, {}, 0, "failed", error=str(e))
        except Exception:
            pass
        return {"status": "failed", "rows_used": 0, "error": str(e)}

    return await _run_or_cold_start(rows)


__all__ = [
    "TrainingRow",
    "_build_features",
    "_fit",
    "_label_response",
    "_load_training_data",
    "_log_run",
    "_write_weights",
    "run_weekly_refit",
]

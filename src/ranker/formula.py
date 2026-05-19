"""Formula ranker: kw_match + emb_sim + comp + freshness + source_quality + resp_rate.

Phase 5.3 — when a successful row exists in `ranker_weights_fit` for the
current tenant, the six component weights are taken from there in
preference to `config/profile/prefs.yaml`. The fitted row is fetched
opportunistically via `load_weights_async`; the sync `load_weights()` path
keeps reading YAML so legacy callers (unit tests, CLI utilities) don't
need to thread an event loop.

`recency_half_life_hours` is NOT refitted — there's no labelled signal in
the data we currently capture to back that out. It stays YAML-driven.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from src.common.logger import get_logger
from src.common.secrets import get_settings
from src.common.types import Opportunity

_log = get_logger(__name__)

# Phase 5.3 — short refresh cache so the hot-path scorer doesn't query DB
# every score(). 5min strikes the balance: a nightly refit lands well
# inside the next cache miss, and a stale read at worst loses one day of
# fitted weights before the next refresh.
_FIT_CACHE_TTL_SECONDS = 300


@dataclass(slots=True)
class RankerWeights:
    kw_match: float = 0.25
    embedding_sim: float = 0.30
    comp_score: float = 0.15
    freshness: float = 0.10
    source_quality: float = 0.10
    response_rate: float = 0.10
    recency_half_life_hours: float = 36.0


def _load_yaml_weights() -> RankerWeights:
    settings = get_settings()
    p = Path(settings.config_root) / "profile" / "prefs.yaml"
    if not p.exists():
        return RankerWeights()
    data = yaml.safe_load(p.read_text()) or {}
    w = (data.get("ranker") or {}).get("weights") or {}
    return RankerWeights(
        kw_match=float(w.get("kw_match", 0.25)),
        embedding_sim=float(w.get("embedding_sim", 0.30)),
        comp_score=float(w.get("comp_score", 0.15)),
        freshness=float(w.get("freshness", 0.10)),
        source_quality=float(w.get("source_quality", 0.10)),
        response_rate=float(w.get("response_rate", 0.10)),
        recency_half_life_hours=float((data.get("ranker") or {}).get("recency_half_life_hours", 36.0)),
    )


def load_weights() -> RankerWeights:
    """Sync API — YAML only. Used by tests and any non-async caller.

    Production scorers should prefer `load_weights_async`; that consults
    the fitted `ranker_weights_fit` row first.
    """
    return _load_yaml_weights()


# Per-tenant cache: {user_id: (weights, fetched_at_monotonic)}. Cleared on
# refit by `invalidate_fit_cache()`.
_fit_cache: dict[int, tuple[RankerWeights, float]] = {}


def invalidate_fit_cache(user_id: int | None = None) -> None:
    """Clear cached fitted weights — called by the nightly cron after a
    successful refit so the next score() sees the new row immediately
    instead of waiting up to `_FIT_CACHE_TTL_SECONDS`.
    """
    if user_id is None:
        _fit_cache.clear()
    else:
        _fit_cache.pop(int(user_id), None)


async def load_weights_async(user_id: int | None = None) -> RankerWeights:
    """Async API — fitted row first, YAML fallback, ttl-cached.

    Pulls the latest `status='ok'` row from `ranker_weights_fit` for the
    tenant, merges those six component weights onto the YAML defaults
    (so `recency_half_life_hours` and any future YAML-only knob stay
    intact), and caches the result for `_FIT_CACHE_TTL_SECONDS`.

    Any DB error falls through to the YAML defaults — the scorer never
    crashes because of a missing or malformed fit row.
    """
    yaml_w = _load_yaml_weights()
    if user_id is None:
        from src.common.db import current_tenant

        user_id = current_tenant()
    uid = int(user_id)

    cached = _fit_cache.get(uid)
    if cached and (time.monotonic() - cached[1]) < _FIT_CACHE_TTL_SECONDS:
        return cached[0]

    try:
        from src.ranker.global_refit import fetch_latest_weights

        fitted = await fetch_latest_weights(uid)
    except Exception as e:
        _log.warning("ranker_fit_fetch_failed", user_id=uid, err=str(e))
        fitted = None

    if fitted is None:
        # Cache the YAML fallback too — saves a DB roundtrip per score()
        # when the fit table is empty (e.g. fresh deployment).
        _fit_cache[uid] = (yaml_w, time.monotonic())
        return yaml_w

    merged = RankerWeights(
        kw_match=float(fitted["kw_match"]),
        embedding_sim=float(fitted["embedding_sim"]),
        comp_score=float(fitted["comp_score"]),
        freshness=float(fitted["freshness"]),
        source_quality=float(fitted["source_quality"]),
        response_rate=float(fitted["response_rate"]),
        recency_half_life_hours=yaml_w.recency_half_life_hours,
    )
    _fit_cache[uid] = (merged, time.monotonic())
    return merged


def kw_score(opp: Opportunity, profile_keywords: set[str]) -> float:
    if not profile_keywords:
        return 0.0
    text = " ".join(filter(None, [opp.title, opp.description or "", opp.company or ""])).lower()
    hits = sum(1 for kw in profile_keywords if kw.lower() in text)
    return min(1.0, hits / max(8, len(profile_keywords) / 4))


def comp_score_of(opp: Opportunity, floors: dict[str, float]) -> float:
    """0..1 score relative to category floor. Missing comp = 0.5 (unknown is neutral)."""
    if opp.comp_min is None and opp.comp_max is None:
        return 0.5
    val = float(opp.comp_max or opp.comp_min or 0)
    floor = floors.get(opp.category.value, 0.0)
    if floor <= 0:
        return 0.7
    ratio = val / floor
    return max(0.0, min(1.0, math.log1p(ratio) / math.log(3)))  # ~1.0 at 2x floor


def freshness(opp: Opportunity, half_life_hours: float) -> float:
    posted = opp.posted_at
    if posted is None:
        return 0.5
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=UTC)
    age_h = max(0.0, (datetime.now(UTC) - posted).total_seconds() / 3600.0)
    return float(0.5 ** (age_h / half_life_hours))


@dataclass(slots=True)
class ScoreOutput:
    score: float
    components: dict[str, float]


def score(
    opp: Opportunity,
    *,
    profile_keywords: set[str],
    embedding_sim: float,
    source_quality: float,
    response_rate: float,
    comp_floors: dict[str, float],
    weights: RankerWeights,
) -> ScoreOutput:
    kw = kw_score(opp, profile_keywords)
    fresh = freshness(opp, weights.recency_half_life_hours)
    comp = comp_score_of(opp, comp_floors)

    components = {
        "kw_match": kw,
        "embedding_sim": embedding_sim,
        "comp_score": comp,
        "freshness": fresh,
        "source_quality": source_quality,
        "response_rate": response_rate,
    }
    final = (
        weights.kw_match * kw
        + weights.embedding_sim * embedding_sim
        + weights.comp_score * comp
        + weights.freshness * fresh
        + weights.source_quality * source_quality
        + weights.response_rate * response_rate
    )
    return ScoreOutput(score=max(0.0, min(1.0, final)), components=components)

"""Formula ranker: kw_match + emb_sim + comp + freshness + source_quality + resp_rate."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from src.common.secrets import get_settings
from src.common.types import Opportunity


@dataclass(slots=True)
class RankerWeights:
    kw_match: float = 0.25
    embedding_sim: float = 0.30
    comp_score: float = 0.15
    freshness: float = 0.10
    source_quality: float = 0.10
    response_rate: float = 0.10
    recency_half_life_hours: float = 36.0


def load_weights() -> RankerWeights:
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

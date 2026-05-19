"""Pure unit tests for `src.ranker.global_refit`.

Exercises the feature-engineering + coefficient-mapping helpers without
hitting a DB or sklearn fit. The async `run_nightly_refit` path is covered
by integration tests (which require Postgres) — out of scope here.
"""

from __future__ import annotations

import json

import numpy as np

from src.ranker.global_refit import (
    _FEATURE_KEYS,
    FitRow,
    _build_matrix,
    _coefs_to_weights,
    _components_to_features,
    _positive_rate,
)


def _row(features: tuple[float, ...], responded: int) -> FitRow:
    return FitRow(application_id=1, features=features, responded=responded)


def test_components_to_features_happy_path() -> None:
    comps = {
        "kw_match": 0.5,
        "embedding_sim": 0.6,
        "comp_score": 0.7,
        "freshness": 0.8,
        "source_quality": 0.9,
        "response_rate": 0.1,
    }
    feats = _components_to_features(comps)
    assert feats == (0.5, 0.6, 0.7, 0.8, 0.9, 0.1)
    assert len(_FEATURE_KEYS) == 6


def test_components_to_features_accepts_json_string() -> None:
    comps_str = json.dumps({k: 0.4 for k in _FEATURE_KEYS})
    feats = _components_to_features(comps_str)
    assert feats is not None
    assert all(v == 0.4 for v in feats)


def test_components_to_features_rejects_two_or_more_missing() -> None:
    comps = {"kw_match": 0.5}  # 5 missing
    assert _components_to_features(comps) is None


def test_components_to_features_tolerates_one_missing() -> None:
    """Single missing key is acceptable — defaults to 0.0."""
    comps = {k: 0.3 for k in _FEATURE_KEYS}
    del comps["response_rate"]
    feats = _components_to_features(comps)
    assert feats is not None
    # response_rate is the 6th feature; defaults to 0.0.
    assert feats[-1] == 0.0


def test_components_to_features_rejects_non_dict() -> None:
    assert _components_to_features(42) is None
    assert _components_to_features("not json") is None
    assert _components_to_features(None) is None


def test_build_matrix_shapes() -> None:
    rows = [
        _row((0.1, 0.2, 0.3, 0.4, 0.5, 0.6), responded=1),
        _row((0.2, 0.3, 0.4, 0.5, 0.6, 0.7), responded=0),
    ]
    X, y = _build_matrix(rows)
    assert X.shape == (2, 6)
    assert y.tolist() == [1, 0]
    assert X[0, 0] == 0.1
    assert X[1, -1] == 0.7


def test_build_matrix_empty() -> None:
    X, y = _build_matrix([])
    assert X.shape == (0, 6)
    assert y.shape == (0,)


def test_coefs_to_weights_clamps_negatives_and_l1_normalises() -> None:
    # Mix of positive and negative — negatives clamp to 0, rest L1-normalise to 1.0.
    coefs = np.array([0.5, 1.0, -0.5, 0.0, 0.5, 0.0])
    weights = _coefs_to_weights(coefs)
    assert set(weights) == set(_FEATURE_KEYS)
    assert abs(sum(weights.values()) - 1.0) < 1e-9
    # No negative leaks through.
    assert all(v >= 0.0 for v in weights.values())
    # The two zero / clamped coefs map to weight 0.
    assert weights["comp_score"] == 0.0  # negative clamped
    assert weights["freshness"] == 0.0  # raw 0
    assert weights["response_rate"] == 0.0  # raw 0


def test_coefs_to_weights_degenerate_all_zero_uniform() -> None:
    weights = _coefs_to_weights(np.zeros(6))
    uniform = 1.0 / 6
    assert all(abs(v - uniform) < 1e-9 for v in weights.values())


def test_coefs_to_weights_all_negative_uniform_fallback() -> None:
    weights = _coefs_to_weights(np.array([-1.0, -2.0, -0.5, -0.1, -0.7, -0.3]))
    # All negative → clamped to 0 → degenerate → uniform.
    uniform = 1.0 / 6
    assert all(abs(v - uniform) < 1e-9 for v in weights.values())


def test_positive_rate_empty_zero() -> None:
    assert _positive_rate([]) == 0.0


def test_positive_rate_mixed() -> None:
    rows = [
        _row((0.0,) * 6, responded=1),
        _row((0.0,) * 6, responded=0),
        _row((0.0,) * 6, responded=1),
        _row((0.0,) * 6, responded=0),
    ]
    assert _positive_rate(rows) == 0.5

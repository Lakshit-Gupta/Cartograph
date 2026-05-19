"""Cache + fallback contract tests for `src.ranker.formula.load_weights_async`.

Verifies:
  - `load_weights_async` falls back to YAML when the DB lookup returns None.
  - Successive calls hit the cache (DB called once).
  - `invalidate_fit_cache` forces a refetch on the next call.
  - Merged result preserves `recency_half_life_hours` from YAML.
"""

from __future__ import annotations

import asyncio

import pytest

from src.ranker import formula


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    formula.invalidate_fit_cache()


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_falls_back_to_yaml_when_fit_table_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _none(_uid: int) -> None:
        return None

    monkeypatch.setattr(
        "src.ranker.global_refit.fetch_latest_weights",
        _none,
    )

    w = _run(formula.load_weights_async(user_id=1))
    assert isinstance(w, formula.RankerWeights)
    # YAML defaults match the dataclass defaults below; mirroring the
    # well-known seed values keeps this test independent of test-tree
    # config files.
    assert w.kw_match == 0.25
    assert w.recency_half_life_hours == 36.0


def test_caches_repeated_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    async def _fake(_uid: int) -> dict[str, float]:
        calls["n"] += 1
        return {
            "kw_match": 0.4,
            "embedding_sim": 0.3,
            "comp_score": 0.1,
            "freshness": 0.1,
            "source_quality": 0.05,
            "response_rate": 0.05,
        }

    monkeypatch.setattr(
        "src.ranker.global_refit.fetch_latest_weights",
        _fake,
    )

    w1 = _run(formula.load_weights_async(user_id=42))
    w2 = _run(formula.load_weights_async(user_id=42))
    assert calls["n"] == 1
    assert w1 is w2 or (w1.kw_match == w2.kw_match == 0.4)


def test_invalidate_forces_refetch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    async def _fake(_uid: int) -> dict[str, float]:
        calls["n"] += 1
        return {
            k: 1.0 / 6
            for k in (
                "kw_match",
                "embedding_sim",
                "comp_score",
                "freshness",
                "source_quality",
                "response_rate",
            )
        }

    monkeypatch.setattr(
        "src.ranker.global_refit.fetch_latest_weights",
        _fake,
    )

    _run(formula.load_weights_async(user_id=42))
    formula.invalidate_fit_cache(42)
    _run(formula.load_weights_async(user_id=42))
    assert calls["n"] == 2


def test_merges_recency_half_life_from_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fitted row only carries the six component weights; the recency
    half-life must come through from YAML so the freshness curve doesn't
    silently flatten to whatever default the dataclass holds.
    """

    async def _fake(_uid: int) -> dict[str, float]:
        return {
            "kw_match": 0.3,
            "embedding_sim": 0.3,
            "comp_score": 0.1,
            "freshness": 0.1,
            "source_quality": 0.1,
            "response_rate": 0.1,
        }

    monkeypatch.setattr(
        "src.ranker.global_refit.fetch_latest_weights",
        _fake,
    )

    w = _run(formula.load_weights_async(user_id=1))
    yaml_w = formula._load_yaml_weights()
    assert w.recency_half_life_hours == yaml_w.recency_half_life_hours
    assert w.kw_match == 0.3  # fitted value wins for the six features

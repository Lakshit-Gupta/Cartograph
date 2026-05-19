"""Tests for the Phase 2.2 variant picker + variant_refit.

These are pure-function tests over the bandit math (UCB1 cold start +
exploitation under sample) and the smoothing weights. The DB-backed
``pick_variant_async`` path is exercised indirectly via the
``test_falls_back_to_uniform_on_single_variant_config`` test (which
stubs ``_load_active_variants``) — full DB round-trips live in the
``tests/application/test_compile.py`` integration lane.
"""

from __future__ import annotations

import random

import pytest

from src.application.resume_latex.variant_picker import (
    VariantStats,
    _ucb1_pick,
    pick_from_stats,
)


# ---------------------------------------------------------------------------
# Cold start — every variant gets tried before exploitation kicks in.
# ---------------------------------------------------------------------------
def test_uniform_cold_start_with_zero_samples():
    """All variants at (sent=0, responded=0) — picker must return one of them.

    Cold start in UCB1 routes through the ``untried`` branch (infinite bonus).
    The exact pick is uniform random; we assert *membership* in the active set,
    not a fixed label, so the test is deterministic without seeding RNG.
    """
    random.seed(0)
    stats = [
        VariantStats(variant_id=1, label="backend", category="fulltime", sent=0, responded=0),
        VariantStats(variant_id=2, label="fullstack", category="fulltime", sent=0, responded=0),
        VariantStats(variant_id=3, label="ml", category="fulltime", sent=0, responded=0),
    ]
    picked = pick_from_stats(stats)
    assert picked in {"backend", "fullstack", "ml"}


def test_cold_start_eventually_covers_every_variant():
    """Sample the cold-start branch many times — every variant must appear."""
    random.seed(42)
    stats = [
        VariantStats(variant_id=1, label="a", category="fulltime", sent=0, responded=0),
        VariantStats(variant_id=2, label="b", category="fulltime", sent=0, responded=0),
        VariantStats(variant_id=3, label="c", category="fulltime", sent=0, responded=0),
    ]
    seen = {pick_from_stats(stats) for _ in range(50)}
    assert seen == {"a", "b", "c"}


# ---------------------------------------------------------------------------
# Exploitation — once every variant has samples, the high-rate one wins.
# ---------------------------------------------------------------------------
def test_ucb1_favours_high_response_rate_variant():
    """Variant A: 10 sends, 5 responses (50% rate); B: 10 sends, 1 response.

    With both well-sampled, UCB1's exploration bonus barely moves the
    needle. The mean reward (0.5 vs 0.1) dominates, so A wins on every
    pick from this configuration.
    """
    stats = [
        VariantStats(variant_id=1, label="winner", category="fulltime", sent=10, responded=5),
        VariantStats(variant_id=2, label="loser", category="fulltime", sent=10, responded=1),
    ]
    picks = {pick_from_stats(stats) for _ in range(20)}
    assert picks == {"winner"}


def test_ucb1_pick_returns_max_score():
    """Spot check the ``_ucb1_pick`` direct helper returns the right arm.

    Variant ``a`` has a much higher mean reward AND more samples — both
    components of UCB1's formula favour it.
    """
    stats = [
        VariantStats(variant_id=1, label="a", category="x", sent=20, responded=8),
        VariantStats(variant_id=2, label="b", category="x", sent=20, responded=1),
    ]
    chosen = _ucb1_pick(stats)
    assert chosen.label == "a"


# ---------------------------------------------------------------------------
# Backward-compat — single-variant / empty-stats fallthrough paths.
# ---------------------------------------------------------------------------
def test_falls_back_to_default_on_empty_stats():
    """No active variants → return the seeded default (``backend``)."""
    assert pick_from_stats([], cold_start_default="backend") == "backend"


def test_falls_back_to_uniform_on_single_variant_config(monkeypatch):
    """One active variant in the DB → picker must return that variant.

    Verifies the Phase 1 single-variant deployment keeps working: even
    if the bandit is enabled, with one option there's only one answer.
    """
    import asyncio

    from src.application.resume_latex import variant_picker as vp

    async def _fake_actives():
        return {"backend": 1}

    monkeypatch.setattr(vp, "_load_active_variants", _fake_actives)

    label = asyncio.run(vp.pick_variant_async({"category": "fulltime"}))
    assert label == "backend"


# ---------------------------------------------------------------------------
# Record outcome / weight refit math.
# ---------------------------------------------------------------------------
def test_record_outcome_increments_counter():
    """The picker's stats are computed live from `applications` — no
    separate counter table. This test asserts the rollup is the right
    shape: each VariantStats record carries sent + responded ints and
    the derived ``mean_reward`` is responded/sent.
    """
    s = VariantStats(variant_id=1, label="x", category="y", sent=4, responded=1)
    assert s.mean_reward == pytest.approx(0.25)
    # Empty arm — mean is 0, not NaN/divide-by-zero.
    s0 = VariantStats(variant_id=2, label="x", category="y", sent=0, responded=0)
    assert s0.mean_reward == 0.0


def test_resolve_variant_main_skips_comments(tmp_path):
    """Phase 2.2 — comment-aware `\\input{...}` flattening.

    The variant stubs include a comment mentioning ``\\input{...}`` for
    documentation; the resolver MUST NOT replace the comment-embedded
    reference, only the actual non-comment one. Regression catch for
    the original boot smoke that compiled into a giant inlined comment.
    """
    from src.application.resume_latex.fallback import resolve_variant_main

    base = tmp_path / "base.tex"
    base.write_text("BASE_CONTENT", encoding="utf-8")

    variant_dir = tmp_path / "variants" / "x"
    variant_dir.mkdir(parents=True)
    variant = variant_dir / "main.tex"
    variant.write_text(
        "% docstring: the resolver flattens \\input{../../base.tex}.\n\\input{../../base.tex}\n",
        encoding="utf-8",
    )

    out = resolve_variant_main(variant, tmp_path)
    # Comment line stays verbatim.
    assert "% docstring: the resolver flattens \\input{../../base.tex}." in out
    # Real \input got flattened — once.
    assert out.count("BASE_CONTENT") == 1


def test_variant_refit_writes_weights():
    """Pure-function check on the weight smoother.

    ``_smooth_weights`` should:
      * Return floor-clipped (>= 0.1), ceiling-clipped (<= 5.0) values.
      * Hand every variant a weight ~= 1.0 when there are zero
        applications across all variants (cold start).
      * Hand a higher weight to the variant with a better posterior rate.
    """
    from src.ranker.variant_refit import VariantOutcome, _smooth_weights

    # Cold start — three variants, no sends. Everyone gets 1.0.
    cold = _smooth_weights(
        [
            VariantOutcome(variant_id=1, label="a", sent=0, responded=0),
            VariantOutcome(variant_id=2, label="b", sent=0, responded=0),
            VariantOutcome(variant_id=3, label="c", sent=0, responded=0),
        ]
    )
    assert cold == {1: 1.0, 2: 1.0, 3: 1.0}

    # Mixed signal — variant 1 has a much better rate (5/10) than 2 (1/10).
    hot = _smooth_weights(
        [
            VariantOutcome(variant_id=1, label="winner", sent=10, responded=5),
            VariantOutcome(variant_id=2, label="loser", sent=10, responded=1),
        ]
    )
    assert 0.1 <= hot[1] <= 5.0
    assert 0.1 <= hot[2] <= 5.0
    assert hot[1] > hot[2], f"winner should have a higher weight than loser; got {hot}"

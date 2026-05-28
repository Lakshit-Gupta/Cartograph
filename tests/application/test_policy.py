"""Hermetic tests for ``src.application.policy`` — auto-apply decision gate.

Covers every branch of `should_auto_submit()` deterministically by
monkeypatching the DB-touching helpers + the prefs loader. The function
is pure modulo three async helpers — each one is patched in turn so the
decision tree is exhaustively exercised without a live Postgres.

Branches covered (one test per decision label):

  refused_disabled       — prefs.auto_apply.enabled=false
  refused_method         — submitter_key not in prefs.methods whitelist
  refused_no_submitter   — opp has no source row OR method has no mapping
  refused_source         — sources.auto_apply_enabled=false for the slug
  refused_no_score       — no opportunity_scores row exists
  refused_score          — score < min_score
  refused_cap            — daily count >= max_per_day
  submit                 — all gates pass, dry_run=false
  submit_deferred_dryrun — all gates pass, dry_run=true

Plus a regression case for `_submitter_key` covering the `in_X` slug
shape.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from src.application import policy
from src.common.types import ApplyMethod

_OPP_ID = UUID("00000000-0000-0000-0000-0000000007aa")
_USER_ID = 1


def _patch_helpers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    prefs: dict[str, Any] | None = None,
    source: tuple[str, bool] | None = ("in_internshala", True),
    score: float | None = 0.85,
    daily_count: int = 0,
) -> None:
    """Stub the four helpers used by `should_auto_submit`."""

    def _load_prefs() -> dict[str, Any]:
        return prefs if prefs is not None else _default_prefs()

    async def _fetch_source(_opp: UUID) -> tuple[str, bool] | None:
        return source

    async def _fetch_score(_opp: UUID, _user: int) -> float | None:
        return score

    async def _fetch_daily(_user: int) -> int:
        return daily_count

    monkeypatch.setattr(policy, "_load_prefs_auto_apply", _load_prefs)
    monkeypatch.setattr(policy, "_fetch_source_for_opp", _fetch_source)
    monkeypatch.setattr(policy, "_fetch_score", _fetch_score)
    monkeypatch.setattr(policy, "_fetch_daily_count", _fetch_daily)


def _default_prefs() -> dict[str, Any]:
    return {
        "enabled": True,
        "dry_run": False,
        "min_score": 0.80,
        "max_per_day": 3,
        "methods": ["in_platform_internshala"],
    }


# --- _submitter_key regression --------------------------------------------
def test_submitter_key_email() -> None:
    assert policy._submitter_key(ApplyMethod.EMAIL, None) == "email"


def test_submitter_key_internshala_strips_in_prefix() -> None:
    assert policy._submitter_key(ApplyMethod.IN_PLATFORM, "in_internshala") == "in_platform_internshala"


def test_submitter_key_naukri_future() -> None:
    # Naukri lands in Phase 4.2; the key shape is forward-compatible.
    assert policy._submitter_key(ApplyMethod.IN_PLATFORM, "in_naukri") == "in_platform_naukri"


def test_submitter_key_in_platform_no_source_is_none() -> None:
    assert policy._submitter_key(ApplyMethod.IN_PLATFORM, None) is None


def test_submitter_key_ats_form_returns_none_phase_1() -> None:
    # Phase 1 has no ATS submitters; the policy SHOULD refuse with
    # refused_no_submitter so the apply falls through to manual.
    assert policy._submitter_key(ApplyMethod.ATS_FORM, "us_greenhouse") is None


# --- decision branches ----------------------------------------------------
@pytest.mark.smoke
async def test_refused_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_helpers(monkeypatch, prefs={"enabled": False})
    d = await policy.should_auto_submit(opportunity_id=_OPP_ID, user_id=_USER_ID, method=ApplyMethod.IN_PLATFORM)
    assert d.decision == "refused_disabled"
    assert d.submit is False


@pytest.mark.smoke
async def test_refused_method_when_not_whitelisted(monkeypatch: pytest.MonkeyPatch) -> None:
    prefs = _default_prefs()
    prefs["methods"] = []  # empty whitelist
    _patch_helpers(monkeypatch, prefs=prefs)
    d = await policy.should_auto_submit(opportunity_id=_OPP_ID, user_id=_USER_ID, method=ApplyMethod.IN_PLATFORM)
    assert d.decision == "refused_method"
    assert d.submit is False


@pytest.mark.smoke
async def test_refused_no_submitter_when_source_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_helpers(monkeypatch, source=None)
    d = await policy.should_auto_submit(opportunity_id=_OPP_ID, user_id=_USER_ID, method=ApplyMethod.IN_PLATFORM)
    assert d.decision == "refused_no_submitter"
    assert d.submit is False


@pytest.mark.smoke
async def test_refused_no_submitter_for_email_without_email_whitelist(monkeypatch: pytest.MonkeyPatch) -> None:
    prefs = _default_prefs()
    prefs["methods"] = ["in_platform_internshala"]  # email NOT whitelisted
    _patch_helpers(monkeypatch, prefs=prefs, source=("in_internshala", True))
    d = await policy.should_auto_submit(opportunity_id=_OPP_ID, user_id=_USER_ID, method=ApplyMethod.EMAIL)
    # EMAIL maps to submitter_key='email'; not in whitelist → refused_method.
    assert d.decision == "refused_method"


@pytest.mark.smoke
async def test_refused_source_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_helpers(monkeypatch, source=("in_internshala", False))
    d = await policy.should_auto_submit(opportunity_id=_OPP_ID, user_id=_USER_ID, method=ApplyMethod.IN_PLATFORM)
    assert d.decision == "refused_source"
    assert d.submit is False


@pytest.mark.smoke
async def test_refused_no_score(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_helpers(monkeypatch, score=None)
    d = await policy.should_auto_submit(opportunity_id=_OPP_ID, user_id=_USER_ID, method=ApplyMethod.IN_PLATFORM)
    assert d.decision == "refused_no_score"
    assert d.submit is False


@pytest.mark.smoke
async def test_refused_score_below_min(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_helpers(monkeypatch, score=0.5)
    d = await policy.should_auto_submit(opportunity_id=_OPP_ID, user_id=_USER_ID, method=ApplyMethod.IN_PLATFORM)
    assert d.decision == "refused_score"
    assert d.score == pytest.approx(0.5)
    assert d.submit is False


@pytest.mark.smoke
async def test_refused_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_helpers(monkeypatch, daily_count=3)  # equals cap
    d = await policy.should_auto_submit(opportunity_id=_OPP_ID, user_id=_USER_ID, method=ApplyMethod.IN_PLATFORM)
    assert d.decision == "refused_cap"
    assert d.daily_count_before == 3
    assert d.daily_cap == 3
    assert d.submit is False


@pytest.mark.smoke
async def test_submit_when_all_gates_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_helpers(monkeypatch)
    d = await policy.should_auto_submit(opportunity_id=_OPP_ID, user_id=_USER_ID, method=ApplyMethod.IN_PLATFORM)
    assert d.decision == "submit"
    assert d.submit is True
    assert d.dry_run is False
    assert d.submitter_key == "in_platform_internshala"
    assert d.source_slug == "in_internshala"


@pytest.mark.smoke
async def test_submit_deferred_dryrun(monkeypatch: pytest.MonkeyPatch) -> None:
    prefs = _default_prefs()
    prefs["dry_run"] = True
    _patch_helpers(monkeypatch, prefs=prefs)
    d = await policy.should_auto_submit(opportunity_id=_OPP_ID, user_id=_USER_ID, method=ApplyMethod.IN_PLATFORM)
    assert d.decision == "submit_deferred_dryrun"
    assert d.submit is True
    assert d.dry_run is True


@pytest.mark.smoke
async def test_missing_prefs_block_defaults_to_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # Empty prefs simulates a missing/malformed `auto_apply:` block.
    _patch_helpers(monkeypatch, prefs={})
    d = await policy.should_auto_submit(opportunity_id=_OPP_ID, user_id=_USER_ID, method=ApplyMethod.IN_PLATFORM)
    assert d.decision == "refused_disabled"


@pytest.mark.smoke
async def test_record_attempt_swallows_db_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """record_attempt() must NEVER raise into the apply hot path."""

    async def _boom(**_kwargs: Any) -> None:
        raise RuntimeError("simulated DB outage")

    monkeypatch.setattr(policy, "_record_attempt_db", _boom)
    decision = policy.AutoApplyDecision(
        submit=False,
        dry_run=False,
        decision="refused_disabled",
        reason="test",
        score=None,
        method="email",
        source_slug=None,
        submitter_key=None,
        daily_count_before=0,
        daily_cap=3,
    )
    # Must not raise.
    await policy.record_attempt(
        user_id=_USER_ID,
        opportunity_id=_OPP_ID,
        application_id=None,
        decision=decision,
    )

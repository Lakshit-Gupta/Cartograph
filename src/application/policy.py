"""Phase 4 auto-apply policy gate.

`should_auto_submit()` is the SINGLE pre-flight check every submitter
(Internshala browser, Greenhouse API, etc.) must pass before firing. It
consults four layers in this order:

  1. `prefs.auto_apply.enabled`         — master kill switch.
  2. `prefs.auto_apply.methods`         — per-method whitelist.
  3. `sources.auto_apply_enabled`       — per-source kill switch.
  4. `opportunity_scores.score`         — quality floor (`min_score`).
  5. `auto_apply_daily_count`           — daily ceiling (`max_per_day`).

If every gate passes, returns a `Submit` decision (or `SubmitDeferredDryRun`
if `dry_run=true`). Otherwise returns one of the `Refused*` variants and the
caller falls through to the existing `manual_apply_ready` notify path.

Every call writes exactly one row into `auto_apply_audit` via
`record_attempt()` so the entire decision history is reconstructible from
SQL without re-running the pipeline.

The policy is intentionally pure-ish — all DB reads are explicit. No
side effects beyond the audit row + (on actual submit) the daily-count
bump. Unit tests in `tests/application/test_policy.py` cover every
branch deterministically by mocking the helpers below.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import yaml

from src.common.db import acquire, fetch_one
from src.common.logger import get_logger
from src.common.metrics import auto_apply_decisions_total
from src.common.secrets import get_settings
from src.common.types import ApplyMethod

_log = get_logger(__name__)


# Decision labels — must stay in sync with the CHECK constraint on
# auto_apply_audit.decision (migrations/V022__auto_apply.sql).
Decision = Literal[
    "submit",
    "submit_deferred_dryrun",
    "refused_disabled",
    "refused_method",
    "refused_source",
    "refused_score",
    "refused_no_score",
    "refused_cap",
    "refused_no_submitter",
]


@dataclass(frozen=True)
class AutoApplyDecision:
    """Return value of `should_auto_submit()`. Callers branch on `submit`."""

    submit: bool
    dry_run: bool
    decision: Decision
    reason: str
    score: float | None
    method: str
    source_slug: str | None
    submitter_key: str | None  # e.g. "in_platform_internshala"; None when refused
    daily_count_before: int
    daily_cap: int


# Hard defaults — used when `auto_apply` block is absent from prefs.yaml
# OR when individual keys are missing. Conservative: a missing block means
# "off", a missing knob means the strictest interpretation.
_DEFAULT_ENABLED = False
_DEFAULT_DRY_RUN = True
_DEFAULT_MIN_SCORE = 0.80
_DEFAULT_MAX_PER_DAY = 3
_DEFAULT_METHODS: tuple[str, ...] = ()  # whitelist; empty = nothing eligible


def _load_prefs_auto_apply() -> dict[str, Any]:
    """Read `auto_apply:` block from `config/profile/prefs.yaml`.

    Returns an empty dict on any read/parse error — degrades to "disabled"
    rather than raising into the apply hot path. Errors logged at WARNING.
    """
    settings = get_settings()
    path = Path(settings.config_root) / "profile" / "prefs.yaml"
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        _log.warning("prefs_auto_apply_read_failed", err=str(e), path=str(path))
        return {}
    block = loaded.get("auto_apply") or {}
    if not isinstance(block, dict):
        _log.warning("prefs_auto_apply_not_a_dict", got_type=type(block).__name__)
        return {}
    return block


def _submitter_key(method: ApplyMethod, source_slug: str | None) -> str | None:
    """Map (apply_method, source_slug) to the registry key used by
    `src/application/submitters/__init__.py`.

    `ats_form` + `external` + `embedded_form` collapse to a single key per
    source family today (`ats_form_greenhouse`, etc.) — those are Phase 4.2+,
    not Internshala-blocking. `email` is universal; `in_platform` is the
    only method that needs source-level routing in Phase 1.
    """
    if method == ApplyMethod.EMAIL:
        return "email"
    if method == ApplyMethod.IN_PLATFORM:
        if source_slug is None:
            return None
        # Source slugs follow the migrations/V003__sources_seed.sql shape:
        #   in_internshala, in_naukri, in_cuvette, in_unstop, in_contra
        # Strip the "in_" prefix and rejoin so the registry key reads
        # naturally as `in_platform_internshala`.
        platform = source_slug.removeprefix("in_")
        return f"in_platform_{platform}"
    # ats_form / external / embedded_form land here. Phase 4.2+ implements
    # per-ATS keys (ats_form_greenhouse, etc.); for Phase 1 they all return
    # None and fall through to manual_apply_ready, which matches today's
    # behavior exactly.
    return None


async def _fetch_score(opportunity_id: UUID, user_id: int) -> float | None:
    """Read `opportunity_scores.score` for (user, opp). None if missing OR
    if the DB read raises — auto-apply should NEVER escalate DB errors
    into the apply hot path."""
    try:
        rec = await fetch_one(
            "SELECT score FROM opportunity_scores WHERE user_id = $1 AND opportunity_id = $2",
            user_id,
            opportunity_id,
        )
    except Exception as e:
        _log.warning("policy_fetch_score_failed", err=str(e), opp=str(opportunity_id))
        return None
    return float(rec["score"]) if rec is not None else None


async def _fetch_source_for_opp(opportunity_id: UUID) -> tuple[str, bool] | None:
    """Return (source_slug, auto_apply_enabled) for the opp's source row.

    Returns None on DB error so the policy degrades to ``refused_no_submitter``
    rather than raising — the apply pipeline then falls through to its
    existing manual path.
    """
    try:
        rec = await fetch_one(
            """
            SELECT s.slug, s.auto_apply_enabled
            FROM opportunities o
            JOIN sources s ON s.id = o.source_id
            WHERE o.id = $1
            """,
            opportunity_id,
        )
    except Exception as e:
        _log.warning("policy_fetch_source_failed", err=str(e), opp=str(opportunity_id))
        return None
    if rec is None:
        return None
    return str(rec["slug"]), bool(rec["auto_apply_enabled"])


async def _fetch_daily_count(user_id: int) -> int:
    """Today's auto-apply submission count. Zero on read error so the cap
    gate degrades to permissive — but `enabled=false` default still blocks
    everything in practice, so this is only relevant once the user has
    actively turned auto-apply on."""
    try:
        rec = await fetch_one(
            "SELECT submitted_count FROM auto_apply_daily_count WHERE user_id = $1 AND apply_date = CURRENT_DATE",
            user_id,
        )
    except Exception as e:
        _log.warning("policy_fetch_daily_count_failed", err=str(e), user_id=user_id)
        return 0
    return int(rec["submitted_count"]) if rec is not None else 0


async def should_auto_submit(
    *,
    opportunity_id: UUID,
    user_id: int,
    method: ApplyMethod,
) -> AutoApplyDecision:
    """Single source of truth for "is this opp auto-apply-eligible right now?"

    Pure function modulo DB reads — does NOT bump the daily count, does NOT
    write the audit row. Callers MUST call `record_attempt()` exactly once
    with the returned decision (and the resulting `application_id` if a row
    was inserted) so the audit log stays grep-equivalent to live behavior.
    """
    prefs = _load_prefs_auto_apply()
    enabled = bool(prefs.get("enabled", _DEFAULT_ENABLED))
    dry_run = bool(prefs.get("dry_run", _DEFAULT_DRY_RUN))
    min_score = float(prefs.get("min_score", _DEFAULT_MIN_SCORE))
    daily_cap = int(prefs.get("max_per_day", _DEFAULT_MAX_PER_DAY))
    methods_whitelist = tuple(prefs.get("methods") or _DEFAULT_METHODS)

    source_info = await _fetch_source_for_opp(opportunity_id)
    source_slug, source_enabled = source_info if source_info is not None else (None, False)

    submitter_key = _submitter_key(method, source_slug)

    daily_count = await _fetch_daily_count(user_id)

    def _build(decision: Decision, reason: str, *, score: float | None = None) -> AutoApplyDecision:
        return AutoApplyDecision(
            submit=decision in ("submit", "submit_deferred_dryrun"),
            dry_run=dry_run,
            decision=decision,
            reason=reason,
            score=score,
            method=method.value,
            source_slug=source_slug,
            submitter_key=submitter_key if decision in ("submit", "submit_deferred_dryrun") else None,
            daily_count_before=daily_count,
            daily_cap=daily_cap,
        )

    # 1) Master kill switch.
    if not enabled:
        return _build("refused_disabled", "prefs.auto_apply.enabled is false")

    # 2) Method whitelist. submitter_key is the canonical name; refuse if
    # neither the raw method ("email") nor the resolved key is whitelisted.
    if submitter_key is None:
        return _build("refused_no_submitter", f"no submitter for ({method.value}, {source_slug})")
    if submitter_key not in methods_whitelist:
        return _build("refused_method", f"submitter_key '{submitter_key}' not in prefs.auto_apply.methods")

    # 3) Per-source kill switch (email is the exception — sources.auto_apply_enabled
    # gating is per-source; for EMAIL the source is whatever crawler produced it
    # and the user explicitly whitelists each one. Same gate either way.)
    if source_info is None:
        return _build("refused_no_submitter", "opportunity has no source row (deleted?)")
    if not source_enabled:
        return _build("refused_source", f"sources.auto_apply_enabled=false for slug '{source_slug}'")

    # 4) Score gate.
    score = await _fetch_score(opportunity_id, user_id)
    if score is None:
        return _build("refused_no_score", "no opportunity_scores row (ranker hasn't seen this opp)")
    if score < min_score:
        return _build("refused_score", f"score {score:.3f} < min_score {min_score:.3f}", score=score)

    # 5) Daily cap.
    if daily_count >= daily_cap:
        return _build("refused_cap", f"daily count {daily_count} >= cap {daily_cap}", score=score)

    # All gates passed.
    if dry_run:
        return _build("submit_deferred_dryrun", "all gates passed; dry_run=true", score=score)
    return _build("submit", "all gates passed", score=score)


async def record_attempt(
    *,
    user_id: int,
    opportunity_id: UUID,
    application_id: int | None,
    decision: AutoApplyDecision,
) -> None:
    """Persist one `auto_apply_audit` row + bump daily count on real submits.

    `application_id` is None when the policy refused (no application row was
    inserted), populated when the caller has already upserted into
    `applications` before firing the submitter. Bump rule: count increments
    ONLY on `decision='submit'` — dry-run submissions are NOT counted toward
    the daily cap so the verification window can fire all 3 dry-runs and
    still leave headroom for a single real submit.

    DB writes are best-effort: a failure here never escalates into the
    apply hot path — auto_apply_decisions_total still increments so the
    metric stays accurate even when the audit row is lost.
    """
    try:
        await _record_attempt_db(
            user_id=user_id,
            opportunity_id=opportunity_id,
            application_id=application_id,
            decision=decision,
        )
    except Exception as e:
        _log.warning(
            "policy_record_attempt_failed",
            err=str(e),
            user_id=user_id,
            opp=str(opportunity_id),
            decision=decision.decision,
        )
    auto_apply_decisions_total.labels(decision=decision.decision, method=decision.method).inc()


async def _record_attempt_db(
    *,
    user_id: int,
    opportunity_id: UUID,
    application_id: int | None,
    decision: AutoApplyDecision,
) -> None:
    """Inner audit + cap-bump in a single transaction. Raises on DB errors;
    `record_attempt` wraps the call and swallows."""
    async with acquire() as conn, conn.transaction():
        await conn.execute(
            """
                INSERT INTO auto_apply_audit
                    (user_id, opportunity_id, application_id, decision, reason,
                     score, method, source_slug, dry_run)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
            user_id,
            opportunity_id,
            application_id,
            decision.decision,
            decision.reason,
            decision.score,
            decision.method,
            decision.source_slug,
            decision.dry_run,
        )
        if decision.decision == "submit":
            # Bump only on real submits. dry-runs deliberately skipped.
            await conn.execute(
                """
                    INSERT INTO auto_apply_daily_count (user_id, apply_date, submitted_count)
                    VALUES ($1, CURRENT_DATE, 1)
                    ON CONFLICT (user_id, apply_date) DO UPDATE
                        SET submitted_count = auto_apply_daily_count.submitted_count + 1,
                            updated_at = NOW()
                    """,
                user_id,
            )


__all__ = [
    "AutoApplyDecision",
    "Decision",
    "record_attempt",
    "should_auto_submit",
]

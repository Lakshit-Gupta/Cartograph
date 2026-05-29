"""Pick top-N opps that meet ALL filter criteria and dispatch them.

Used by:
  - /auto-apply slash command (user-triggered batch)
  - nightly auto-apply cron (08:30 IST, src/workers/scheduler.py)

Filter pipeline (executed entirely in SQL — no per-row Python):

  1. opp.apply_method ∈ prefs.auto_apply.apply_methods_whitelist
  2. source.auto_apply_enabled = TRUE
  3. opportunity_scores.score ≥ per_source_min_score[slug] OR global min_score
  4. opp.state ∈ (queued, ranked, digested, seen) — i.e. not already applied/skipped
  5. opp.posted_at > NOW() - max_age_days (when set)
  6. opp.comp_min_inr ≥ filters.min_comp_inr_month (when both set)

The hard `min_comp_inr_month` filter pairs with `config/sources/
internshala_filters.yaml:stipend_min_inr` — Internshala already filters
on its side at crawl time, so most non-matches never enter the DB.
This is the second layer for the rare opp that slips through.

Returns a list of (opportunity_id, score, source_slug) tuples sorted
by score DESC, limited to `remaining_daily_cap`.

`dispatch()` walks that list and publishes `stream:apply` for each
opp_id with `source='auto_cron'` so the applier-worker treats it as
auto-triggered (vs. user-clicked `/apply`). Each call goes through
`policy.should_auto_submit()` for the per-opp gate + audit row.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from src.common.db import fetch_all, fetch_one
from src.common.logger import get_logger
from src.common.queue import RedisQ, Streams
from src.common.secrets import get_settings

_log = get_logger(__name__)


@dataclass(frozen=True)
class EligibleOpp:
    opportunity_id: UUID
    score: float
    source_slug: str
    title: str
    company: str
    apply_url: str | None


@dataclass(frozen=True)
class DispatchSummary:
    """Returned by `dispatch()` for the slash command + cron to surface."""

    candidates_found: int
    daily_cap: int
    daily_count_before: int
    fired_count: int
    dry_run: bool
    skipped_reasons: dict[str, int]


def _load_prefs_block() -> dict[str, Any]:
    """Read `auto_apply:` block from `config/profile/prefs.yaml`. Empty
    dict on read failure so engine fails safe (no candidates)."""
    settings = get_settings()
    path = Path(settings.config_root) / "profile" / "prefs.yaml"
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        _log.warning("auto_apply_prefs_read_failed", err=str(e), path=str(path))
        return {}
    block = loaded.get("auto_apply") or {}
    return block if isinstance(block, dict) else {}


async def _fetch_daily_count(user_id: int) -> int:
    """Today's submitted count for the cap math."""
    try:
        rec = await fetch_one(
            "SELECT submitted_count FROM auto_apply_daily_count WHERE user_id = $1 AND apply_date = CURRENT_DATE",
            user_id,
        )
    except Exception as e:
        _log.warning("auto_apply_engine_daily_count_failed", err=str(e), user_id=user_id)
        return 0
    return int(rec["submitted_count"]) if rec is not None else 0


def _resolve_min_score(source_slug: str, prefs: dict[str, Any]) -> float:
    """Per-source override > global > 0.30 default."""
    per_source = prefs.get("per_source_min_score") or {}
    if isinstance(per_source, dict) and source_slug in per_source:
        return float(per_source[source_slug])
    return float(prefs.get("min_score", 0.30))


def _load_negative_keywords() -> list[str]:
    """Read `config/policy/negative_keywords.yaml` and flatten every
    category-grouped list (except `borderline_disabled`) into a single
    deduplicated list. Returns the lowercase terms ready for SQL ILIKE
    pattern wrapping.

    The borderline group is intentionally excluded so a future A/B
    rollback only needs a config edit, not a code change.
    """
    settings = get_settings()
    path = Path(settings.config_root) / "policy" / "negative_keywords.yaml"
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return []
    except Exception as e:
        _log.warning("auto_apply_negative_keywords_read_failed", err=str(e), path=str(path))
        return []
    block = loaded.get("negative_keywords") or {}
    if not isinstance(block, dict):
        return []
    terms: set[str] = set()
    for category, lst in block.items():
        if category == "borderline_disabled":
            continue
        if not isinstance(lst, list):
            continue
        for term in lst:
            if isinstance(term, str) and term.strip():
                terms.add(term.strip().lower())
    return sorted(terms)


def _build_negative_clause(terms: list[str]) -> str | None:
    """Render a NOT ILIKE-ANY clause across (title, description) for the
    given negative-keyword list. Returns None when the list is empty
    (no clause added)."""
    if not terms:
        return None
    # asyncpg + Postgres ILIKE doesn't have an ANY-array shortcut for
    # patterns, so we OR each term. Title weight = 1.0 (any match
    # disqualifies); description weight = lower (require term ~ pattern
    # OR title match). For phase-1 simplicity we OR on both with the
    # same weight; the borderline flag in negative_keywords.yaml is the
    # config-level dial for which terms even reach this SQL.
    title_or = " OR ".join(f"lower(o.title) LIKE '%{t.replace('%', '').replace(chr(39), '')}%'" for t in terms)
    desc_or = " OR ".join(f"lower(o.description) LIKE '%{t.replace('%', '').replace(chr(39), '')}%'" for t in terms)
    return f"NOT ({title_or} OR {desc_or})"


async def find_eligible(
    *,
    user_id: int,
    limit: int,
) -> list[EligibleOpp]:
    """SQL query — returns up to `limit` opps that pass every hard filter.

    SQL-only filter execution means a fresh dimension (e.g. comp floor
    raise) goes live the moment prefs.yaml is edited + applier reloads,
    without restarting any worker. The engine consults prefs on every
    call.
    """
    prefs = _load_prefs_block()
    if not prefs.get("enabled", False):
        _log.info("auto_apply_engine_disabled")
        return []

    apply_methods = list(prefs.get("apply_methods_whitelist") or ["in_platform", "email"])
    filters = prefs.get("filters") or {}
    min_comp_inr = filters.get("min_comp_inr_month")
    max_age_days = int(filters.get("max_age_days", 14))

    # Per-source min_score realised as a WHERE clause via CASE expression.
    # Easier to read + faster than emitting one query per source.
    per_source = prefs.get("per_source_min_score") or {}
    global_min = float(prefs.get("min_score", 0.30))
    if per_source:
        cases = " ".join(f"WHEN s.slug = '{slug}' THEN {float(v)}" for slug, v in per_source.items())
        min_score_expr = f"(CASE {cases} ELSE {global_min} END)"
    else:
        min_score_expr = f"{global_min}"

    where_clauses = [
        "s.auto_apply_enabled = TRUE",
        f"o.apply_method = ANY(ARRAY[{','.join(repr(m) for m in apply_methods)}]::apply_method_enum[])",
        "o.state IN ('queued','ranked','digested','seen')",
        f"os.score >= {min_score_expr}",
        f"(o.posted_at IS NULL OR o.posted_at > NOW() - INTERVAL '{max_age_days} days')",
    ]
    if isinstance(min_comp_inr, int | float) and min_comp_inr > 0:
        # STRICT: drop the "OR comp_min_inr IS NULL" pass-through. Pre-V023
        # opps with NULL comp_min_inr were leaking ALL ₹7k mechanical-
        # engineering style noise past the ₹30k floor. Strict mode means
        # the opp MUST have a populated comp value above the floor;
        # ranker_worker writes comp_min_inr on score, so any opp scored
        # after the V023 ship has a real number.
        #
        # Toggleable via prefs.auto_apply.filters.strict_comp (default true).
        strict_comp = bool(filters.get("strict_comp", True))
        if strict_comp:
            where_clauses.append(f"(o.comp_min_inr IS NOT NULL AND o.comp_min_inr >= {float(min_comp_inr)})")
        else:
            where_clauses.append(f"(o.comp_min_inr IS NULL OR o.comp_min_inr >= {float(min_comp_inr)})")

    # Negative-keyword filter — finally wired. Reject opps whose title or
    # description matches any term in
    # config/policy/negative_keywords.yaml (excluding the
    # borderline_disabled group, which stays inert until A/B'd in).
    neg_clause = _build_negative_clause(_load_negative_keywords())
    if neg_clause:
        where_clauses.append(neg_clause)

    sql = f"""
    SELECT o.id, os.score, s.slug, o.title, o.company, o.apply_url
    FROM opportunities o
    JOIN sources s ON s.id = o.source_id
    JOIN opportunity_scores os ON os.opportunity_id = o.id AND os.user_id = $1
    WHERE {" AND ".join(where_clauses)}
    ORDER BY os.score DESC NULLS LAST, o.first_seen DESC
    LIMIT {int(limit)}
    """
    try:
        rows = await fetch_all(sql, user_id)
    except Exception as e:
        _log.exception("auto_apply_engine_query_failed", err=str(e))
        return []

    return [
        EligibleOpp(
            opportunity_id=row["id"],
            score=float(row["score"]),
            source_slug=str(row["slug"]),
            title=str(row["title"] or "(untitled)"),
            company=str(row["company"] or "(unknown)"),
            apply_url=row["apply_url"],
        )
        for row in rows
    ]


async def _remaining_cap(user_id: int, prefs: dict[str, Any]) -> tuple[int, int, int]:
    """Returns (remaining, daily_cap, daily_count_before)."""
    cap = int(prefs.get("max_per_day", 3))
    used = await _fetch_daily_count(user_id)
    return max(0, cap - used), cap, used


async def dispatch(
    *,
    user_id: int,
    requested_count: int | None = None,
    source: str = "auto_cron",
) -> DispatchSummary:
    """Find eligible opps and enqueue them onto Streams.APPLY.

    Per-opp policy gate still runs inside applier-worker -> policy.
    Engine only enforces the BATCH-LEVEL caps (daily cap, requested count).
    Per-opp gates (score, source kill switch, method whitelist) all run
    again per opp so a config flip between engine + per-opp check still
    catches the latest state.

    `source='auto_cron'` is plumbed into the applier-worker so cron-fired
    applies are distinguishable from user-clicked ones in audit logs.
    """
    prefs = _load_prefs_block()
    remaining, daily_cap, used = await _remaining_cap(user_id, prefs)

    target = min(remaining, requested_count) if requested_count is not None else remaining
    if target <= 0:
        return DispatchSummary(
            candidates_found=0,
            daily_cap=daily_cap,
            daily_count_before=used,
            fired_count=0,
            dry_run=bool(prefs.get("dry_run", True)),
            skipped_reasons={"daily_cap_exhausted": 1} if remaining == 0 else {},
        )

    # Over-fetch a small margin so we still hit `target` even if a few
    # opps fail the per-opp policy gate inside applier-worker.
    candidates = await find_eligible(user_id=user_id, limit=target * 3)
    if not candidates:
        return DispatchSummary(
            candidates_found=0,
            daily_cap=daily_cap,
            daily_count_before=used,
            fired_count=0,
            dry_run=bool(prefs.get("dry_run", True)),
            skipped_reasons={"no_eligible_opps": 1},
        )

    queue = await RedisQ.connect()
    fired = 0
    for opp in candidates[:target]:
        try:
            await queue.publish(
                Streams.APPLY,
                {
                    "action": "apply",
                    "opp_id": str(opp.opportunity_id),
                    "user_id": user_id,
                    "source": source,
                },
            )
            fired += 1
        except Exception as e:
            _log.warning("auto_apply_engine_publish_failed", err=str(e), opp_id=str(opp.opportunity_id))

    _log.info(
        "auto_apply_engine_dispatched",
        fired=fired,
        candidates=len(candidates),
        daily_cap=daily_cap,
        daily_count_before=used,
        source=source,
    )

    return DispatchSummary(
        candidates_found=len(candidates),
        daily_cap=daily_cap,
        daily_count_before=used,
        fired_count=fired,
        dry_run=bool(prefs.get("dry_run", True)),
        skipped_reasons={},
    )


__all__ = [
    "DispatchSummary",
    "EligibleOpp",
    "dispatch",
    "find_eligible",
]

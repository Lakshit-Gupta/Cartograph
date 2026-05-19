"""Phase 2.2 — Resume A/B variant picker.

Selects which resume variant to send for an opportunity. Uses UCB1
(Upper-Confidence-Bound 1) over (variant_label, opp_category) pairs to
balance exploitation of high-response-rate variants against exploration
of less-tried ones. Cold start (no prior applications for the pair):
uniform over active variants.

Backward compat: when only a single active variant exists (or none —
Phase 1 single-tex tree), ``pick_variant`` returns that label (or the
default ``"backend"``) so the legacy single-variant path keeps working.

Math reminder (UCB1):
    ucb(arm) = mean_reward(arm) + sqrt(2 * ln(N) / n(arm))
    where N is total trials across all arms and n(arm) is per-arm count.
    Untried arms get an "infinite" bonus so each is tried at least once
    before exploitation kicks in.

The reward signal is binary: 1 if the application got any non-NULL
``response_status`` within 14d of ``sent_at``, 0 otherwise. The picker
queries this on demand from ``applications`` (no separate counter
table — recompute is cheap because we cap to a 30-day window).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any

from src.common.db import acquire, current_tenant
from src.common.logger import get_logger
from src.common.types import Opportunity

_log = get_logger(__name__)

_DEFAULT_VARIANT = "backend"
# Bounded recall window so we don't reward a variant for a 6-month-old reply
# while ignoring the last 4 weeks of silence.
_DEFAULT_WINDOW_DAYS = 30
# UCB1 exploration constant. Sqrt(2) is the textbook value; we slightly
# inflate it (1.5x) to keep variants with few samples in rotation for
# longer — solo user has thin signal.
_UCB_C = math.sqrt(2.0) * 1.5


@dataclass(frozen=True)
class VariantStats:
    """Per-arm rollup used by the UCB calculation."""

    variant_id: int
    label: str
    category: str
    sent: int
    responded: int

    @property
    def mean_reward(self) -> float:
        if self.sent == 0:
            return 0.0
        return self.responded / self.sent


async def _load_active_variants() -> dict[str, int]:
    """Return label -> id for every active variant for the current tenant.

    Reads `db.current_tenant()`. Empty dict when the V011 migration hasn't
    run or the seed inserts failed — the caller falls back to the legacy
    `backend` default.
    """
    try:
        async with acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, label
                  FROM resume_variants
                 WHERE user_id = $1 AND active = TRUE
                """,
                current_tenant(),
            )
    except Exception as e:
        _log.warning("variant_picker_db_read_failed", err=str(e))
        return {}
    return {str(r["label"]): int(r["id"]) for r in rows}


async def _load_stats(category: str, window_days: int) -> list[VariantStats]:
    """Roll up sent + responded counts per (variant, category) pair.

    Joins ``applications`` to ``opportunities`` so we can filter on
    ``opp.category``. ``response_status`` is treated as the signal — any
    non-NULL value within 14d of ``sent_at`` counts as a response. Rows
    with NULL ``resume_variant_id`` (Phase 1 legacy) are skipped so they
    can't inflate one variant's mean.
    """
    try:
        async with acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT v.id   AS variant_id,
                       v.label AS label,
                       o.category AS category,
                       COUNT(*) FILTER (WHERE a.id IS NOT NULL) AS sent,
                       COUNT(*) FILTER (
                         WHERE a.response_status IS NOT NULL
                           AND a.response_at IS NOT NULL
                           AND a.response_at <= a.sent_at + INTERVAL '14 days'
                       ) AS responded
                  FROM resume_variants v
                  LEFT JOIN applications a
                    ON a.resume_variant_id = v.id
                   AND a.sent_at >= NOW() - ($1::int || ' days')::interval
                  LEFT JOIN opportunities o
                    ON o.id = a.opportunity_id
                   AND o.category = $2
                 WHERE v.user_id = $3 AND v.active = TRUE
                 GROUP BY v.id, v.label, o.category
                """,
                window_days,
                category,
                current_tenant(),
            )
    except Exception as e:
        _log.warning("variant_picker_stats_read_failed", err=str(e), category=category)
        return []
    out: list[VariantStats] = []
    for r in rows:
        out.append(
            VariantStats(
                variant_id=int(r["variant_id"]),
                label=str(r["label"]),
                category=str(r["category"] or category),
                sent=int(r["sent"] or 0),
                responded=int(r["responded"] or 0),
            )
        )
    return out


def _ucb1_pick(stats: list[VariantStats]) -> VariantStats:
    """Return the arm with the highest UCB score.

    Untried arms (sent=0) win immediately — they get an infinite bonus,
    which guarantees every variant is tried at least once before the
    algorithm starts exploiting.
    """
    total_n = sum(s.sent for s in stats)
    if total_n == 0:
        return random.choice(stats)

    untried = [s for s in stats if s.sent == 0]
    if untried:
        return random.choice(untried)

    ln_n = math.log(total_n)
    return max(
        stats,
        key=lambda s: s.mean_reward + _UCB_C * math.sqrt(ln_n / max(s.sent, 1)),
    )


def _opp_category(opp: Opportunity | dict[str, Any]) -> str:
    """Coerce the opp.category column into a stable lowercase string."""
    if isinstance(opp, dict):
        raw = opp.get("category")
    else:
        raw = getattr(opp, "category", None)
    if raw is None:
        return "unknown"
    val = getattr(raw, "value", raw)
    return str(val).lower()


async def pick_variant_async(
    opp: Opportunity | dict[str, Any],
    *,
    window_days: int = _DEFAULT_WINDOW_DAYS,
) -> str:
    """Pick the variant label whose rolling UCB score is highest for this opp.

    Args:
        opp: an Opportunity-like dict (or pydantic Opportunity) — only
            ``category`` is consulted today, but the signature accepts the
            full row so the picker can grow more features (remote_type,
            geo bias) without churning every caller.
        window_days: rolling lookback for the response-rate signal.

    Returns:
        The label of the chosen variant (e.g. "backend"). Falls back to
        ``"backend"`` when no active variants exist (V011 not run, seed
        failed, or backward-compat single-variant config).
    """
    actives = await _load_active_variants()
    if not actives:
        return _DEFAULT_VARIANT
    if len(actives) == 1:
        return next(iter(actives.keys()))

    category = _opp_category(opp)
    stats = await _load_stats(category, window_days)
    if not stats:
        # No rows came back at all (DB unreachable / table truly empty).
        # Fall back to uniform random over active variant labels.
        return random.choice(list(actives.keys()))

    chosen = _ucb1_pick(stats)
    _log.info(
        "variant_picked",
        label=chosen.label,
        category=category,
        sent=chosen.sent,
        responded=chosen.responded,
        mean_reward=round(chosen.mean_reward, 4),
    )
    return chosen.label


async def variant_id_for_label(label: str) -> int | None:
    """Look up resume_variants.id by label for the DB FK column."""
    actives = await _load_active_variants()
    return actives.get(label)


# ---------------------------------------------------------------------------
# Public sync wrapper for cold-path tests / non-async call sites.
# The async DB path is the only production code path; this helper is here
# so unit tests can pass a fake ``stats`` list and verify the picker math
# without spinning up Postgres.
# ---------------------------------------------------------------------------
def pick_from_stats(stats: list[VariantStats], cold_start_default: str = _DEFAULT_VARIANT) -> str:
    """Pure-function UCB1 pick over a hand-built stats list.

    Returns ``cold_start_default`` when ``stats`` is empty so the test
    helper mirrors the production fallback exactly.
    """
    if not stats:
        return cold_start_default
    return _ucb1_pick(stats).label


__all__ = [
    "VariantStats",
    "pick_from_stats",
    "pick_variant_async",
    "variant_id_for_label",
]

"""Cycle-report dataclass + pure (IO-free) helpers for the discovery worker.

Everything here is deliberately side-effect-free so it is unit-testable without
a browser, Redis, or Postgres:

  - `passes_floor`   — comp-floor predicate over an Opportunity.
  - `dedup_key`      — Redis SET-NX key for a canonical URL.
  - `DiscoveryCycleReport` — the per-cycle tally; `to_row()` maps it to the
    `discovery_cycle_log` columns and `to_details()` to the notify payload's
    `details` block.
  - `build_summary`  — the single-line Discord summary string.
  - `build_cycle_report_payload` — the FROZEN `stream:notify` payload shape the
    `notify_discovery_cycle` Discord handler consumes.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any

from src.common.currency import to_inr_per_month
from src.common.types import Opportunity

# Stream payload discriminator — the Discord handler matches on this exact value.
CYCLE_REPORT_KIND = "discovery_cycle_report"


def passes_floor(opp: Opportunity, floor_inr: float) -> bool:
    """True when the opp's stipend normalises to >= `floor_inr` INR/month.

    Uses `comp_max` when present (range upper bound — a "₹15k-35k" listing clears
    a 30k floor), else `comp_min`. A null/unparseable amount or an amount that
    `to_inr_per_month` cannot convert (unknown currency) is treated as sub-floor
    and dropped — discovery only publishes opps with a demonstrable >= floor
    stipend, since the whole point of the worker is to enforce the floor the
    Internshala UI cannot.
    """
    native = opp.comp_max if opp.comp_max is not None else opp.comp_min
    if native is None:
        return False
    floor = to_inr_per_month(native, opp.comp_currency, opp.comp_period)
    if floor is None:
        return False
    return floor >= floor_inr


def dedup_key(canonical_url: str) -> str:
    """Redis SET-NX dedup key: `internshala:seen:<sha256(canonical_url)>`."""
    digest = hashlib.sha256(canonical_url.encode()).hexdigest()
    return f"internshala:seen:{digest}"


def _fmt_duration(seconds: float) -> str:
    """`192.4` -> `3m12s`; sub-minute -> `48s`."""
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


@dataclass(slots=True)
class DiscoveryCycleReport:
    """One discovery cycle's outcome. Field names mirror `discovery_cycle_log`
    columns 1:1 so `to_row()` is a straight projection."""

    cycle_id: str
    worker_id: str
    source_slug: str
    started_at: str  # ISO-8601 UTC
    duration_sec: float
    combos_attempted: int = 0
    combos_succeeded: int = 0
    combo_timeouts: list[str] = field(default_factory=list)
    selector_misses: list[str] = field(default_factory=list)
    cards_scraped: int = 0
    cards_published: int = 0
    cards_rejected_subfloor: int = 0
    cards_rejected_dedup: int = 0
    cards_rejected_parse: int = 0
    healthy: bool = True
    selectors_version: str = ""
    matrix_version: str = ""

    def to_details(self) -> dict[str, Any]:
        """Full report as a JSON-safe dict (the notify payload `details` block)."""
        return asdict(self)

    def to_row(self) -> dict[str, Any]:
        """Column-name -> value map for the `discovery_cycle_log` INSERT.

        Identical keys to `to_details()` today, but kept as a separate method so
        the SQL projection and the wire payload can diverge without surprise.
        """
        return asdict(self)


def build_summary(report: DiscoveryCycleReport) -> str:
    """Single-line Discord summary, e.g.
    `✓ 47 cards • 12/12 combos • 3m12s • selectors 2026.05.29.v1`.
    Degraded cycles lead with `✗` and append the timeout / miss counts.
    """
    mark = "✓" if report.healthy else "✗"
    head = (
        f"{mark} {report.cards_published} cards • "
        f"{report.combos_succeeded}/{report.combos_attempted} combos • "
        f"{_fmt_duration(report.duration_sec)} • "
        f"selectors {report.selectors_version or '?'}"
    )
    if report.combo_timeouts or report.selector_misses:
        head += f" • {len(report.combo_timeouts)} timeouts • {len(report.selector_misses)} selector-misses"
    return head


def build_cycle_report_payload(
    report: DiscoveryCycleReport,
    *,
    screenshot_b64: str | None = None,
) -> dict[str, Any]:
    """Assemble the FROZEN `stream:notify` cycle-report payload.

    Shape (consumed by `notify_discovery_cycle`):
        {kind, cycle_id, source_slug, started_at, duration_sec, summary,
         healthy, screenshot_b64, details}
    """
    return {
        "kind": CYCLE_REPORT_KIND,
        "cycle_id": report.cycle_id,
        "source_slug": report.source_slug,
        "started_at": report.started_at,
        "duration_sec": report.duration_sec,
        "summary": build_summary(report),
        "healthy": report.healthy,
        "screenshot_b64": screenshot_b64,
        "details": report.to_details(),
    }


__all__ = [
    "CYCLE_REPORT_KIND",
    "DiscoveryCycleReport",
    "build_cycle_report_payload",
    "build_summary",
    "dedup_key",
    "passes_floor",
]

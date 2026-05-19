"""Dark-source discovery pipeline.

Orchestrates the 4 strategies → dedupe → classifier → promoter cycle.
Called by the cron entrypoint (`src/workers/dark_source_discovery.py`)
and the test suite.

Hard caps enforced here:
  - LLM classifier calls per run capped by settings.dark_source_daily_llm_cap.
    Overflow candidates are simply skipped (not enqueued for tomorrow — the
    weekly cron means a re-run captures them naturally + dedupe filters
    already-seen URLs).
  - Strategies run sequentially (not asyncio.gather) — CLAUDE.md hard
    constraint: identifiable HTTP fingerprint + simpler rate-limit story.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from src.common import db
from src.common.logger import get_logger
from src.common.secrets import get_settings
from src.sources.discovery import classifier, promoter
from src.sources.discovery.base import CandidateSource, DiscoveryStrategy
from src.sources.discovery.github_awesome import GitHubAwesomeStrategy
from src.sources.discovery.google_dorks import GoogleDorksStrategy
from src.sources.discovery.hn_algolia import HNAlgoliaStrategy
from src.sources.discovery.reddit_search import RedditSearchStrategy

_log = get_logger(__name__)


@dataclass
class StrategyRunStats:
    name: str
    discovered: int = 0
    classified: int = 0
    auto_promoted: int = 0
    pending: int = 0
    discarded: int = 0
    duplicates: int = 0
    skipped_for_cap: int = 0


@dataclass
class PipelineRunStats:
    started_at: str
    finished_at: str | None = None
    strategy_stats: list[StrategyRunStats] = field(default_factory=list)
    total_llm_calls: int = 0
    total_auto_promoted: int = 0
    total_pending: int = 0


def _all_strategies() -> list[DiscoveryStrategy]:
    """The 4 strategies, in run order. Order = stable for test snapshots."""
    return [
        GitHubAwesomeStrategy(),
        HNAlgoliaStrategy(),
        RedditSearchStrategy(),
        GoogleDorksStrategy(),
    ]


async def _load_active_strategy_names() -> set[str]:
    """Read discovery_strategies.active so the user can pause one without
    code changes. Falls open (run all) if the table is empty / unreachable."""
    try:
        rows = await db.fetch_all("SELECT name FROM discovery_strategies WHERE active IS TRUE")
    except Exception as e:
        _log.warning("active_strategies_read_failed", err=str(e))
        return {s.name for s in _all_strategies()}
    return {r["name"] for r in rows} or {s.name for s in _all_strategies()}


async def _update_strategy_counters(stats: StrategyRunStats) -> None:
    """Best-effort write of per-strategy counters into discovery_strategies."""
    try:
        await db.execute(
            """
            UPDATE discovery_strategies
               SET last_run_at = NOW(),
                   discovered_count = COALESCE(discovered_count, 0) + $2,
                   promoted_count  = COALESCE(promoted_count, 0)  + $3,
                   discarded_count = COALESCE(discarded_count, 0) + $4
             WHERE name = $1
            """,
            stats.name,
            stats.discovered,
            stats.auto_promoted,
            stats.discarded,
        )
    except Exception as e:
        _log.warning("strategy_counter_update_failed", strategy=stats.name, err=str(e))


def _dedupe_within_batch(candidates: list[CandidateSource]) -> list[CandidateSource]:
    """First-pass dedupe by exact URL — strategies sometimes return the same
    URL twice (e.g. a README that links to RemoteOK both in a table and a
    bullet). DB layer dedupes again on insert; this just saves LLM calls."""
    seen: set[str] = set()
    out: list[CandidateSource] = []
    for c in candidates:
        if c.url in seen:
            continue
        seen.add(c.url)
        out.append(c)
    return out


async def _already_known(url: str) -> bool:
    """Pre-filter against sources.base_url + candidate_sources.url before LLM call."""
    rec = await db.fetch_one(
        """
        SELECT 1 FROM sources WHERE base_url = $1
        UNION ALL
        SELECT 1 FROM candidate_sources WHERE url = $1
        LIMIT 1
        """,
        url,
    )
    return rec is not None


async def run_discovery_pipeline(
    *,
    http_client: httpx.AsyncClient | None = None,
    llm_cap: int | None = None,
) -> PipelineRunStats:
    """Run all enabled strategies. Returns aggregated stats.

    Pre-filters duplicates against the DB so LLM budget isn't burned on URLs
    we already know about. Per-candidate LLM call is made one-at-a-time
    (sequential) to keep the daily cap honest.
    """
    settings = get_settings()
    cap = llm_cap if llm_cap is not None else settings.dark_source_daily_llm_cap

    started_at = datetime.now(UTC).isoformat()
    run_stats = PipelineRunStats(started_at=started_at)

    active = await _load_active_strategy_names()
    strategies = [s for s in _all_strategies() if s.name in active]
    if not strategies:
        _log.warning("no_active_strategies")
        run_stats.finished_at = datetime.now(UTC).isoformat()
        return run_stats

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=20.0)

    try:
        llm_budget_remaining = cap
        for strategy in strategies:
            s_stats = StrategyRunStats(name=strategy.name)
            try:
                raw_candidates = await strategy.run(client)
            except Exception as e:
                _log.exception("strategy_run_failed", strategy=strategy.name, err=str(e))
                run_stats.strategy_stats.append(s_stats)
                continue

            deduped = _dedupe_within_batch(raw_candidates)
            s_stats.discovered = len(deduped)
            _log.info("strategy_done", strategy=strategy.name, raw=len(raw_candidates), deduped=len(deduped))

            # Pre-filter against the DB to save LLM budget.
            survivors: list[CandidateSource] = []
            for c in deduped:
                if await _already_known(c.url):
                    s_stats.duplicates += 1
                    continue
                survivors.append(c)

            # Classify each surviving candidate, respecting cap.
            classified: list[CandidateSource] = []
            for c in survivors:
                if llm_budget_remaining <= 0:
                    s_stats.skipped_for_cap += 1
                    continue
                result = await classifier.classify(c)
                run_stats.total_llm_calls += 1
                llm_budget_remaining -= 1
                if result is None:
                    s_stats.discarded += 1
                    continue
                classifier.apply_to_candidate(c, result)
                s_stats.classified += 1
                classified.append(c)

            # Promote / queue / drop.
            p_stats = await promoter.promote_candidates(classified)
            s_stats.auto_promoted = p_stats.auto_promoted
            s_stats.pending = p_stats.pending
            s_stats.discarded += p_stats.discarded
            s_stats.duplicates += p_stats.duplicates

            await _update_strategy_counters(s_stats)
            run_stats.strategy_stats.append(s_stats)
            run_stats.total_auto_promoted += s_stats.auto_promoted
            run_stats.total_pending += s_stats.pending
    finally:
        if owns_client:
            await client.aclose()

    run_stats.finished_at = datetime.now(UTC).isoformat()
    _log.info(
        "discovery_pipeline_done",
        strategies=len(run_stats.strategy_stats),
        llm_calls=run_stats.total_llm_calls,
        auto_promoted=run_stats.total_auto_promoted,
        pending=run_stats.total_pending,
    )
    return run_stats

"""Phase 3.4 — OSS contribution funnel worker.

Daily cron entrypoint (08:00 IST = 02:30 UTC) that:

  1. Reads `target_companies WHERE active=true AND github_org IS NOT NULL`.
  2. For each company, queries GitHub Search for "good first issue"
     issues in the org. Filters out stale (>30d) and already-assigned.
  3. Maps surviving issues to `Opportunity` rows
     (category=freelance, apply_method=external, no compensation).
  4. Publishes via `extractors.persist.persist_and_publish` — the
     same canonical write path the freelance Telegram lane uses.
     That handles dedup, opportunities upsert, and Streams.RANK emit.
  5. Updates `last_funnel_scan_at` + `issues_emitted_30d` per company.
  6. Logs daily stats: scanned, emitted, deduped, rate-limited.

The worker drives off APScheduler, NOT a Redis stream consumer —
this is a once-a-day push, not an event-driven flow. The cron is
ALSO registered in `src/workers/scheduler.py` (one extra job) so a
fresh `jobs-scheduler` install picks it up; this module mirrors the
trigger so an admin can `docker compose exec scheduler python -m
src.workers.oss_funnel` for a one-off manual scan.

Boot behaviour:
  * Initialise DB pool + Redis publisher.
  * If `mp_oss_funnel_enabled=False`, log + idle.
  * Wait for SIGINT/SIGTERM.

The scan is idempotent across restarts because every Opportunity row
carries the deterministic `oss:<org>:<repo>:<issue>` fingerprint hash;
`persist_and_publish.already_known` short-circuits second emits.
"""

from __future__ import annotations

import asyncio
import signal
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.common.db import acquire, close_pool, fetch_one, init_pool
from src.common.logger import configure_logging, get_logger
from src.common.queue import RedisQ
from src.common.secrets import get_settings
from src.extractors.persist import persist_and_publish
from src.sources.oss_funnel.github_issues import (
    GitHubIssueScanner,
    parse_issue_to_opportunity,
)

configure_logging("oss_funnel")
_log = get_logger(__name__)

# 02:30 UTC == 08:00 IST. Hardcoded — the OSS funnel's only consumer
# is the daily digest, which runs ~02:30 UTC by default. Running the
# funnel BEFORE the digest tick maximises the chance that newly
# scraped issues make it into the same day's digest.
_CRON_HOUR_UTC = 2
_CRON_MINUTE_UTC = 30


@dataclass(frozen=True, slots=True)
class ScanSummary:
    """Per-run aggregate. Logged + returned for the CLI smoke probe."""

    companies_scanned: int = 0
    issues_seen: int = 0
    issues_emitted: int = 0
    issues_deduped: int = 0
    companies_rate_limited: int = 0
    companies_errored: int = 0


async def _load_active_targets() -> list[dict]:
    """Return rows the funnel cares about. Filters happen in SQL to
    keep the per-tick query cheap even when target_companies grows."""
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, user_id, name, github_org
            FROM target_companies
            WHERE active = TRUE
              AND github_org IS NOT NULL
              AND github_org <> ''
            ORDER BY id
            """
        )
    return [dict(r) for r in rows]


async def _resolve_source_id() -> int | None:
    """Cache the synthetic 'oss_funnel' source row id at start of scan.

    V016 seeds the row with `slug='oss_funnel'`, `status='paused'`.
    Returning None here causes run_daily_scan to bail with a single
    error log — preferable to letting persist_and_publish fail with
    a confusing FK error per emit.
    """
    rec = await fetch_one("SELECT id FROM sources WHERE slug = 'oss_funnel'")
    return int(rec["id"]) if rec else None


async def _bump_company_stats(*, company_id: int, emitted_this_tick: int) -> None:
    """Update `target_companies.last_funnel_scan_at` + the 30d counter.

    `issues_emitted_30d` is a simple incrementer — Phase 5 will swap
    it for a windowed query if the counter drifts. For Phase 3 the
    rough rolling count is enough for /status to surface 'is this
    target actually generating signal?' at a glance.
    """
    async with acquire() as conn:
        await conn.execute(
            """
            UPDATE target_companies
            SET last_funnel_scan_at = NOW(),
                issues_emitted_30d = issues_emitted_30d + $2
            WHERE id = $1
            """,
            company_id,
            emitted_this_tick,
        )


async def run_daily_scan(
    q: RedisQ | None = None,
    *,
    per_company_limit: int = 5,
) -> ScanSummary:
    """One-shot scan over all active target_companies.

    `q` may be None for unit tests; runtime callers (the cron handler
    and the CLI smoke probe) always pass a connected RedisQ so
    persist_and_publish can emit onto Streams.RANK.
    """
    s = get_settings()
    if not s.mp_oss_funnel_enabled:
        _log.info("oss_funnel_disabled")
        return ScanSummary()

    if q is None:
        q = await RedisQ.connect()

    source_id = await _resolve_source_id()
    if source_id is None:
        _log.error("oss_funnel_source_row_missing", slug="oss_funnel")
        return ScanSummary()

    targets = await _load_active_targets()
    if not targets:
        _log.info("oss_funnel_no_targets")
        return ScanSummary()

    scanner = GitHubIssueScanner(token=s.github_token)
    summary = ScanSummary()
    try:
        async with scanner:
            for tgt in targets:
                org = (tgt["github_org"] or "").strip()
                if not org:
                    continue
                result = await scanner.fetch_company_issues(org, limit=per_company_limit)
                summary = ScanSummary(
                    companies_scanned=summary.companies_scanned + 1,
                    issues_seen=summary.issues_seen + len(result.issues),
                    issues_emitted=summary.issues_emitted,
                    issues_deduped=summary.issues_deduped,
                    companies_rate_limited=summary.companies_rate_limited + (1 if result.rate_limited else 0),
                    companies_errored=summary.companies_errored + (1 if result.error else 0),
                )
                if result.rate_limited:
                    _log.warning("oss_funnel_company_rate_limited", org=org, name=tgt["name"])
                    continue
                if result.error:
                    _log.warning("oss_funnel_company_error", org=org, name=tgt["name"], err=result.error)
                    continue

                emitted_for_company = 0
                deduped_for_company = 0
                for issue in result.issues:
                    opp = parse_issue_to_opportunity(
                        issue,
                        source_id=source_id,
                        company_name=tgt["name"],
                    )
                    if opp is None:
                        continue
                    opp_id = await persist_and_publish(q, opp, user_id=int(tgt["user_id"]))
                    if opp_id is None:
                        deduped_for_company += 1
                    else:
                        emitted_for_company += 1

                if emitted_for_company:
                    await _bump_company_stats(
                        company_id=int(tgt["id"]),
                        emitted_this_tick=emitted_for_company,
                    )
                else:
                    # Touch last_funnel_scan_at even on empty scan so /status
                    # reflects "we did look, there just wasn't anything".
                    await _bump_company_stats(company_id=int(tgt["id"]), emitted_this_tick=0)

                summary = ScanSummary(
                    companies_scanned=summary.companies_scanned,
                    issues_seen=summary.issues_seen,
                    issues_emitted=summary.issues_emitted + emitted_for_company,
                    issues_deduped=summary.issues_deduped + deduped_for_company,
                    companies_rate_limited=summary.companies_rate_limited,
                    companies_errored=summary.companies_errored,
                )
                _log.info(
                    "oss_funnel_company_done",
                    org=org,
                    name=tgt["name"],
                    issues_seen=len(result.issues),
                    emitted=emitted_for_company,
                    deduped=deduped_for_company,
                )
    finally:
        await scanner.aclose()

    _log.info("oss_funnel_scan_done", **asdict(summary))
    return summary


async def main() -> None:
    await init_pool()
    q = await RedisQ.connect()

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        run_daily_scan,
        CronTrigger(hour=_CRON_HOUR_UTC, minute=_CRON_MINUTE_UTC),
        args=[q],
        id="oss_funnel_daily",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()

    _log.info(
        "oss_funnel_started",
        now=datetime.now(UTC).isoformat(),
        enabled=get_settings().mp_oss_funnel_enabled,
        cron_utc=f"{_CRON_HOUR_UTC:02d}:{_CRON_MINUTE_UTC:02d}",
    )

    stop = asyncio.Event()

    def _stop(*_a: object) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop)

    await stop.wait()
    scheduler.shutdown(wait=False)
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

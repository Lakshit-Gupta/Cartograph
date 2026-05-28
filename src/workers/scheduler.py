"""APScheduler driver — turns sources.fetch_freq_minutes into Redis Streams FetchTask emissions."""

from __future__ import annotations

import asyncio
import signal
import uuid
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.common.db import acquire, close_pool, init_pool
from src.common.logger import configure_logging, get_logger
from src.common.queue import RedisQ, Streams
from src.common.types import FetchTask

configure_logging("scheduler")
_log = get_logger(__name__)


async def emit_for_active_sources(q: RedisQ) -> None:
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, slug, base_url, crawler_strategy, fetch_freq_minutes, tier_chain
            FROM sources
            WHERE status = 'active'
              AND (last_successful_crawl_at IS NULL
                   OR last_successful_crawl_at < NOW() - (fetch_freq_minutes || ' minutes')::interval)
            """
        )
    if not rows:
        return

    # Lazy import — registry imports plugins eagerly which is fine, but avoid
    # cycling during startup.
    from src.sources.registry import get as get_plugin

    for r in rows:
        plugin = get_plugin(r["crawler_strategy"])
        if plugin is None:
            _log.warning("strategy_unregistered", strategy=r["crawler_strategy"])
            continue
        try:
            plan = await plugin.plan(
                source_id=int(r["id"]),
                base_url=r["base_url"],
                config={"slug": r["slug"]},
            )
        except Exception as e:
            _log.warning("plan_failed", source=r["slug"], err=str(e))
            continue

        for url in plan.urls:
            task = FetchTask(
                source_id=plan.source_id,
                source_slug=plan.source_slug,
                url=url,
                crawler_strategy=r["crawler_strategy"],
                tier_chain=plan.tier_chain or list(r["tier_chain"] or [0]),
                requires_identity=plan.requires_identity,
                correlation_id=uuid.uuid4().hex,
            )
            await q.publish(Streams.FETCH, task.model_dump(mode="json"))

        # Optimistically advance last_successful_crawl_at after queueing
        async with acquire() as conn:
            await conn.execute(
                "UPDATE sources SET last_successful_crawl_at = NOW() WHERE id = $1",
                int(r["id"]),
            )

    _log.info("scheduler_tick_emitted", sources=len(rows))


async def emit_daily_digest(q: RedisQ) -> None:
    await q.publish(Streams.NOTIFY, {"kind": "digest", "user_id": 1, "scheduled": True})


_DIGEST_SCHEDULE_CACHE: tuple[int, int] = (2, 30)  # (hour_utc, minute_utc)


async def reload_digest_schedule(scheduler: AsyncIOScheduler) -> None:
    """Poll users.digest_hour_utc / digest_minute_utc for owner=1 and reschedule.

    Called every 60s. No-op when the DB row matches the active cron trigger.
    Cheap query — one row, one column compare.
    """
    global _DIGEST_SCHEDULE_CACHE
    try:
        async with acquire() as conn:
            rec = await conn.fetchrow("SELECT digest_hour_utc, digest_minute_utc FROM users WHERE id = 1")
    except Exception as e:
        _log.warning("digest_schedule_db_read_failed", err=str(e))
        return
    if rec is None:
        return
    hh, mm = int(rec["digest_hour_utc"]), int(rec["digest_minute_utc"])
    if (hh, mm) == _DIGEST_SCHEDULE_CACHE:
        return
    scheduler.reschedule_job("emit_digest_utc", trigger=CronTrigger(hour=hh, minute=mm))
    _DIGEST_SCHEDULE_CACHE = (hh, mm)
    _log.info("digest_schedule_reloaded", hour_utc=hh, minute_utc=mm)


async def emit_apply_rate_nudge(q: RedisQ) -> None:
    """9pm reminder if applied < target today."""
    async with acquire() as conn:
        rec = await conn.fetchrow("SELECT COUNT(*) AS n FROM applications WHERE sent_at::date = CURRENT_DATE")
    sent = int(rec["n"] if rec else 0)
    if sent < 5:
        await q.publish(
            Streams.ALERTS,
            {"kind": "apply_rate_under_target_9pm", "sent": sent, "target": 5},
        )


async def emit_daily_followup_scan(q: RedisQ) -> None:
    """Phase 2.3 — 13:00 IST follow-up scan.

    Calls ``daily_followup_scan`` which scans applications older than
    the configured window, drafts follow-ups via LLM, persists draft
    rows, and publishes ``kind=followup_ready`` onto Streams.NOTIFY so
    the Discord notifier surfaces them with Send / Edit / Skip buttons.

    Idempotent: the UNIQUE(application_id) constraint on followups means
    running this twice in the same day is a no-op for already-drafted
    rows. The feature flag (settings.mp_followup_enabled) gates the
    entire scan inside find_eligible_applications.
    """
    try:
        from src.application.followup import daily_followup_scan
    except Exception as e:
        _log.warning("followup_module_import_failed", err=str(e))
        return
    try:
        stats = await daily_followup_scan(q)
        _log.info("followup_cron_tick", **stats)
    except Exception as e:
        _log.exception("followup_cron_failed", err=str(e))


async def weekly_variant_refit(q: RedisQ) -> None:
    """Phase 2.2 — weekly refit of ``resume_variants.weight``.

    Pulls the last 30 days of applications + opportunity_transitions and
    writes a smoothed per-variant weight back to the table. Pure-local
    sklearn-style compute — no LLM call, so no daily-spend gate.

    Emits a heartbeat onto ``Streams.ALERTS`` so the user sees a row in
    ``#🔔-alerts`` confirming the refit ran. Failure is logged but does
    not propagate — the picker degrades gracefully to UCB1 with the
    previous (or seeded) weights.
    """
    try:
        from src.ranker.variant_refit import refit_variant_weights

        weights = await refit_variant_weights()
    except Exception as e:
        _log.warning("variant_refit_cron_failed", err=str(e))
        return
    await q.publish(
        Streams.ALERTS,
        {"kind": "variant_refit_done", "weights": weights},
    )


async def weekly_dark_source_discovery(q: RedisQ) -> None:
    """Phase 3.2 — weekly dark-source discovery cron.

    Sunday 04:00 IST. Gated by ``settings.mp_dark_source_discovery_enabled``
    inside ``run_once`` so flipping the flag off via SOPS is instant; the
    cron still fires but the handler returns ``{"status": "disabled"}`` and
    publishes nothing.

    Inside the gate: pulls all 4 strategies, classifies via LLM (cap 50/day
    enforced inside the pipeline), and auto-promotes high-confidence rows
    into ``sources``. Mid-confidence land in ``candidate_sources`` for the
    user to review via ``/review``.

    Failure is logged but does not propagate — the rest of the scheduler
    keeps emitting fetch ticks and digest crons regardless.
    """
    try:
        from src.workers.dark_source_discovery import run_once

        result = await run_once(publish_alert=True)
    except Exception as e:
        _log.warning("dark_source_discovery_cron_failed", err=str(e))
        return
    _log.info("dark_source_discovery_cron_done", **{k: v for k, v in result.items() if k != "strategies"})


async def emit_daily_oss_funnel_scan(q: RedisQ) -> None:
    """Phase 3.4 — 08:00 IST OSS contribution funnel scan.

    Imports lazily because the worker pulls in httpx +
    extractors.persist + sources.oss_funnel.github_issues. Keeping
    the import inside the handler stops a missing-dependency hiccup
    in the OSS funnel module from taking down the rest of the
    scheduler.

    Idempotent across reruns — every Opportunity emitted carries a
    deterministic ``oss:<org>:<repo>:<issue>`` fingerprint hash that
    persist_and_publish dedupes against. Gated by
    ``settings.mp_oss_funnel_enabled`` inside ``run_daily_scan`` so
    flipping the flag off via SOPS is instant; the cron still fires
    but the handler returns an empty summary and publishes nothing.
    """
    try:
        from src.workers.oss_funnel import run_daily_scan
    except Exception as e:
        _log.warning("oss_funnel_module_import_failed", err=str(e))
        return
    try:
        summary = await run_daily_scan(q)
        _log.info(
            "oss_funnel_cron_tick",
            **{k: getattr(summary, k) for k in summary.__dataclass_fields__},
        )
    except Exception as e:
        _log.exception("oss_funnel_cron_failed", err=str(e))


async def nightly_global_ranker_refit(q: RedisQ) -> None:
    """Phase 5.3 — nightly refit of the global formula weights.

    Pulls every application from the last 90 days, joins
    `opportunity_scores.score_components` (the six features the scorer
    used at the time) + `opportunity_transitions` (engagement label
    inside a 30-day window), fits L2 logistic regression, persists one
    row to `ranker_weights_fit`.

    Cold-start safe: <50 labelled apps → row inserted with
    status='cold_start' and the formula keeps reading
    `config/profile/prefs.yaml`. Failures land status='failed' with
    `error_message` — the scheduler keeps ticking.

    Cache invalidation: on success we clear the per-tenant fit cache in
    `formula._fit_cache` so the next score() picks up the new weights
    immediately instead of waiting up to 5 minutes.

    Free-only: pure local sklearn LR; no LLM / proxy spend.
    """
    try:
        from src.ranker.formula import invalidate_fit_cache
        from src.ranker.global_refit import run_nightly_refit

        summary = await run_nightly_refit()
    except Exception as e:
        _log.warning("global_ranker_refit_cron_failed", err=str(e))
        return
    if summary.get("status") == "ok":
        try:
            invalidate_fit_cache(int(summary.get("user_id") or 0))
        except Exception as e:
            _log.warning("global_ranker_refit_cache_invalidate_failed", err=str(e))
    await q.publish(
        Streams.ALERTS,
        {
            "kind": "global_ranker_refit_done",
            "status": summary.get("status"),
            "rows_used": summary.get("rows_used"),
            "positive_rate": summary.get("positive_rate"),
            "auc": summary.get("auc"),
        },
    )


async def emit_daily_auto_apply(q: RedisQ) -> None:
    """Phase 4 v2 — daily auto-apply cron at 08:30 IST.

    Calls `auto_apply_engine.dispatch()` which:
      1. Reads `prefs.auto_apply` (filters + caps + whitelists).
      2. Queries opportunities matching every hard filter via SQL.
      3. Enqueues `stream:apply` for the top-N matches (capped by
         `max_per_day` minus today's count).

    Per-opp policy gate (score, source kill switch, method whitelist)
    runs again inside applier-worker, so a flag flip between cron tick
    and per-opp consideration still catches the latest state.

    Failure logged but does not propagate — the scheduler keeps ticking
    every other cron regardless.
    """
    _ = q
    try:
        from src.application.auto_apply_engine import dispatch

        summary = await dispatch(user_id=1, source="auto_cron")
    except Exception as e:
        _log.warning("daily_auto_apply_cron_failed", err=str(e))
        return
    _log.info(
        "daily_auto_apply_cron_tick",
        fired=summary.fired_count,
        candidates=summary.candidates_found,
        daily_cap=summary.daily_cap,
        daily_count_before=summary.daily_count_before,
        dry_run=summary.dry_run,
    )


async def weekly_source_refit(q: RedisQ) -> None:
    """Phase 2.4 — weekly refit of ``sources.ranking_weight``.

    Fits an L2 logistic regression over the last 90 days of applications
    joined to opportunity_transitions (engagement-window labels) and
    writes per-source weights in ``[0.5, 2.0]`` back to the sources
    table. The formula ranker already reads that column, so the next
    opp scored picks up the new weight without further code changes.

    Cold-start safe: when the labeled-row count is below 50, the run
    logs ``source_refit_cold_start`` and exits without writing weights.

    Failure is logged but does not propagate — the ranker keeps using
    its previous (or seeded ``1.0``) weights.
    """
    try:
        from src.ranker.source_refit import run_weekly_refit

        summary = await run_weekly_refit()
    except Exception as e:
        _log.warning("source_refit_cron_failed", err=str(e))
        return
    await q.publish(
        Streams.ALERTS,
        {"kind": "source_refit_done", **{k: v for k, v in summary.items() if k != "weights"}},
    )


async def main() -> None:
    await init_pool()
    q = await RedisQ.connect()

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(emit_for_active_sources, "interval", minutes=1, args=[q], id="emit_sources")
    # Seeded with DB defaults (hour=2, minute=30 UTC). reload_digest_schedule
    # polls users.digest_*_utc every 60s and reschedules when the user changes
    # it via /digest schedule.
    scheduler.add_job(emit_daily_digest, "cron", hour=2, minute=30, args=[q], id="emit_digest_utc")
    scheduler.add_job(emit_apply_rate_nudge, "cron", hour=15, minute=30, args=[q], id="nudge_21_ist")
    scheduler.add_job(
        reload_digest_schedule,
        "interval",
        minutes=1,
        args=[scheduler],
        id="reload_digest_schedule",
    )
    # Phase 2.3 — daily follow-up scan at 13:00 Asia/Kolkata. CronTrigger
    # carries an explicit timezone so the scheduler's UTC default doesn't
    # drift the send window after DST changes (India doesn't observe DST,
    # but the explicit binding keeps the contract clear).
    scheduler.add_job(
        emit_daily_followup_scan,
        CronTrigger(hour=13, minute=0, timezone="Asia/Kolkata"),
        args=[q],
        id="daily_followup_scan",
    )
    # Phase 2.2 — weekly variant weight refit. Sunday 02:00 IST is the
    # quietest window (post-Saturday digest, pre-Sunday digest). Explicit
    # IST timezone keeps the column update landing at the same wall-clock
    # time each week regardless of UTC drift.
    scheduler.add_job(
        weekly_variant_refit,
        CronTrigger(day_of_week="sun", hour=2, minute=0, timezone="Asia/Kolkata"),
        args=[q],
        id="weekly_variant_refit",
    )
    # Phase 2.4 — weekly source response-rate refit. Sunday 03:00 IST,
    # one hour after the variant refit so it sees up-to-date weight rows
    # if the two refit modules ever share signals. Independent transactions
    # today; the scheduling offset is defensive future-proofing.
    scheduler.add_job(
        weekly_source_refit,
        CronTrigger(day_of_week="sun", hour=3, minute=0, timezone="Asia/Kolkata"),
        args=[q],
        id="weekly_source_refit",
    )
    # Phase 5.3 — nightly global ranker weights refit at 02:30 IST. Lands
    # 30 minutes after the digest cron's earliest tick and well before
    # the Sunday source_refit, so the source refit can read the most
    # recent global weights cached in formula._fit_cache.
    # Cold-start safe + flag-free (no toggle) — the scheduler simply runs
    # nightly, the handler decides whether to fit or insert a cold_start
    # audit row.
    scheduler.add_job(
        nightly_global_ranker_refit,
        CronTrigger(hour=2, minute=30, timezone="Asia/Kolkata"),
        args=[q],
        id="nightly_global_ranker_refit",
        max_instances=1,
        coalesce=True,
    )
    # Phase 3.2 — Sunday 04:00 IST. Lands one hour after weekly_source_refit
    # so the discovery run sees freshly-updated source weights. Gated by
    # settings.mp_dark_source_discovery_enabled inside the handler — when
    # off, the tick returns immediately without touching the LLM budget.
    scheduler.add_job(
        weekly_dark_source_discovery,
        CronTrigger(day_of_week="sun", hour=4, minute=0, timezone="Asia/Kolkata"),
        args=[q],
        id="weekly_dark_source_discovery",
    )
    # Phase 4 v2 — daily auto-apply cron at 08:30 IST. Lands AFTER the
    # OSS funnel scan (08:00) so OSS opps captured this morning are
    # eligible candidates, and AFTER the digest cron (~02:30 UTC = 08:00
    # IST) so the user sees today's batch before the auto-apply fires.
    # Cap enforced inside the handler — overshooting it logs but doesn't
    # fire more than `max_per_day - today_count` applies.
    scheduler.add_job(
        emit_daily_auto_apply,
        CronTrigger(hour=8, minute=30, timezone="Asia/Kolkata"),
        args=[q],
        id="daily_auto_apply",
        max_instances=1,
        coalesce=True,
    )
    # Phase 3.4 — daily OSS contribution funnel scan at 08:00 IST.
    # Explicit IST timezone matches the digest + follow-up crons so
    # the entire user-facing day starts on a stable wall-clock. Lands
    # before the apply-rate nudge (21:00 IST) so any new "good first
    # issue" pickups have a chance to surface in the day's digest.
    scheduler.add_job(
        emit_daily_oss_funnel_scan,
        CronTrigger(hour=8, minute=0, timezone="Asia/Kolkata"),
        args=[q],
        id="daily_oss_funnel_scan",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _log.info("scheduler_started", now=datetime.now(UTC).isoformat())

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

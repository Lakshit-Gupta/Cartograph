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
            rec = await conn.fetchrow(
                "SELECT digest_hour_utc, digest_minute_utc FROM users WHERE id = 1"
            )
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
        rec = await conn.fetchrow(
            "SELECT COUNT(*) AS n FROM applications WHERE sent_at::date = CURRENT_DATE"
        )
    sent = int(rec["n"] if rec else 0)
    if sent < 5:
        await q.publish(
            Streams.ALERTS,
            {"kind": "apply_rate_under_target_9pm", "sent": sent, "target": 5},
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
        reload_digest_schedule, "interval", minutes=1, args=[scheduler], id="reload_digest_schedule",
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

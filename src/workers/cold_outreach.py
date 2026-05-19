"""Cold-outreach worker — daily 10:00 IST cron + idle loop.

Boot behaviour:
  - Init DB pool + Redis publisher.
  - Schedule one cron job: drain the cold-outreach queue every day at
    10:00 IST (= 04:30 UTC).
  - Idle on a signal-aware Event until SIGINT/SIGTERM.

Refusal contract:
  - If `cold_outreach_enabled=False` (the default), `run_daily_cycle`
    short-circuits with `cold_outreach_disabled` and emits zero LLM calls.
  - The cap module is the second line of defense (belt + suspenders).

The worker NEVER consumes a Redis stream. Cold outreach is a daily push,
not an event-driven flow — so APScheduler is the right driver.
"""

from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.application.cold_outreach.sender import run_one_cycle
from src.common.db import close_pool, init_pool
from src.common.logger import configure_logging, get_logger
from src.common.queue import RedisQ
from src.common.secrets import get_settings

configure_logging("cold_outreach")
_log = get_logger(__name__)


# 04:30 UTC == 10:00 IST. Hardcoded so a wall-clock shift cannot push a
# burst send outside business hours. Re-tune via cron re-add after Phase 4.
_CRON_HOUR_UTC = 4
_CRON_MINUTE_UTC = 30


async def run_daily_cycle(q: RedisQ, user_id: int = 1) -> None:
    """Drain up to `cold_outreach_daily_cap` sends in one tick.

    Each iteration calls `run_one_cycle` which internally enforces the
    cap; once `cap_daily_cap_reached` comes back we stop. We also break
    early on `no_target_company_eligible` / `feature_flag_off` so a long
    sleep loop doesn't burn LLM budget.
    """
    s = get_settings()
    if not s.cold_outreach_enabled:
        _log.info("cold_outreach_disabled")
        return

    max_attempts = max(1, int(s.cold_outreach_daily_cap)) + 5  # small overhead
    sent = 0
    for i in range(max_attempts):
        outcome = await run_one_cycle(user_id=user_id, q=q)
        _log.info(
            "cold_outreach_attempt",
            attempt=i + 1,
            sent=outcome.sent,
            reason=outcome.reason,
            target_company_id=outcome.target_company_id,
            recipient_hash=outcome.recipient_hash,
        )
        if outcome.sent:
            sent += 1
            continue
        if outcome.reason in {
            "feature_flag_off",
            "no_target_company_eligible",
            "cap_feature_flag_off",
            "cap_daily_cap_reached",
        }:
            break
        # `draft_failed`, `no_contact_resolved`, `resend_failed`,
        # `cap_recipient_recent_14d`, `cap_subject_recent_30d` —
        # try the next target_company.
    _log.info("cold_outreach_cycle_done", sent=sent)


async def main() -> None:
    await init_pool()
    q = await RedisQ.connect()

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        run_daily_cycle,
        CronTrigger(hour=_CRON_HOUR_UTC, minute=_CRON_MINUTE_UTC),
        args=[q],
        id="cold_outreach_daily",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()

    _log.info(
        "cold_outreach_started",
        now=datetime.now(UTC).isoformat(),
        enabled=get_settings().cold_outreach_enabled,
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

"""Dark-source discovery worker — invoked by the weekly cron.

Reads `settings.mp_dark_source_discovery_enabled`. When False, logs and exits
(stays out of the cron handler's hot path). When True, opens a DB pool, runs
`pipeline.run_discovery_pipeline()`, and emits an ALERTS event so the user
sees the run in #🔔-alerts.

Can also be invoked manually for smoke-testing:
    MP_DARK_SOURCE_DISCOVERY_ENABLED=true python -m src.workers.dark_source_discovery
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict

from src.common.db import close_pool, init_pool
from src.common.logger import configure_logging, get_logger
from src.common.queue import RedisQ, Streams
from src.common.secrets import get_settings
from src.sources.discovery.pipeline import run_discovery_pipeline

configure_logging("dark_source_discovery")
_log = get_logger(__name__)


async def run_once(*, publish_alert: bool = True) -> dict:
    """One-shot run. Called by the scheduler cron AND CLI invocation.

    Returns a dict-serialised PipelineRunStats so callers can log / assert.
    """
    settings = get_settings()
    if not settings.mp_dark_source_discovery_enabled:
        _log.info("dark_source_discovery_disabled_flag_off")
        return {"status": "disabled"}

    _log.info("dark_source_discovery_start", llm_cap=settings.dark_source_daily_llm_cap)
    stats = await run_discovery_pipeline()

    payload = {
        "started_at": stats.started_at,
        "finished_at": stats.finished_at,
        "strategies": [asdict(s) for s in stats.strategy_stats],
        "total_llm_calls": stats.total_llm_calls,
        "total_auto_promoted": stats.total_auto_promoted,
        "total_pending": stats.total_pending,
    }

    if publish_alert:
        try:
            q = await RedisQ.connect()
            await q.publish(
                Streams.ALERTS,
                {"kind": "dark_source_discovery_done", **payload},
            )
        except Exception as e:
            _log.warning("alert_publish_failed", err=str(e))

    return payload


async def main() -> None:
    await init_pool()
    try:
        result = await run_once(publish_alert=True)
        _log.info("dark_source_discovery_result", **{k: v for k, v in result.items() if k != "strategies"})
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

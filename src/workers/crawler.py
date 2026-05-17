"""Crawler worker — consumes Streams.FETCH, dispatches through tier chain, emits Streams.EXTRACT."""
from __future__ import annotations

import asyncio
import signal

from src.common.db import close_pool, init_pool
from src.common.logger import configure_logging, get_logger
from src.common.queue import Groups, RedisQ, Streams
from src.common.types import FetchResult, FetchTask
from src.fetchers.base import FetchRequest
from src.fetchers.dispatcher import TierDispatcher

configure_logging("crawler")
_log = get_logger(__name__)


async def _process(q: RedisQ, dispatcher: TierDispatcher, fields: dict) -> None:
    task = FetchTask.model_validate(fields)
    req = FetchRequest(
        source_id=task.source_id,
        source_slug=task.source_slug,
        url=task.url,
    )
    resp = await dispatcher.fetch(req, task.tier_chain)
    result = FetchResult(
        source_id=task.source_id,
        source_slug=task.source_slug,
        url=task.url,
        http_status=resp.status,
        content=resp.body,
        content_type=resp.content_type,
        tier_used=resp.tier,
        correlation_id=task.correlation_id,
        error=resp.error,
    )
    await q.publish(Streams.EXTRACT, result.model_dump(mode="json"))


async def main() -> None:
    await init_pool()
    q = await RedisQ.connect()
    dispatcher = TierDispatcher()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    _log.info("crawler_started")
    async for msg in q.consume(Streams.FETCH, Groups.CRAWLERS):
        if stop.is_set():
            break
        try:
            await _process(q, dispatcher, msg.fields)
        except Exception as e:
            _log.exception("crawler_process_failed", err=str(e))
            await q.dlq(Streams.FETCH, msg.msg_id, msg.fields, str(e))
        await q.ack(Streams.FETCH, Groups.CRAWLERS, msg.msg_id)

    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

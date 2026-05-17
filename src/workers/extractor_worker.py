"""Consumes Streams.EXTRACT → runs extractor cascade → writes opportunities → emits Streams.RANK."""
from __future__ import annotations

import asyncio
import signal

from src.common.db import acquire, close_pool, init_pool
from src.common.logger import configure_logging, get_logger
from src.common.metrics import extract_selector_miss_total
from src.common.queue import Groups, RedisQ, Streams
from src.common.types import FetchResult, Opportunity
from src.extractors.base import ExtractInput
from src.extractors.persist import persist_and_publish
from src.extractors.tier0_regex import Tier0Regex
from src.extractors.tier1_selectors import get as get_t1
from src.extractors.tier2_llm import Tier2LLM

configure_logging("extractor")
_log = get_logger(__name__)


T0 = Tier0Regex()
T2 = Tier2LLM()


async def _cascade(inp: ExtractInput, strategy: str | None) -> list[Opportunity]:
    # Try tier-1 by strategy first (most accurate when available)
    t1 = get_t1(strategy) if strategy else None
    if t1 is not None:
        out = await t1(inp)
        if out.opps:
            return out.opps
        extract_selector_miss_total.labels(source=inp.source_slug).inc()
    # Tier-0 regex
    out0 = await T0.extract(inp)
    if out0.opps and out0.confidence >= 0.55:
        return out0.opps
    # Tier-2 LLM fallback
    out2 = await T2.extract(inp)
    return out2.opps


async def _resolve_strategy(source_id: int) -> str | None:
    async with acquire() as conn:
        r = await conn.fetchrow(
            "SELECT crawler_strategy FROM sources WHERE id = $1", source_id
        )
    return r["crawler_strategy"] if r else None


async def main() -> None:
    await init_pool()
    q = await RedisQ.connect()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    _log.info("extractor_started")
    async for msg in q.consume(Streams.EXTRACT, Groups.EXTRACTORS):
        if stop.is_set():
            break
        try:
            result = FetchResult.model_validate(msg.fields)
            if not result.content or result.http_status >= 400:
                await q.ack(Streams.EXTRACT, Groups.EXTRACTORS, msg.msg_id)
                continue
            strategy = await _resolve_strategy(result.source_id)
            inp = ExtractInput(
                source_id=result.source_id,
                source_slug=result.source_slug,
                url=result.url,
                content=result.content,
                content_type=result.content_type,
            )
            opps = await _cascade(inp, strategy)
            for opp in opps:
                await persist_and_publish(q, opp)
        except Exception as e:
            _log.exception("extractor_failed", err=str(e))
            await q.dlq(Streams.EXTRACT, msg.msg_id, msg.fields, str(e))
        await q.ack(Streams.EXTRACT, Groups.EXTRACTORS, msg.msg_id)

    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

"""gmail-watcher container entrypoint — runs personal + worker mailboxes concurrently."""
from __future__ import annotations

import asyncio
import signal

from src.common.db import acquire, close_pool, init_pool
from src.common.logger import configure_logging, get_logger
from src.common.queue import RedisQ
from src.extractors.persist import persist_and_publish

configure_logging("gmail")
_log = get_logger(__name__)


async def _resolve_upwork_source_id() -> int | None:
    async with acquire() as conn:
        rec = await conn.fetchrow("SELECT id FROM sources WHERE slug = 'fl_upwork_email'")
    return int(rec["id"]) if rec else None


async def main() -> None:
    await init_pool()
    q = await RedisQ.connect()

    from src.gmail_watcher.classifier import classify
    from src.gmail_watcher.imap import connect_personal, connect_worker, watch_mailbox
    from src.gmail_watcher.state_writer import handle_classification
    from src.gmail_watcher.upwork_parser import parse_upwork_digest

    upwork_source_id = await _resolve_upwork_source_id()

    async def on_personal(msg) -> None:
        try:
            classification = await classify(msg)
            await handle_classification(msg, classification)
        except Exception as e:
            _log.exception("personal_handler_failed", err=str(e))

    async def on_worker(msg) -> None:
        try:
            from_header = (msg.get("From") or "").lower()
            if "upwork.com" in from_header and upwork_source_id is not None:
                opps = parse_upwork_digest(msg, source_id=upwork_source_id)
                for opp in opps:
                    # Single write path — persists into opportunities + publishes
                    # opportunity_id onto Streams.RANK. Same contract as extractor.
                    await persist_and_publish(q, opp)
            else:
                # Treat non-Upwork worker-inbox mail like personal mail
                classification = await classify(msg)
                await handle_classification(msg, classification)
        except Exception as e:
            _log.exception("worker_handler_failed", err=str(e))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    _log.info("gmail_worker_started")
    tasks = [
        asyncio.create_task(watch_mailbox(connect_personal, on_personal)),
        asyncio.create_task(watch_mailbox(connect_worker, on_worker)),
    ]
    await stop.wait()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

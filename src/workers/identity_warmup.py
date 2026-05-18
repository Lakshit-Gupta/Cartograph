"""Identity warmup worker — paced low-volume browsing on platforms to age sessions.

For Phase 1 we keep this skeletal: it picks an identity, simulates 4-8 page views
via curl_cffi with the identity's cookies, and bumps warmup_score on success.
Real workflow is Day -3 → Day 0 with the user, this worker takes over after.
"""

from __future__ import annotations

import asyncio
import random
import signal

from src.common.db import acquire, close_pool, init_pool
from src.common.identity_vault import checkout, release
from src.common.logger import configure_logging, get_logger
from src.fetchers.base import FetchRequest
from src.fetchers.http import HttpFetcher

configure_logging("identity_warmup")
_log = get_logger(__name__)


_PLATFORM_PROBES: dict[str, list[str]] = {
    "internshala": ["https://internshala.com/internships", "https://internshala.com/jobs"],
    "cuvette": ["https://cuvette.tech/", "https://cuvette.tech/internships"],
    "unstop": ["https://unstop.com/internships", "https://unstop.com/jobs"],
    "contra": ["https://contra.com/", "https://contra.com/opportunities"],
    "wellfound": ["https://wellfound.com/jobs", "https://wellfound.com/discover"],
    "reddit": ["https://www.reddit.com/r/forhire/new"],
}


async def warmup_one(platform: str) -> None:
    lease = await checkout(platform=platform, worker_id="identity_warmup")
    if lease is None:
        return
    fetcher = HttpFetcher()
    probes = _PLATFORM_PROBES.get(platform, [])
    random.shuffle(probes)
    try:
        for url in probes[: random.randint(4, 8)]:
            req = FetchRequest(source_id=0, source_slug=f"warmup_{platform}", url=url)
            await fetcher.fetch(req)
            await asyncio.sleep(random.uniform(8, 25))
        async with acquire() as conn:
            await conn.execute(
                """
                UPDATE identities
                SET warmup_score = LEAST(warmup_score + 0.1, 1.0),
                    warmup_completed = (warmup_score + 0.1) >= 1.0,
                    last_used_at = NOW()
                WHERE id = $1
                """,
                lease.identity_id,
            )
    finally:
        await release(lease.lease_id)


async def main() -> None:
    await init_pool()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    _log.info("identity_warmup_started")
    while not stop.is_set():
        for platform in list(_PLATFORM_PROBES.keys()):
            if stop.is_set():
                break
            try:
                await warmup_one(platform)
            except Exception as e:
                _log.warning("warmup_failed", platform=platform, err=str(e))
        try:
            await asyncio.wait_for(stop.wait(), timeout=3600)  # one cycle / hr
        except TimeoutError:
            continue

    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

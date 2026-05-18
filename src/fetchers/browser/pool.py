"""Browser pool — max 3 concurrent Camoufox sessions per worker.

Each session is recycled every N pages (see lifecycle.py) so RAM doesn't
leak. mem_limit 1G per container (see compose.yaml).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from dataclasses import dataclass

from camoufox.async_api import AsyncCamoufox

from src.fetchers.browser.lifecycle import PageCounter, should_recycle


@dataclass(slots=True)
class _Lease:
    browser: AsyncCamoufox
    counter: PageCounter


class BrowserPool:
    def __init__(self, max_size: int = 3, recycle_after_pages: int = 30) -> None:
        self._sem = asyncio.Semaphore(max_size)
        self._max_size = max_size
        self._recycle_after = recycle_after_pages
        self._lock = asyncio.Lock()
        self._slots: list[tuple[AsyncCamoufox, PageCounter] | None] = [None] * max_size

    async def _spawn(self) -> AsyncCamoufox:
        # camoufox launches its own Firefox process; humanize=True enables timing jitter
        browser = AsyncCamoufox(humanize=True, headless="virtual")
        await browser.__aenter__()
        return browser

    @contextlib.asynccontextmanager
    async def lease(self) -> AsyncGenerator[_Lease, None]:
        await self._sem.acquire()
        slot_idx = -1
        try:
            async with self._lock:
                for i, slot in enumerate(self._slots):
                    if slot is None:
                        slot_idx = i
                        break
                if slot_idx == -1:
                    # all slots full but sem said yes — should not happen, force first
                    slot_idx = 0
                browser, counter = self._slots[slot_idx] or (None, None)
                if browser is None or counter is None or should_recycle(counter, self._recycle_after):
                    if browser is not None:
                        with contextlib.suppress(Exception):
                            await browser.__aexit__(None, None, None)
                    browser = await self._spawn()
                    counter = PageCounter()
                self._slots[slot_idx] = (browser, counter)
            counter.bump()
            yield _Lease(browser=browser, counter=counter)
        finally:
            self._sem.release()

    async def close_all(self) -> None:
        for i, slot in enumerate(self._slots):
            if slot is None:
                continue
            browser, _ = slot
            with contextlib.suppress(Exception):
                await browser.__aexit__(None, None, None)
            self._slots[i] = None

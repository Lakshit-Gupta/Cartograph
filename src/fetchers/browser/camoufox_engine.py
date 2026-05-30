"""Camoufox-backed `BrowserEngine` implementation (Phase 1, the only impl).

Wraps `camoufox.async_api.AsyncCamoufox` with the same launch + per-context
cookie-jar pattern used by `src/fetchers/browser/camoufox.py` and the
apply-browser worker: ONE Firefox process held alive across many contexts,
each `open_context` minting a fresh isolated `BrowserContext` so identity
cookie jars never cross-contaminate.

Adds the lifecycle the discovery worker needs on top of the fetcher's pool:
`restart()` (drop the browser every N cycles to fight camoufox memory drift)
and `is_alive()` (probe before each combo, restart on IPC death).

camoufox is imported lazily inside the methods so importing this module costs
nothing at scaffold time and does not require camoufox on hosts that never
launch a browser (e.g. the Pi). Cookies arrive already in Playwright shape —
the worker converts the identity dict → list before calling — so they are
passed straight to `add_cookies` with NO re-conversion here.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from src.common.logger import get_logger

_log = get_logger(__name__)


class CamoufoxEngine:
    """`BrowserEngine` impl backed by a single long-lived `AsyncCamoufox`.

    The browser launches lazily on the first `open_context` (or an explicit
    `start()`) and stays up until `restart()` / `shutdown()`. `restart_after_cycles`
    is advisory metadata the worker reads to decide when to call `restart()`;
    the engine itself does not count cycles.
    """

    def __init__(self, *, headless: bool = True, restart_after_cycles: int = 10) -> None:
        self._headless = headless
        self.restart_after_cycles = restart_after_cycles
        # `Any` because the camoufox type is only importable lazily; keeping the
        # annotation loose avoids a module-load import of camoufox.
        self._browser: Any | None = None

    async def start(self) -> None:
        """Launch the underlying camoufox browser if it is not already up.

        Mirrors `BrowserPool._spawn`: instantiate `AsyncCamoufox` then drive its
        async context-manager `__aenter__` by hand so we own the lifecycle.
        `headless="virtual"` runs Firefox under Xvfb (the ThinkPad/Pi pattern);
        `humanize=True` enables camoufox's built-in timing jitter.
        """
        if self._browser is not None:
            return

        # Lazy import — see module docstring. Importing this module must not
        # require camoufox to be installed.
        from camoufox.async_api import AsyncCamoufox

        headless_mode: bool | str = "virtual" if self._headless else False
        browser = AsyncCamoufox(humanize=True, headless=headless_mode)
        await browser.__aenter__()
        self._browser = browser
        _log.info("camoufox_engine_started", headless=headless_mode)

    def is_alive(self) -> bool:
        """True once the browser handle is initialised and not torn down.

        Tracks our own handle rather than probing the IPC channel — the worker
        treats a launch failure or a `restart()`/`shutdown()` as not-alive and
        relaunches on the next `open_context`.
        """
        return self._browser is not None

    def open_context(self, *, cookies: list[dict], ua: str | None = None, viewport: dict | None = None) -> AbstractAsyncContextManager:
        """Async CM yielding a fresh `BrowserContext`; closes it (not the
        browser) on exit. See `BrowserEngine.open_context` for the contract.
        """
        return self._context_cm(cookies=cookies, ua=ua, viewport=viewport)

    @asynccontextmanager
    async def _context_cm(self, *, cookies: list[dict], ua: str | None, viewport: dict | None) -> AsyncIterator[Any]:
        await self.start()
        browser = self._browser
        if browser is None:  # pragma: no cover - start() either set it or raised
            raise RuntimeError("camoufox browser failed to launch")

        # Only pass user_agent when ua is truthy and viewport when provided —
        # passing empty/None would override camoufox's stealth defaults. Mirror
        # camoufox.py's CamoufoxFetcher.fetch context-kwarg gating.
        context_kwargs: dict[str, Any] = {}
        if ua:
            context_kwargs["user_agent"] = ua
        if viewport is not None:
            context_kwargs["viewport"] = viewport

        context = await browser.new_context(**context_kwargs)
        try:
            # `_cookies_passthrough`: cookies are already Playwright-shaped
            # (worker converted dict → list upstream). Do NOT re-convert.
            if cookies:
                await context.add_cookies(cookies)
            yield context
        finally:
            with contextlib.suppress(Exception):
                await context.close()

    async def restart(self) -> None:
        """Tear the browser down and clear the handle so the next `open_context`
        relaunches a fresh process. Swallows teardown errors — the goal is a
        clean handle, and a half-dead browser must not block the relaunch.
        """
        await self._teardown()
        _log.info("camoufox_engine_restarted")

    async def shutdown(self) -> None:
        """Close the browser and release resources. Idempotent."""
        await self._teardown()
        _log.info("camoufox_engine_shutdown")

    async def _teardown(self) -> None:
        browser = self._browser
        self._browser = None
        if browser is not None:
            with contextlib.suppress(Exception):
                await browser.__aexit__(None, None, None)

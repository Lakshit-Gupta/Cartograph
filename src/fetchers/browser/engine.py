"""Swappable browser-engine Protocol for the Internshala discovery worker.

All browser interaction in the discovery path routes through `BrowserEngine`
so a future engine (Nodriver, Patchright) can replace camoufox by config when
the deferred "browser engine refresh" triggers — no caller change required.
`CamoufoxEngine` (`camoufox_engine.py`) is the only Phase 1 implementation.

The Protocol is `@runtime_checkable` so the worker (and tests) can assert
`isinstance(engine, BrowserEngine)` for a duck-typed implementation. Only the
presence of the four members is checked at runtime — `runtime_checkable` does
not verify signatures, so conformance of the call contract is pinned by the
test suite rather than `isinstance` alone.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol, runtime_checkable


@runtime_checkable
class BrowserEngine(Protocol):
    """A long-lived browser process that hands out per-call contexts.

    The engine owns a single underlying browser; each `open_context` yields an
    isolated `BrowserContext` (own cookie jar) so back-to-back callers never
    cross-contaminate identity state. The browser persists across contexts and
    is only torn down by `restart` / `shutdown`.
    """

    def open_context(self, *, cookies: list[dict], ua: str | None = None, viewport: dict | None = None) -> AbstractAsyncContextManager:
        """Return an async context manager yielding a Playwright `BrowserContext`
        (camoufox-backed). Caller does `ctx.new_page()`.

        `cookies` are already in the Playwright cookie-list shape (list of dicts
        carrying `name`/`value`/`url`, or `name`/`value`/`domain`/`path`) — the
        worker converts the identity dict → list before calling, so the engine
        passes them straight to `add_cookies` without re-conversion. `ua` sets
        the context user-agent only when truthy; `viewport` is applied when
        provided. On `__aexit__` the context is closed (the browser is not).
        """
        ...

    def is_alive(self) -> bool:
        """True when the underlying browser handle is initialised and not closed."""
        ...

    async def restart(self) -> None:
        """Tear the browser down and clear the handle so the next `open_context`
        relaunches. Used by the worker every N cycles to fight camoufox memory
        drift, and on any IPC failure."""
        ...

    async def shutdown(self) -> None:
        """Close the browser and release all resources. Idempotent."""
        ...

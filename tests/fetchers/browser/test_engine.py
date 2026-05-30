"""Tests for the `BrowserEngine` Protocol + `CamoufoxEngine` implementation.

None of these launch a real browser. The Protocol conformance + async-CM
contract are exercised through a minimal fake engine; `CamoufoxEngine`'s
launch / restart / shutdown state transitions are driven through a fake
`AsyncCamoufox` injected into `sys.modules` so camoufox itself never spawns
Firefox.

The "camoufox imported lazily" guarantee is checked in a clean subprocess so a
camoufox import from any other test in the session cannot mask a regression.
"""

from __future__ import annotations

import subprocess
import sys
import types
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import pytest

from src.fetchers.browser.camoufox_engine import CamoufoxEngine
from src.fetchers.browser.engine import BrowserEngine

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeContext:
    """Stand-in for a Playwright BrowserContext."""

    def __init__(self) -> None:
        self.added_cookies: list[dict] | None = None
        self.closed = False

    async def add_cookies(self, cookies: list[dict]) -> None:
        self.added_cookies = cookies

    async def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    """Stand-in for the Playwright `Browser` that `AsyncCamoufox.__aenter__`
    RETURNS. Critically a *separate* object from the CM — `new_context` lives
    here, NOT on the CM — so a regression that stores the CM in `self._browser`
    (the real bug this mirrors) raises AttributeError under test, just as it
    does against real camoufox."""

    def __init__(self) -> None:
        self.contexts: list[_FakeContext] = []
        self.last_context_kwargs: dict | None = None

    async def new_context(self, **kwargs: object) -> _FakeContext:
        self.last_context_kwargs = kwargs
        ctx = _FakeContext()
        self.contexts.append(ctx)
        return ctx


class _FakeAsyncCamoufox:
    """Fake `AsyncCamoufox` context-manager — records lifecycle without
    launching Firefox.

    Instances are tracked on the class so tests can assert how many browser
    processes were spawned / entered / exited across a restart. Mirrors real
    camoufox: `__aenter__` returns a SEPARATE `_FakeBrowser`, and this CM has
    NO `new_context` of its own. `contexts` / `last_context_kwargs` are proxied
    to the yielded browser so existing assertions read through unchanged.
    """

    instances: list[_FakeAsyncCamoufox] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.entered = False
        self.exited = False
        self.browser = _FakeBrowser()
        _FakeAsyncCamoufox.instances.append(self)

    async def __aenter__(self) -> _FakeBrowser:
        self.entered = True
        return self.browser

    async def __aexit__(self, *exc: object) -> None:
        self.exited = True

    @property
    def contexts(self) -> list[_FakeContext]:
        return self.browser.contexts

    @property
    def last_context_kwargs(self) -> dict | None:
        return self.browser.last_context_kwargs


class _FakeEngine:
    """Minimal `BrowserEngine` duck-type — the 4 members, no browser."""

    def __init__(self) -> None:
        self.alive = False
        self.context = _FakeContext()
        self.last_open_kwargs: dict | None = None

    def open_context(self, *, cookies: list[dict], ua: str | None = None, viewport: dict | None = None) -> AbstractAsyncContextManager:
        self.last_open_kwargs = {"cookies": cookies, "ua": ua, "viewport": viewport}
        return self._cm()

    @asynccontextmanager
    async def _cm(self) -> AsyncIterator[_FakeContext]:
        self.alive = True
        try:
            yield self.context
        finally:
            self.context.closed = True

    def is_alive(self) -> bool:
        return self.alive

    async def restart(self) -> None:
        self.alive = False

    async def shutdown(self) -> None:
        self.alive = False


@pytest.fixture
def fake_camoufox(monkeypatch: pytest.MonkeyPatch) -> type[_FakeAsyncCamoufox]:
    """Inject a fake `camoufox.async_api.AsyncCamoufox` so `CamoufoxEngine`'s
    lazy `from camoufox.async_api import AsyncCamoufox` resolves to the fake.
    """
    _FakeAsyncCamoufox.instances.clear()
    module = types.ModuleType("camoufox.async_api")
    module.AsyncCamoufox = _FakeAsyncCamoufox  # type: ignore[attr-defined]
    # Ensure the parent package exists so the submodule import resolves.
    pkg = sys.modules.get("camoufox")
    if pkg is None:
        pkg = types.ModuleType("camoufox")
        monkeypatch.setitem(sys.modules, "camoufox", pkg)
    monkeypatch.setitem(sys.modules, "camoufox.async_api", module)
    return _FakeAsyncCamoufox


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_fake_engine_satisfies_protocol() -> None:
    """A minimal duck-type with the 4 members passes the runtime_checkable
    isinstance gate the worker uses."""
    assert isinstance(_FakeEngine(), BrowserEngine)


@pytest.mark.smoke
def test_camoufox_engine_satisfies_protocol() -> None:
    """The real impl conforms without launching anything (isinstance only
    inspects member presence)."""
    assert isinstance(CamoufoxEngine(), BrowserEngine)


@pytest.mark.smoke
def test_incomplete_engine_fails_protocol() -> None:
    """A type missing a member must NOT pass the gate, or the contract is moot."""

    class _Missing:
        def open_context(self, *, cookies, ua=None, viewport=None):  # type: ignore[no-untyped-def]
            return None

        def is_alive(self) -> bool:
            return False

        # restart / shutdown absent

    assert not isinstance(_Missing(), BrowserEngine)


# ---------------------------------------------------------------------------
# Fake-engine open_context async-CM contract
# ---------------------------------------------------------------------------


@pytest.mark.smoke
async def test_fake_engine_open_context_yields_and_closes() -> None:
    engine = _FakeEngine()
    cookies = [{"name": "session", "value": "abc", "url": "https://internshala.com"}]

    async with engine.open_context(cookies=cookies, ua="UA/1.0") as ctx:
        assert ctx is engine.context
        assert ctx.closed is False
        assert engine.is_alive() is True

    # Exit closed the yielded context.
    assert engine.context.closed is True
    assert engine.last_open_kwargs == {"cookies": cookies, "ua": "UA/1.0", "viewport": None}


# ---------------------------------------------------------------------------
# Lazy import guarantee
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_camoufox_imported_lazily_not_at_module_load() -> None:
    """Importing `camoufox_engine` must NOT drag camoufox into sys.modules.

    Run in a clean subprocess: a camoufox import from any sibling test in this
    session would otherwise hide a regression where the module-level import
    creeps back in.
    """
    code = (
        "import sys\n"
        "import src.fetchers.browser.camoufox_engine  # noqa: F401\n"
        "assert 'camoufox' not in sys.modules, 'camoufox imported at module load'\n"
        "assert 'camoufox.async_api' not in sys.modules, 'camoufox.async_api imported at module load'\n"
        "print('LAZY_OK')\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "LAZY_OK" in proc.stdout


# ---------------------------------------------------------------------------
# CamoufoxEngine lifecycle / state transitions (fake browser)
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_camoufox_engine_not_alive_before_start() -> None:
    """is_alive() is False before any context is opened / start() called."""
    assert CamoufoxEngine().is_alive() is False


@pytest.mark.smoke
async def test_camoufox_engine_lazy_launch_on_open_context(fake_camoufox) -> None:
    engine = CamoufoxEngine(headless=True)
    assert engine.is_alive() is False

    cookies = [{"name": "sid", "value": "v", "url": "https://internshala.com"}]
    async with engine.open_context(cookies=cookies, ua="UA/2", viewport={"width": 1280, "height": 800}) as ctx:
        # Browser launched + entered exactly once on first open_context.
        assert len(fake_camoufox.instances) == 1
        browser = fake_camoufox.instances[0]
        assert browser.entered is True
        assert engine.is_alive() is True
        # ua + viewport forwarded to new_context.
        assert browser.last_context_kwargs == {"user_agent": "UA/2", "viewport": {"width": 1280, "height": 800}}
        # Cookies passed through verbatim (no re-conversion).
        assert ctx.added_cookies == cookies
        assert ctx.closed is False

    # Context closed on exit; browser stays up (not exited).
    assert browser.contexts[0].closed is True
    assert browser.exited is False
    assert engine.is_alive() is True

    await engine.shutdown()


@pytest.mark.smoke
async def test_camoufox_engine_skips_ua_and_viewport_when_absent(fake_camoufox) -> None:
    """No user_agent / viewport kwargs when ua falsy + viewport None — must not
    override camoufox stealth defaults."""
    engine = CamoufoxEngine()
    async with engine.open_context(cookies=[]) as ctx:
        browser = fake_camoufox.instances[0]
        assert browser.last_context_kwargs == {}
        # Empty cookie list → add_cookies never called.
        assert ctx.added_cookies is None
    await engine.shutdown()


@pytest.mark.smoke
async def test_camoufox_engine_browser_reused_across_contexts(fake_camoufox) -> None:
    """The browser persists across multiple open_context calls (one process)."""
    engine = CamoufoxEngine()
    async with engine.open_context(cookies=[]):
        pass
    async with engine.open_context(cookies=[]):
        pass
    # One browser, two contexts.
    assert len(fake_camoufox.instances) == 1
    assert len(fake_camoufox.instances[0].contexts) == 2
    await engine.shutdown()


@pytest.mark.smoke
async def test_camoufox_engine_restart_relaunches_fresh_browser(fake_camoufox) -> None:
    engine = CamoufoxEngine()
    async with engine.open_context(cookies=[]):
        pass
    first = fake_camoufox.instances[0]
    assert engine.is_alive() is True

    await engine.restart()
    # Handle cleared + old browser torn down.
    assert engine.is_alive() is False
    assert first.exited is True

    # Next open_context launches a brand-new browser process.
    async with engine.open_context(cookies=[]):
        pass
    assert len(fake_camoufox.instances) == 2
    assert fake_camoufox.instances[1] is not first
    assert engine.is_alive() is True

    await engine.shutdown()


@pytest.mark.smoke
async def test_camoufox_engine_shutdown_closes_browser_and_is_idempotent(fake_camoufox) -> None:
    engine = CamoufoxEngine()
    async with engine.open_context(cookies=[]):
        pass
    browser = fake_camoufox.instances[0]

    await engine.shutdown()
    assert browser.exited is True
    assert engine.is_alive() is False

    # Second shutdown is a no-op, does not raise.
    await engine.shutdown()
    assert engine.is_alive() is False


@pytest.mark.smoke
async def test_camoufox_engine_explicit_start_then_open(fake_camoufox) -> None:
    """start() launches eagerly; a later open_context reuses that browser."""
    engine = CamoufoxEngine()
    await engine.start()
    assert engine.is_alive() is True
    assert len(fake_camoufox.instances) == 1

    async with engine.open_context(cookies=[]):
        pass
    # Still one browser — start() did not double-launch.
    assert len(fake_camoufox.instances) == 1
    await engine.shutdown()

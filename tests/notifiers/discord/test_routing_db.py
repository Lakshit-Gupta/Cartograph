"""Phase 3 follow-on — `notification_routes` loader + cache contract tests.

Coverage:
  - `load_routes` caches by user_id with 5min TTL.
  - Cache hits skip the DB.
  - `invalidate_routes_cache(user_id)` clears one tenant; `None` clears all.
  - DB-empty path returns an empty dict (caller falls back to settings).
  - DB error path swallows the exception and returns empty dict.
  - `channel_id_for(name)` in `routing.py` returns the DB ID when present
    and falls back to `settings.discord_channel(name)` otherwise.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Fake asyncpg connection that returns a canned row list.
# ---------------------------------------------------------------------------
class _FakeConn:
    def __init__(self, rows: list[dict[str, Any]], *, raise_on_fetch: bool = False):
        self._rows = rows
        self._raise = raise_on_fetch
        self.fetch_calls = 0

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls += 1
        if self._raise:
            raise RuntimeError("simulated db failure")
        return list(self._rows)


def _patch_acquire(monkeypatch: pytest.MonkeyPatch, conn: _FakeConn) -> None:
    @asynccontextmanager
    async def fake_acquire():
        yield conn

    monkeypatch.setattr("src.notifiers.discord.routing_db.acquire", fake_acquire)


def _row(target: str, *, channel_id: int | None = 12345, route_type: str = "daily_digest", color: int | None = None) -> dict[str, Any]:
    return {
        "target": target,
        "route_type": route_type,
        "discord_channel_id": channel_id,
        "discord_thread_id": None,
        "embed_color": color,
        "enabled": True,
    }


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    """Every test gets a clean cache — there is one module-level dict."""
    from src.notifiers.discord import routing_db

    routing_db._routes_cache.clear()
    yield
    routing_db._routes_cache.clear()


# ---------------------------------------------------------------------------
# 1. Happy path — DB rows load into a {target: RouteRow} dict.
# ---------------------------------------------------------------------------
def test_load_routes_returns_target_keyed_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _FakeConn([_row("daily_digest", channel_id=111), _row("alerts", channel_id=222, route_type="alerts")])
    _patch_acquire(monkeypatch, conn)

    from src.notifiers.discord.routing_db import load_routes

    routes = asyncio.run(load_routes(user_id=1))
    assert set(routes.keys()) == {"daily_digest", "alerts"}
    assert routes["daily_digest"].discord_channel_id == 111
    assert routes["alerts"].route_type == "alerts"
    assert conn.fetch_calls == 1


# ---------------------------------------------------------------------------
# 2. Cache hit — second call inside the TTL skips the DB.
# ---------------------------------------------------------------------------
def test_cache_hit_skips_db(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _FakeConn([_row("daily_digest", channel_id=111)])
    _patch_acquire(monkeypatch, conn)

    from src.notifiers.discord.routing_db import load_routes

    asyncio.run(load_routes(user_id=1))
    asyncio.run(load_routes(user_id=1))
    assert conn.fetch_calls == 1  # second call hit the cache


# ---------------------------------------------------------------------------
# 3. Cache miss after TTL expiry — fetches again.
# ---------------------------------------------------------------------------
def test_cache_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _FakeConn([_row("daily_digest", channel_id=111)])
    _patch_acquire(monkeypatch, conn)

    from src.notifiers.discord import routing_db
    from src.notifiers.discord.routing_db import load_routes

    asyncio.run(load_routes(user_id=1))
    # Move the recorded fetch time backwards past the TTL window so the
    # next call re-fetches. Touching monotonic clock directly is awkward;
    # rewriting the cache entry is the simplest deterministic hook.
    cached, _ = routing_db._routes_cache[1]
    routing_db._routes_cache[1] = (cached, time.monotonic() - routing_db._ROUTES_CACHE_TTL_SECONDS - 1)
    asyncio.run(load_routes(user_id=1))
    assert conn.fetch_calls == 2


# ---------------------------------------------------------------------------
# 4. Invalidate for one tenant — that user re-fetches, others survive.
# ---------------------------------------------------------------------------
def test_invalidate_one_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _FakeConn([_row("daily_digest", channel_id=111)])
    _patch_acquire(monkeypatch, conn)

    from src.notifiers.discord import routing_db
    from src.notifiers.discord.routing_db import invalidate_routes_cache, load_routes

    asyncio.run(load_routes(user_id=1))
    asyncio.run(load_routes(user_id=2))
    assert conn.fetch_calls == 2

    invalidate_routes_cache(user_id=1)
    assert 1 not in routing_db._routes_cache
    assert 2 in routing_db._routes_cache

    asyncio.run(load_routes(user_id=1))
    assert conn.fetch_calls == 3  # user 1 re-fetched
    asyncio.run(load_routes(user_id=2))
    assert conn.fetch_calls == 3  # user 2 still cached


# ---------------------------------------------------------------------------
# 5. Invalidate all — both tenants re-fetch.
# ---------------------------------------------------------------------------
def test_invalidate_all_tenants(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _FakeConn([_row("daily_digest", channel_id=111)])
    _patch_acquire(monkeypatch, conn)

    from src.notifiers.discord import routing_db
    from src.notifiers.discord.routing_db import invalidate_routes_cache, load_routes

    asyncio.run(load_routes(user_id=1))
    asyncio.run(load_routes(user_id=2))
    invalidate_routes_cache()
    assert routing_db._routes_cache == {}


# ---------------------------------------------------------------------------
# 6. DB-empty fallback — caller sees empty dict (settings fallback in routing.py).
# ---------------------------------------------------------------------------
def test_db_empty_returns_empty_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _FakeConn([])
    _patch_acquire(monkeypatch, conn)

    from src.notifiers.discord.routing_db import load_routes

    routes = asyncio.run(load_routes(user_id=1))
    assert routes == {}


# ---------------------------------------------------------------------------
# 7. DB error path — exception swallowed, empty dict returned, future calls
#    still proceed (the bot must not crash on a transient DB hiccup).
# ---------------------------------------------------------------------------
def test_db_error_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _FakeConn([], raise_on_fetch=True)
    _patch_acquire(monkeypatch, conn)

    from src.notifiers.discord.routing_db import load_routes

    routes = asyncio.run(load_routes(user_id=1))
    assert routes == {}


# ---------------------------------------------------------------------------
# 8. channel_id_for — DB hit wins.
# ---------------------------------------------------------------------------
def test_channel_id_for_prefers_db(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _FakeConn([_row("daily_digest", channel_id=999_111)])
    _patch_acquire(monkeypatch, conn)

    from src.notifiers.discord.routing_db import load_routes

    asyncio.run(load_routes(user_id=1))

    from src.notifiers.discord.routing import channel_id_for

    assert channel_id_for("daily_digest") == 999_111


# ---------------------------------------------------------------------------
# 9. channel_id_for — DB row with NULL discord_channel_id falls back to settings.
# ---------------------------------------------------------------------------
def test_channel_id_for_db_null_falls_back_to_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _FakeConn([_row("daily_digest", channel_id=None)])
    _patch_acquire(monkeypatch, conn)

    from src.notifiers.discord.routing_db import load_routes

    asyncio.run(load_routes(user_id=1))

    # Stub settings to return a deterministic env-side ID.
    class _FakeSettings:
        def discord_channel(self, name: str) -> int:
            return 42 if name == "daily_digest" else 0

    monkeypatch.setattr("src.notifiers.discord.routing.get_settings", lambda: _FakeSettings())

    from src.notifiers.discord.routing import channel_id_for

    assert channel_id_for("daily_digest") == 42


# ---------------------------------------------------------------------------
# 10. channel_id_for — cold cache (DB never loaded) falls back to settings
#     without crashing. This is the Day-0 boot scenario.
# ---------------------------------------------------------------------------
def test_channel_id_for_cold_cache_uses_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeSettings:
        def discord_channel(self, name: str) -> int:
            return 7 if name == "alerts" else 0

    monkeypatch.setattr("src.notifiers.discord.routing.get_settings", lambda: _FakeSettings())

    from src.notifiers.discord.routing import channel_id_for

    assert channel_id_for("alerts") == 7
    assert channel_id_for("daily_digest") is None  # 0 → None per current contract

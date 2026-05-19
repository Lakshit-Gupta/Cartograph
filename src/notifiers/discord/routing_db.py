"""Per-tenant Discord channel routes, sourced from `notification_routes`.

Phase 3 follow-on. Hop used to look up Discord channel IDs straight out of
`get_settings().discord_channel_<name>`. That worked for the solo Phase 1/2
deployment but every multi-tenant tenant onboarded via `/jobs-onboard <token>`
needs their own channel IDs in DB without the operator re-editing SOPS and
restarting the bot.

This module owns the loader + a tiny process-local cache. The public
`channel_id_for(name)` API in `src/notifiers/discord/routing.py` reads through
this cache so existing call sites stay synchronous (they're all inside
asyncio handlers, but the read is a hot path — querying Postgres on every
embed post would be wasteful).

Cache semantics:
  * Keyed on `user_id` so a multi-tenant bot can hold routes for every active
    tenant in memory without cross-pollution.
  * TTL = 5min. Matches `src/ranker/formula._FIT_CACHE_TTL_SECONDS` — same
    "next refresh lands well inside the window even if a CLI write happens
    mid-window" reasoning.
  * `invalidate_routes_cache(user_id)` is called by `mp routes set/refresh`
    so operator edits land instantly. `None` clears every tenant (full nuke).
  * On DB error / empty result the loader returns `{}` so callers fall back
    to settings — Hop must boot fine on a fresh DB where nobody has run the
    V020 seed yet.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from src.common.db import acquire, current_tenant
from src.common.logger import get_logger

_log = get_logger(__name__)

# 5min matches `ranker/formula._FIT_CACHE_TTL_SECONDS`. A `mp routes set`
# call always triggers `invalidate_routes_cache(user_id)` so the operator
# never has to wait this out in practice; the TTL exists for the case
# where another process (e.g. a sibling worker) mutated the row.
_ROUTES_CACHE_TTL_SECONDS = 300


@dataclass(slots=True)
class RouteRow:
    """One row of `notification_routes` rendered as a typed record.

    `discord_channel_id` is the actual numeric Discord channel ID. May be
    None when the row was seeded by V020 but never overwritten via
    `mp routes set` — callers should fall back to settings in that case.
    """

    target: str
    route_type: str
    discord_channel_id: int | None
    discord_thread_id: int | None
    embed_color: int | None
    enabled: bool


# {user_id: ({target: RouteRow}, fetched_at_monotonic)}. The inner dict is
# the read shape callers want (lookup by logical channel name). Hold the
# whole dict per-user so a single refresh ages out the whole snapshot.
_routes_cache: dict[int, tuple[dict[str, RouteRow], float]] = {}


def invalidate_routes_cache(user_id: int | None = None) -> None:
    """Drop the cache for a single tenant (or every tenant if `None`).

    Called by the `mp routes` CLI after a successful UPDATE so the next
    `channel_id_for(name)` in the live bot sees the new ID without waiting
    on the 5min TTL.
    """
    if user_id is None:
        _routes_cache.clear()
        return
    _routes_cache.pop(int(user_id), None)


async def load_routes(user_id: int | None = None) -> dict[str, RouteRow]:
    """Return `{target: RouteRow}` for the given tenant, ttl-cached.

    `user_id=None` resolves the active tenant via
    `src.common.db.current_tenant()` — the Discord handler sets that on
    every interaction before calling into this module.

    On DB error or empty result, returns an empty dict. Callers in
    `routing.py` interpret that as "fall back to settings" — the bot is
    expected to boot on a brand-new database where V020 has not yet run.
    """
    if user_id is None:
        user_id = current_tenant()
    uid = int(user_id)

    cached = _routes_cache.get(uid)
    if cached is not None and (time.monotonic() - cached[1]) < _ROUTES_CACHE_TTL_SECONDS:
        return cached[0]

    rows: dict[str, RouteRow] = {}
    try:
        async with acquire() as conn:
            db_rows = await conn.fetch(
                """
                SELECT target, route_type, discord_channel_id, discord_thread_id,
                       embed_color, enabled
                  FROM notification_routes
                 WHERE user_id = $1 AND channel = 'discord'
                """,
                uid,
            )
        for r in db_rows:
            rows[r["target"]] = RouteRow(
                target=r["target"],
                route_type=str(r["route_type"]),
                discord_channel_id=r["discord_channel_id"],
                discord_thread_id=r["discord_thread_id"],
                embed_color=r["embed_color"],
                enabled=bool(r["enabled"]),
            )
    except Exception as e:
        # No DB? Pool not initialised? Fine — caller falls back to settings.
        # We log once so an actually-broken DB isn't silent, but we don't
        # raise — Hop's gateway loop must keep running.
        _log.warning("routes_db_load_failed", user_id=uid, err=str(e))
        # Cache the empty result too — saves hammering a dead DB on every
        # embed post. The TTL still ages it out so we retry eventually.

    _routes_cache[uid] = (rows, time.monotonic())
    return rows


def get_cached_routes(user_id: int | None = None) -> dict[str, RouteRow] | None:
    """Synchronous peek into the cache. Returns None on miss.

    Used by `routing.channel_id_for()` which must stay synchronous (called
    from inside async embed builders — switching to async would ripple
    through every notify_* handler). Misses fall through to settings, then
    a background refresh fills the cache for next time.
    """
    if user_id is None:
        user_id = current_tenant()
    uid = int(user_id)
    cached = _routes_cache.get(uid)
    if cached is None:
        return None
    if (time.monotonic() - cached[1]) >= _ROUTES_CACHE_TTL_SECONDS:
        return None
    return cached[0]

"""`mp routes` — inspect & override per-tenant Discord channel routes.

Phase 3 follow-on. Hop used to read every Discord channel ID straight out of
the SOPS-encrypted env (`DISCORD_CHANNEL_<NAME>`). That worked for the solo
Phase 1/2 deployment, but every additional tenant (Phase 4.2 multi-tenant)
wants their OWN channel IDs without forcing the operator to edit SOPS and
restart the bot. V020 seeds 14 logical channels per user; this CLI lets the
operator promote real Discord IDs into those rows.

Sub-commands:

  mp routes list [--user-id N]            tabulate rows for one tenant
  mp routes set <name> <id> [--user-id]   UPSERT discord_channel_id, invalidate cache
  mp routes refresh [--user-id N]         invalidate the in-process cache only

The `set` and `refresh` commands invalidate the live cache via
`routing_db.invalidate_routes_cache()` so the next embed post sees the new
ID without waiting on the 5min TTL. The CLI runs in a separate process from
the bot, so cache invalidation only affects this Python process — but the
DB write is durable, and the bot's own 5min TTL guarantees convergence in
the worst case.
"""

from __future__ import annotations

import asyncio

import click

from src.common.db import acquire, close_pool, init_pool
from src.notifiers.discord.routing_db import invalidate_routes_cache

# Listing cap — keeps terminal output scannable on multi-tenant deployments.
_LIST_LIMIT = 200


@click.group("routes")
def routes_group() -> None:
    """Per-tenant Discord channel routes (Phase 3)."""


@routes_group.command("list")
@click.option("--user-id", type=int, default=1, show_default=True)
def list_cmd(user_id: int) -> None:
    """Show every route row for one tenant."""
    asyncio.run(_list(user_id=user_id))


@routes_group.command("set")
@click.argument("name")
@click.argument("channel_id", type=int)
@click.option("--user-id", type=int, default=1, show_default=True)
@click.option(
    "--route-type",
    default=None,
    help="Override route_type when inserting a NEW row. Defaults to 'daily_digest'.",
)
def set_cmd(name: str, channel_id: int, user_id: int, route_type: str | None) -> None:
    """UPSERT the Discord channel ID for one logical channel name."""
    asyncio.run(_set(user_id=user_id, name=name, channel_id=channel_id, route_type=route_type))


@routes_group.command("refresh")
@click.option("--user-id", type=int, default=None, help="Default: clear every tenant.")
def refresh_cmd(user_id: int | None) -> None:
    """Drop the in-process routes cache so the next read re-fetches from DB."""
    invalidate_routes_cache(user_id)
    target = f"user_id={user_id}" if user_id is not None else "all tenants"
    click.echo(f"routes cache invalidated ({target})")


async def _list(*, user_id: int) -> None:
    await init_pool()
    try:
        async with acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT target, route_type, discord_channel_id, discord_thread_id,
                       embed_color, enabled
                  FROM notification_routes
                 WHERE user_id = $1 AND channel = 'discord'
                 ORDER BY route_type, target
                 LIMIT $2
                """,
                user_id,
                _LIST_LIMIT,
            )
    finally:
        await close_pool()
    if not rows:
        click.echo(f"(no rows for user_id={user_id})")
        return
    click.echo(f"{'target':<18} {'route_type':<16} {'channel_id':<22} {'color':<10} enabled")
    click.echo("-" * 78)
    for r in rows:
        cid = str(r["discord_channel_id"]) if r["discord_channel_id"] is not None else "(env fallback)"
        color = f"#{r['embed_color']:06X}" if r["embed_color"] is not None else "-"
        click.echo(f"{r['target']:<18} {r['route_type']:<16} {cid:<22} {color:<10} {r['enabled']}")


async def _set(*, user_id: int, name: str, channel_id: int, route_type: str | None) -> None:
    await init_pool()
    try:
        async with acquire() as conn:
            # UPSERT: keep existing route_type when the row already exists
            # (operator just wants to swap the ID); otherwise seed
            # 'daily_digest' as the safe default — handlers that care about
            # route_type look it up by target, not the other way around.
            existing = await conn.fetchrow(
                """
                SELECT route_type FROM notification_routes
                 WHERE user_id = $1 AND channel = 'discord' AND target = $2
                """,
                user_id,
                name,
            )
            effective_route_type = route_type or (existing["route_type"] if existing else "daily_digest")
            await conn.execute(
                """
                INSERT INTO notification_routes
                    (user_id, channel, route_type, target, discord_channel_id, enabled)
                VALUES ($1, 'discord', $2, $3, $4, TRUE)
                ON CONFLICT (user_id, channel, target) DO UPDATE
                  SET discord_channel_id = EXCLUDED.discord_channel_id,
                      enabled = TRUE
                """,
                user_id,
                effective_route_type,
                name,
                channel_id,
            )
    finally:
        await close_pool()
    invalidate_routes_cache(user_id)
    click.echo(f"set user_id={user_id} target={name} → channel_id={channel_id} (route_type={effective_route_type})")

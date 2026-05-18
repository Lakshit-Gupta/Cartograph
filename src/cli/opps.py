"""`mp opps ...` — list / show / requeue."""

from __future__ import annotations

import asyncio
from uuid import UUID

import click

from src.common.db import acquire, close_pool, init_pool


@click.group("opps")
def opps_group() -> None:
    """Inspect / manipulate opportunities."""


@opps_group.command("recent")
@click.option("--limit", default=20, type=int)
def recent(limit: int) -> None:
    asyncio.run(_recent(limit))


async def _recent(limit: int) -> None:
    await init_pool()
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT o.id, o.title, o.company, o.state, o.first_seen, s.slug,
                   COALESCE(os.score, 0) AS score
            FROM opportunities o
            JOIN sources s ON s.id = o.source_id
            LEFT JOIN opportunity_scores os ON os.opportunity_id = o.id AND os.user_id = 1
            ORDER BY o.first_seen DESC
            LIMIT $1
            """,
            limit,
        )
    for r in rows:
        click.echo(f"{r['id']}  {r['state']:10s}  {r['score']:.2f}  [{r['slug']:18s}] {r['company'] or '-':25s}  {r['title'][:80]}")
    await close_pool()


@opps_group.command("show")
@click.argument("opp_id", type=click.UUID)
def show(opp_id: UUID) -> None:
    asyncio.run(_show(opp_id))


async def _show(opp_id: UUID) -> None:
    await init_pool()
    async with acquire() as conn:
        rec = await conn.fetchrow(
            """
            SELECT o.*, s.slug AS source_slug,
                   COALESCE(os.score, 0) AS score, os.score_components
            FROM opportunities o
            JOIN sources s ON s.id = o.source_id
            LEFT JOIN opportunity_scores os ON os.opportunity_id = o.id AND os.user_id = 1
            WHERE o.id = $1
            """,
            opp_id,
        )
    if not rec:
        click.echo("not found")
        return
    for k, v in dict(rec).items():
        click.echo(f"{k:25s} {v}")
    await close_pool()

"""`mp sources ...` — list / pause / resume / add."""

from __future__ import annotations

import asyncio

import click

from src.common.db import acquire, close_pool, init_pool


@click.group("sources")
def sources_group() -> None:
    """Manage crawl sources."""


@sources_group.command("list")
def list_cmd() -> None:
    asyncio.run(_list())


async def _list() -> None:
    await init_pool()
    async with acquire() as conn:
        rows = await conn.fetch(
            "SELECT slug, name, category, status, priority, last_successful_crawl_at FROM sources ORDER BY priority DESC, slug"
        )
    for r in rows:
        click.echo(f"{r['slug']:30s} {r['category']:10s} {r['status']:10s} prio={r['priority']:>2}  last={r['last_successful_crawl_at']}")
    await close_pool()


@sources_group.command("pause")
@click.argument("slug")
def pause(slug: str) -> None:
    asyncio.run(_set_status(slug, "paused"))


@sources_group.command("resume")
@click.argument("slug")
def resume(slug: str) -> None:
    asyncio.run(_set_status(slug, "active"))


async def _set_status(slug: str, status: str) -> None:
    await init_pool()
    async with acquire() as conn:
        await conn.execute("UPDATE sources SET status = $1 WHERE slug = $2", status, slug)
    await close_pool()
    click.echo(f"{slug} → {status}")

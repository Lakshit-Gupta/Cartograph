"""Top-level CLI — `mp <command>`. Wires migrate / seed / sources / identity / opps."""
from __future__ import annotations

import asyncio
from pathlib import Path

import click

from src.cli.identity import identity_group
from src.cli.opps import opps_group
from src.cli.sources import sources_group
from src.common.db import acquire, close_pool, init_pool


@click.group()
def cli() -> None:
    """cartograph admin CLI."""


@cli.command()
def migrate() -> None:
    """Apply pending SQL migrations in migrations/ dir, in lexicographic order."""
    asyncio.run(_migrate())


async def _migrate() -> None:
    await init_pool()
    mig_dir = Path(__file__).resolve().parents[2] / "migrations"
    files = sorted(p for p in mig_dir.glob("V*.sql"))
    async with acquire() as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT NOW())"
        )
        applied = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
        for f in files:
            version = f.stem.split("__")[0]
            if version in applied:
                click.echo(f"[skip] {f.name}")
                continue
            click.echo(f"[apply] {f.name}")
            sql = f.read_text(encoding="utf-8")
            await conn.execute(sql)
    await close_pool()


@cli.command("seed-sources")
def seed_sources() -> None:
    """Re-run V003 source seed (idempotent)."""
    asyncio.run(_seed_sources())


async def _seed_sources() -> None:
    await init_pool()
    p = Path(__file__).resolve().parents[2] / "migrations" / "V003__sources_seed.sql"
    async with acquire() as conn:
        await conn.execute(p.read_text())
    await close_pool()
    click.echo("sources reseeded")


cli.add_command(sources_group)
cli.add_command(identity_group)
cli.add_command(opps_group)


if __name__ == "__main__":
    cli()

"""Top-level CLI — `mp <command>`. Wires migrate / seed / sources / identity / opps.

Migration semantics (do NOT skip on failure — there is no need to wipe volumes):
  - Each V*.sql wraps its body in BEGIN/COMMIT and inserts its own
    schema_migrations marker INSIDE that transaction. A failed file rolls back
    its statements AND its marker row, leaving the database exactly as it was
    before that file ran. Re-running `migrate` after fixing the SQL replays
    cleanly from the failed file onwards.
  - The wipe-volume-and-retry ritual is a misconception, kept here as a
    docstring breadcrumb so the next debugger does not waste cycles.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import asyncpg
import click

from src.cli.identity import identity_group
from src.cli.internshala_discover import internshala_discover
from src.cli.opps import opps_group
from src.cli.routes import routes_group
from src.cli.sources import sources_group, targets_group
from src.cli.tenant import tenant_group
from src.common.db import acquire, close_pool, init_pool

# Static lock id — any 32-bit signed int. Picked from /dev/random once and
# pinned. Held for the full migrate loop to prevent concurrent runners
# (e.g. two parallel `docker compose run --rm tools migrate` invocations)
# from interleaving and corrupting schema_migrations bookkeeping.
_MIGRATE_LOCK_ID = 727274


@click.group()
def cli() -> None:
    """cartograph admin CLI."""


@cli.command()
def migrate() -> None:
    """Apply pending SQL migrations in migrations/ dir, in lexicographic order."""
    asyncio.run(_migrate())


def _format_pg_error(err: asyncpg.PostgresError, sql: str, filename: str) -> str:
    """Render a PostgresError with file:line:col + offending source snippet.

    asyncpg surfaces `position` as a 1-based byte offset into the SQL string
    that was sent. Translating that to line/column + showing the line beats
    dumping a 400-line file at the user and asking them to find the typo.
    """
    pos_attr = getattr(err, "position", None)
    try:
        pos = int(pos_attr) if pos_attr is not None else 0
    except (TypeError, ValueError):
        pos = 0
    if pos <= 0:
        return f"{filename}: {err.__class__.__name__}: {err}"
    head = sql[: pos - 1]
    line_no = head.count("\n") + 1
    col_no = pos - (head.rfind("\n") + 1)
    line_start = head.rfind("\n") + 1
    line_end = sql.find("\n", pos - 1)
    if line_end < 0:
        line_end = len(sql)
    source_line = sql[line_start:line_end]
    caret = " " * (col_no - 1) + "^"
    return f"{filename}:{line_no}:{col_no}: {err.__class__.__name__}: {err}\n    {source_line}\n    {caret}"


async def _migrate() -> None:
    await init_pool()
    mig_dir = Path(__file__).resolve().parents[2] / "migrations"
    files = sorted(p for p in mig_dir.glob("V*.sql"))
    try:
        async with acquire() as conn:
            # Advisory lock blocks until released or connection drops. Two
            # concurrent migrators serialise; we never interleave file writes
            # to schema_migrations.
            await conn.execute("SELECT pg_advisory_lock($1)", _MIGRATE_LOCK_ID)
            try:
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
                    try:
                        await conn.execute(sql)
                    except asyncpg.PostgresError as err:
                        click.echo(_format_pg_error(err, sql, f.name), err=True)
                        click.echo(
                            "[hint] fix the SQL and re-run `mp migrate` — no volume wipe needed; failed file rolled back cleanly.",
                            err=True,
                        )
                        raise SystemExit(1) from err
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", _MIGRATE_LOCK_ID)
    finally:
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
cli.add_command(targets_group)
cli.add_command(tenant_group)
cli.add_command(routes_group)
cli.add_command(internshala_discover)


if __name__ == "__main__":
    cli()

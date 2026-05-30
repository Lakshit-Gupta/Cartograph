"""`mp internshala-discover` — ad-hoc Internshala discovery cycles.

Fast-iteration entrypoint for selector recon + smoke testing without waiting on
the long-running worker's idle loop:

    mp internshala-discover --once                       # one full-matrix cycle
    mp internshala-discover --once --combo backend-development-wfh
    mp internshala-discover --once --dry-run             # no Redis/Postgres writes

`--once` runs exactly one cycle and exits (the CLI always implies it — there is
no daemon mode here). `--combo NAME` filters the matrix to the single combo
whose generated name matches (see `Combo.name`). `--dry-run` skips the Redis
dedup write + `persist_and_publish`; surviving cards print to stdout instead.

Exit code propagates from the worker: 0 = clean cycle, 2 = no healthy identity,
1 = fatal. Against placeholder selectors set `INTERNSHALA_ALLOW_RECON_PENDING=1`
so `load_config` does not refuse to boot.
"""

from __future__ import annotations

import asyncio

import click

from src.workers.internshala_discovery.config import load_config
from src.workers.internshala_discovery_worker import serve


@click.command("internshala-discover")
@click.option("--once", is_flag=True, default=False, help="Run a single cycle and exit (CLI always runs once).")
@click.option("--combo", "combo", default=None, help="Filter the matrix to one combo by its generated name (e.g. backend-development-wfh).")
@click.option(
    "--dry-run", "dry_run", is_flag=True, default=False, help="Skip Redis dedup + Postgres persist; print surviving cards to stdout."
)
def internshala_discover(once: bool, combo: str | None, dry_run: bool) -> None:
    """Run one Internshala discovery cycle (selector recon / smoke)."""
    cfg = load_config(once=True, dry_run=dry_run, combo_filter=combo)
    if combo is not None and not cfg.active_combos():
        known = ", ".join(c.name for c in cfg.matrix)
        raise click.UsageError(f"--combo {combo!r} matched no matrix entry. Known combos: {known}")
    code = asyncio.run(serve(cfg))
    raise SystemExit(code)


__all__ = ["internshala_discover"]

"""`carto internshala-jobs-discover` — ad-hoc Internshala JOBS discovery cycles.

Fast-iteration entrypoint for jobs selector recon + smoke testing without the
long-running worker's idle loop:

    carto internshala-jobs-discover --once                    # both URL variants
    carto internshala-jobs-discover --once --variant fresher  # one variant
    carto internshala-jobs-discover --once --dry-run          # no Redis/Postgres writes

`--variant {general|fresher}` filters to a single listing URL. `--dry-run` skips
the Redis dedup write + `persist_and_publish`; surviving cards print to stdout.
Against placeholder selectors set `INTERNSHALA_JOBS_ALLOW_RECON_PENDING=1` so
`load_jobs_config` does not refuse to boot.
"""

from __future__ import annotations

import asyncio

import click

from src.workers.internshala_jobs_discovery.config import load_jobs_config
from src.workers.internshala_jobs_discovery_worker import serve


@click.command("internshala-jobs-discover")
@click.option("--once", is_flag=True, default=False, help="Run a single cycle and exit (CLI always runs once).")
@click.option("--variant", "variant", type=click.Choice(["general", "fresher"]), default=None, help="Filter to one URL variant.")
@click.option(
    "--dry-run", "dry_run", is_flag=True, default=False, help="Skip Redis dedup + Postgres persist; print surviving cards to stdout."
)
def internshala_jobs_discover(once: bool, variant: str | None, dry_run: bool) -> None:
    """Run one Internshala jobs discovery cycle (selector recon / smoke)."""
    cfg = load_jobs_config(once=True, dry_run=dry_run, variant_filter=variant)
    if variant is not None and not cfg.active_variants():
        known = ", ".join(v.name for v in cfg.active_variants() or [])
        raise click.UsageError(f"--variant {variant!r} matched no enabled variant. Enabled: {known or '(none)'}")
    code = asyncio.run(serve(cfg))
    raise SystemExit(code)


__all__ = ["internshala_jobs_discover"]

"""`mp sources ...` — list / pause / resume / add.

Also exposes `mp targets ...` so the Phase 3.4 OSS funnel can be
populated from the CLI without a SQL shell. Both groups share this
module because they're both source-of-truth-edit operations and the
CLI is already wired here.
"""

from __future__ import annotations

import asyncio
import re

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


# ---------------------------------------------------------------------------
# Phase 3.4 — `mp targets ...` group.
# ---------------------------------------------------------------------------
#
# Lightweight CRUD over target_companies. The cold-outreach lane
# (Phase 2.1) already reads name/domain/mission_summary/why_target;
# Phase 3.4 added github_org + active. This group lets the user
# populate / pause those rows from a shell without hand-editing SQL.

# GitHub org slugs follow `^[A-Za-z0-9][A-Za-z0-9-]{0,38}$`. We're a
# touch more permissive (no length cap upper bound enforcement —
# GitHub caps at 39, we accept 64) since the API will hard-fail any
# real overrun; the validator here is just to keep typos out.
_GITHUB_ORG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")


@click.group("targets")
def targets_group() -> None:
    """Manage target_companies (cold outreach + OSS funnel)."""


@targets_group.command("add")
@click.option("--name", required=True, help="Display name, e.g. 'Vercel'.")
@click.option("--github-org", "github_org", default=None, help="GitHub org slug, e.g. 'vercel'.")
@click.option("--domain", default=None, help="Primary domain (used by cold-outreach).")
@click.option("--why", "why_target", default=None, help="One-line note: why this company is on the list.")
@click.option("--user-id", "user_id", default=1, show_default=True, type=int, help="Owner user_id (Phase 4 multi-tenant).")
def add_target(
    name: str,
    github_org: str | None,
    domain: str | None,
    why_target: str | None,
    user_id: int,
) -> None:
    """Add or upsert one target_companies row.

    At least one of --github-org or --domain must be supplied;
    otherwise the row would be useless to every downstream consumer.
    The OSS funnel only scans rows with `github_org` set; the
    cold-outreach lane only scans rows with `domain` set. Setting
    both lights both lanes up for the same company.
    """
    if not github_org and not domain:
        raise click.UsageError("supply at least one of --github-org or --domain")
    if github_org and not _GITHUB_ORG_RE.match(github_org):
        raise click.UsageError(f"--github-org {github_org!r} fails ^[A-Za-z0-9][A-Za-z0-9-]{{0,63}}$ — check for typos")
    asyncio.run(_add_target(name=name, github_org=github_org, domain=domain, why_target=why_target, user_id=user_id))


async def _add_target(
    *,
    name: str,
    github_org: str | None,
    domain: str | None,
    why_target: str | None,
    user_id: int,
) -> None:
    await init_pool()
    async with acquire() as conn:
        # Two-pronged upsert because the V010 unique index is partial
        # (only WHERE domain IS NOT NULL). When the caller supplies no
        # domain we cannot rely on ON CONFLICT — we look up by name +
        # user_id and INSERT or UPDATE manually.
        if domain:
            rec = await conn.fetchrow(
                """
                INSERT INTO target_companies
                    (user_id, name, domain, github_org, why_target, active)
                VALUES ($1, $2, $3, $4, $5, TRUE)
                ON CONFLICT (user_id, (lower(domain))) WHERE domain IS NOT NULL
                DO UPDATE SET
                    name       = EXCLUDED.name,
                    github_org = COALESCE(EXCLUDED.github_org, target_companies.github_org),
                    why_target = COALESCE(EXCLUDED.why_target, target_companies.why_target),
                    active     = TRUE
                RETURNING id, name, github_org, domain
                """,
                user_id,
                name,
                domain,
                github_org,
                why_target,
            )
        else:
            existing = await conn.fetchrow(
                "SELECT id FROM target_companies WHERE user_id = $1 AND lower(name) = lower($2) LIMIT 1",
                user_id,
                name,
            )
            if existing is None:
                rec = await conn.fetchrow(
                    """
                    INSERT INTO target_companies
                        (user_id, name, github_org, why_target, active)
                    VALUES ($1, $2, $3, $4, TRUE)
                    RETURNING id, name, github_org, domain
                    """,
                    user_id,
                    name,
                    github_org,
                    why_target,
                )
            else:
                rec = await conn.fetchrow(
                    """
                    UPDATE target_companies
                    SET github_org = COALESCE($2, github_org),
                        why_target = COALESCE($3, why_target),
                        active     = TRUE
                    WHERE id = $1
                    RETURNING id, name, github_org, domain
                    """,
                    int(existing["id"]),
                    github_org,
                    why_target,
                )
    await close_pool()
    if rec:
        click.echo(f"target #{rec['id']:>4d}  name={rec['name']}  github_org={rec['github_org'] or '-'}  domain={rec['domain'] or '-'}")
    else:
        click.echo("no row affected", err=True)


@targets_group.command("list")
def list_targets() -> None:
    asyncio.run(_list_targets())


async def _list_targets() -> None:
    await init_pool()
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, github_org, domain, active, issues_emitted_30d, last_funnel_scan_at
            FROM target_companies
            ORDER BY id
            """
        )
    for r in rows:
        click.echo(
            f"#{r['id']:>4d}  {r['name']:<30s}  github_org={r['github_org'] or '-':<24s}  domain={r['domain'] or '-':<25s}  "
            f"active={r['active']}  emitted30d={r['issues_emitted_30d']:>3d}  last_scan={r['last_funnel_scan_at']}"
        )
    await close_pool()


@targets_group.command("pause")
@click.argument("name")
def pause_target(name: str) -> None:
    asyncio.run(_set_target_active(name, False))


@targets_group.command("resume")
@click.argument("name")
def resume_target(name: str) -> None:
    asyncio.run(_set_target_active(name, True))


async def _set_target_active(name: str, active: bool) -> None:
    await init_pool()
    async with acquire() as conn:
        result = await conn.execute(
            "UPDATE target_companies SET active = $2 WHERE lower(name) = lower($1)",
            name,
            active,
        )
    await close_pool()
    click.echo(f"{name} → active={active}  ({result})")

"""`mp tenant` — invite-token lifecycle for Phase 4.2 multi-tenant onboarding.

The owner mints a single-use token, copies it to the new tenant out-of-band
(DM, Signal, etc.), and the tenant redeems it via the Discord
`/jobs-onboard <token>` slash command. Tokens are 64-char hex
(`secrets.token_hex(32)`) — long enough that brute force is infeasible
even if an attacker knew the table was populated.

Sub-commands:

  mp tenant invite [--ttl-hours N]   mint a new token
  mp tenant list                     show unused tokens (+ recent consumed)
  mp tenant revoke <token>           mark a token consumed-by-revocation

Listing tokens shows the prefix only by default (first 12 chars + `…`) so
operators can grep without screenshotting full secrets; pass `--full` to
print the entire token (e.g. when re-sending to the new tenant). Revoke
sets `consumed_at = NOW()` with a sentinel `consumed_by_user_id = 0`, so
the row is permanently dead but auditable.
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime, timedelta

import click

from src.common.db import acquire, close_pool, init_pool

_DEFAULT_TTL_HOURS = 24 * 7  # one week — long enough for async coordination
_REVOKE_SENTINEL_USER_ID = 0


@click.group("tenant")
def tenant_group() -> None:
    """Tenant invite tokens (Phase 4.2)."""


@tenant_group.command("invite")
@click.option("--ttl-hours", type=int, default=_DEFAULT_TTL_HOURS, show_default=True)
@click.option("--note", default="", help="Free-text metadata stored on the row.")
def invite_cmd(ttl_hours: int, note: str) -> None:
    """Mint a fresh single-use invite token."""
    asyncio.run(_invite(ttl_hours=ttl_hours, note=note))


@tenant_group.command("list")
@click.option("--full", is_flag=True, help="Print full tokens (default truncates).")
@click.option("--include-consumed/--no-include-consumed", default=False)
def list_cmd(full: bool, include_consumed: bool) -> None:
    """Show unused (and optionally consumed) invite tokens."""
    asyncio.run(_list(full=full, include_consumed=include_consumed))


@tenant_group.command("revoke")
@click.argument("token")
def revoke_cmd(token: str) -> None:
    """Mark an unused invite token as permanently dead."""
    asyncio.run(_revoke(token=token.strip().lower()))


async def _invite(*, ttl_hours: int, note: str) -> None:
    token = secrets.token_hex(32)
    expires_at = datetime.now(UTC) + timedelta(hours=ttl_hours)
    await init_pool()
    try:
        async with acquire() as conn:
            # `created_by_user_id` = the founding owner (id=1). The CLI is
            # owner-only by deployment posture (runs from inside the
            # operator's shell), so we don't need a richer attribution model.
            await conn.execute(
                """
                INSERT INTO tenant_invites (token, created_by_user_id, expires_at, metadata)
                VALUES ($1, $2, $3, $4::jsonb)
                """,
                token,
                1,
                expires_at,
                f'{{"note": "{note}"}}' if note else "{}",
            )
    finally:
        await close_pool()
    click.echo(f"token: {token}")
    click.echo(f"expires_at: {expires_at.isoformat()}")
    click.echo("share via DM/Signal; tenant redeems via Discord `/jobs-onboard <token>`")


async def _list(*, full: bool, include_consumed: bool) -> None:
    await init_pool()
    try:
        async with acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT token, created_at, expires_at, consumed_at,
                       consumed_by_user_id, metadata
                  FROM tenant_invites
                 WHERE $1::bool OR consumed_at IS NULL
                 ORDER BY created_at DESC
                 LIMIT 100
                """,
                include_consumed,
            )
    finally:
        await close_pool()
    if not rows:
        click.echo("(no rows)")
        return
    for r in rows:
        tok = r["token"] if full else (r["token"][:12] + "…")
        status = _status_label(r)
        click.echo(f"{tok}  {status:<14}  created={_fmt_ts(r['created_at'])}  expires={_fmt_ts(r['expires_at'])}")


async def _revoke(*, token: str) -> None:
    await init_pool()
    try:
        async with acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE tenant_invites
                   SET consumed_at = NOW(),
                       consumed_by_user_id = $2
                 WHERE token = $1 AND consumed_at IS NULL
                 RETURNING token
                """,
                token,
                _REVOKE_SENTINEL_USER_ID,
            )
    finally:
        await close_pool()
    if row is None:
        click.echo("no unused row matched that token", err=True)
        raise SystemExit(1)
    click.echo(f"revoked: {row['token'][:12]}…")


def _status_label(row) -> str:  # type: ignore[no-untyped-def]
    if row["consumed_at"] is not None:
        if row["consumed_by_user_id"] == _REVOKE_SENTINEL_USER_ID:
            return "revoked"
        return f"consumed→u{row['consumed_by_user_id']}"
    if row["expires_at"] is not None and row["expires_at"] < datetime.now(UTC):
        return "expired"
    return "unused"


def _fmt_ts(ts) -> str:  # type: ignore[no-untyped-def]
    if ts is None:
        return "—"
    return ts.strftime("%Y-%m-%d %H:%M")

"""/jobs-onboard <token> — claim a tenant_invites token + link Discord user.

Phase 4.2 multi-tenant onboarding entry point. Consuming a token does three
things in one transaction so a torn read never leaves the bot half-onboarded:

1. SELECT FOR UPDATE the row from `tenant_invites` and verify it is unused
   and unexpired.
2. INSERT a new `users` row (or UPDATE existing if the Discord id already
   has a row from a prior owner_autolink) carrying `discord_user_id`,
   `onboarded_via='invite_token'`, and `onboarded_at=NOW()`.
3. UPDATE the invite row to record `consumed_by_user_id` + `consumed_at`.

After success the bot pins the new tenant via `db.set_tenant()` so any
further DB calls inside this handler (none today, but future
welcome-flow hooks) are correctly scoped.

Refusal cases (all ephemeral):
- Token shape wrong (not 64 hex chars).
- Token unknown.
- Token already consumed.
- Token expired.
- Discord user already linked to a tenant (running `/jobs-onboard` twice).
"""

from __future__ import annotations

import re

import discord

from src.common import db
from src.common.logger import get_logger

_log = get_logger(__name__)

_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")


def setup(bot) -> None:  # type: ignore[no-untyped-def]
    @bot.tree.command(
        name="jobs-onboard",
        description="Link this Discord user to a Cartograph tenant via invite token.",
    )
    @discord.app_commands.describe(token="64-char hex invite token from the owner.")
    async def jobs_onboard_cmd(interaction: discord.Interaction, token: str) -> None:
        token = token.strip().lower()
        if not _TOKEN_RE.match(token):
            await interaction.response.send_message(
                "Invalid token shape. Expect 64 hex chars.",
                ephemeral=True,
            )
            return

        discord_user_id = int(interaction.user.id)
        display_name = interaction.user.display_name[:64]
        handle = str(interaction.user.name)[:64] or f"discord_{discord_user_id}"

        try:
            new_user_id = await _consume_invite_and_create_user(
                token=token,
                discord_user_id=discord_user_id,
                handle=handle,
                display_name=display_name,
            )
        except _OnboardError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        except Exception as e:
            _log.exception("jobs_onboard_failed", err=str(e))
            await interaction.response.send_message(
                "Internal error linking tenant. The owner has been alerted.",
                ephemeral=True,
            )
            return

        db.set_tenant(new_user_id)
        _log.info("tenant_onboarded", user_id=new_user_id, discord_user_id=discord_user_id)
        await interaction.response.send_message(
            f"Linked. Welcome — your tenant id is {new_user_id}. Run `/status` to see the pipeline.",
            ephemeral=True,
        )


class _OnboardError(RuntimeError):
    """User-visible refusal (printed ephemerally). Never logged as exception."""


async def _consume_invite_and_create_user(
    *,
    token: str,
    discord_user_id: int,
    handle: str,
    display_name: str,
) -> int:
    """Single-transaction onboarding flow. Returns the new `users.id`.

    Why all in one txn? If the invite row is consumed but the user row
    fails to insert (or vice versa), the bot ends up with a half-onboarded
    Discord id — recovery is messy (no clean "retry onboarding" UX). The
    txn rolls back both writes if either step fails.
    """
    async with db.acquire() as conn, conn.transaction():
        # Already linked? Refuse without consuming the token.
        existing = await conn.fetchrow(
            "SELECT id FROM users WHERE discord_user_id = $1 LIMIT 1",
            discord_user_id,
        )
        if existing is not None:
            raise _OnboardError(f"This Discord user is already linked to tenant id {existing['id']}.")

        invite = await conn.fetchrow(
            """
            SELECT token, expires_at, consumed_at
              FROM tenant_invites
             WHERE token = $1
             FOR UPDATE
            """,
            token,
        )
        if invite is None:
            raise _OnboardError("Unknown invite token.")
        if invite["consumed_at"] is not None:
            raise _OnboardError("That invite token has already been used.")
        if invite["expires_at"] is not None and await _is_expired(conn, invite["expires_at"]):
            raise _OnboardError("That invite token has expired. Ask the owner for a fresh one.")

        new_user = await conn.fetchrow(
            """
            INSERT INTO users (handle, display_name, discord_user_id, onboarded_via, onboarded_at)
            VALUES ($1, $2, $3, 'invite_token', NOW())
            RETURNING id
            """,
            handle,
            display_name,
            discord_user_id,
        )
        new_user_id = int(new_user["id"])

        await conn.execute(
            """
            UPDATE tenant_invites
               SET consumed_by_user_id = $1, consumed_at = NOW()
             WHERE token = $2
            """,
            new_user_id,
            token,
        )
        return new_user_id


async def _is_expired(conn, expires_at) -> bool:  # type: ignore[no-untyped-def]
    """Compare expires_at to server NOW() — keeps clock authority on
    Postgres so the bot host's clock drift can never accidentally accept
    an expired token.
    """
    row = await conn.fetchrow("SELECT $1::timestamptz < NOW() AS expired", expires_at)
    return bool(row["expired"])

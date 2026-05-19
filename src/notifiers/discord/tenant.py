"""Phase 4.2 — Discord interaction → tenant resolver.

Maps `discord.Interaction.user.id` to `users.id` and pins it on the asyncio
task via `db.set_tenant()`. Every slash-command handler should call
`resolve_tenant(interaction)` first and refuse the interaction (ephemeral
reply) if the Discord user has no `users.discord_user_id` row.

`/jobs-onboard <token>` is the only command that runs without a resolved
tenant — it CREATES the linkage by consuming a row from `tenant_invites`.

Why a resolver rather than reading every query against
`users.discord_user_id` directly? Per-tenant scoping touches dozens of code
paths (digest, apply, follow-up, refit, status, cost). Pinning the
contextvar once at the handler boundary keeps every downstream query
ignorant of Discord internals — they just call `db.current_tenant()`.

The founding owner (V001 inserted `users(id=1)` with `discord_user_id=NULL`)
gets linked automatically the first time a Discord interaction arrives from
the configured `DISCORD_OWNER_ID` setting — see `_ensure_owner_linked`.
"""

from __future__ import annotations

from dataclasses import dataclass

import discord

from src.common import db
from src.common.logger import get_logger
from src.common.secrets import get_settings

_log = get_logger(__name__)


@dataclass(frozen=True)
class TenantContext:
    """Resolved tenant for a single Discord interaction."""

    user_id: int
    is_owner: bool


_OWNER_USER_ID = 1  # V001 founding row — never recycled.


async def _ensure_owner_linked(discord_user_id: int) -> None:
    """Best-effort: first time the configured owner Discord id appears, link
    it to `users.id = 1`. Idempotent — repeated calls become no-ops once
    the row carries the id.

    Why bother? Removes a manual SQL step from the multi-tenant cutover —
    the existing solo user keeps working without operator intervention.
    """
    settings = get_settings()
    owner = getattr(settings, "discord_owner_id", None)
    if not owner or int(owner) != int(discord_user_id):
        return
    try:
        await db.execute(
            """
            UPDATE users
               SET discord_user_id = $1,
                   onboarded_via = COALESCE(onboarded_via, 'owner_autolink'),
                   onboarded_at = COALESCE(onboarded_at, NOW())
             WHERE id = $2 AND discord_user_id IS NULL
            """,
            int(discord_user_id),
            _OWNER_USER_ID,
        )
    except Exception as e:
        # Don't fail the interaction over a soft-link best-effort.
        _log.warning("owner_autolink_failed", err=str(e))


async def _lookup_tenant(discord_user_id: int) -> int | None:
    """Return `users.id` for a Discord user, or None if not onboarded."""
    row = await db.fetch_one(
        "SELECT id FROM users WHERE discord_user_id = $1 LIMIT 1",
        int(discord_user_id),
    )
    return int(row["id"]) if row else None


async def resolve_tenant(interaction: discord.Interaction) -> TenantContext | None:
    """Pin the tenant for this interaction. Returns None if the Discord
    user has no `users` row — caller should reply ephemerally and refuse.

    `/jobs-onboard` is the documented escape hatch and MUST be allowed to
    run without a resolved tenant; that command should *not* call this.
    """
    discord_user_id = int(interaction.user.id)

    # Try owner autolink first — this is a no-op except the very first time
    # the founding-owner Discord id ever interacts with the bot.
    await _ensure_owner_linked(discord_user_id)

    user_id = await _lookup_tenant(discord_user_id)
    if user_id is None:
        return None

    db.set_tenant(user_id)
    return TenantContext(user_id=user_id, is_owner=(user_id == _OWNER_USER_ID))


async def refuse_unonboarded(interaction: discord.Interaction) -> None:
    """Ephemeral reply telling an unknown Discord user how to onboard.

    The message names `/jobs-onboard` explicitly — discoverable + actionable.
    """
    try:
        await interaction.response.send_message(
            "You're not linked to a Cartograph tenant. Ask the owner for an invite token then run `/jobs-onboard <token>`.",
            ephemeral=True,
        )
    except Exception as e:
        _log.warning("refuse_unonboarded_reply_failed", err=str(e))


__all__ = ["TenantContext", "refuse_unonboarded", "resolve_tenant"]

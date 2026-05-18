"""/identity status | update — identity-vault management."""

from __future__ import annotations

import discord
from discord import app_commands

from src.common import db
from src.common.logger import get_logger
from src.common.queue import RedisQ, Streams

_log = get_logger(__name__)

_VALID_FIELDS = {"ban_status", "warmup_score", "warmup_completed", "email_alias"}


def setup(bot) -> None:  # type: ignore[no-untyped-def]
    group = app_commands.Group(name="identity", description="Identity vault management.")

    @group.command(name="status", description="Show identity health by platform.")
    async def status(interaction: discord.Interaction):
        try:
            rows = await db.fetch_all(
                """
                SELECT platform, ban_status, COUNT(*) AS n
                FROM identities
                GROUP BY platform, ban_status
                ORDER BY platform, ban_status
                """
            )
            if not rows:
                await interaction.response.send_message("No identities provisioned yet.", ephemeral=True)
                return
            lines = [f"`{r['platform']}` [{r['ban_status']}] = {r['n']}" for r in rows]
            await interaction.response.send_message("\n".join(lines), ephemeral=True)
        except Exception as e:
            _log.exception("identity_status_failed", err=str(e))
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

    @group.command(name="update", description="Update one field on one identity (account_label).")
    @app_commands.describe(
        account_label="The identity's `account_label`",
        field="ban_status|warmup_score|warmup_completed|email_alias",
        value="New value",
    )
    async def update(interaction: discord.Interaction, account_label: str, field: str, value: str):
        if field not in _VALID_FIELDS:
            await interaction.response.send_message(f"Field must be one of: {', '.join(sorted(_VALID_FIELDS))}", ephemeral=True)
            return
        try:
            q = await RedisQ.connect()
            await q.publish(
                Streams.APPLY,
                {
                    "action": "identity_update",
                    "user_id": 1,
                    "account_label": account_label,
                    "field": field,
                    "value": value,
                },
            )
            await interaction.response.send_message(f"Update queued: `{account_label}`.{field}={value}", ephemeral=True)
        except Exception as e:
            _log.exception("identity_update_failed", err=str(e))
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

    bot.tree.add_command(group)

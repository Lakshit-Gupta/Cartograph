"""/snooze <opp_id> <days> — hide opp for N days."""

from __future__ import annotations

from uuid import UUID

import discord
from discord import app_commands

from src.common import db
from src.common.logger import get_logger
from src.common.queue import RedisQ, Streams
from src.notifiers.discord import voice

_log = get_logger(__name__)


def setup(bot) -> None:  # type: ignore[no-untyped-def]
    @bot.tree.command(name="snooze", description="Snooze an opp for N days.")
    @app_commands.describe(opp_id="UUID of the opportunity", days="How many days to snooze")
    async def snooze_cmd(interaction: discord.Interaction, opp_id: str, days: int = 1):
        try:
            uid = str(UUID(opp_id))
        except ValueError:
            await interaction.response.send_message(f"`{opp_id}` is not a valid UUID.", ephemeral=True)
            return
        if days < 1 or days > 30:
            await interaction.response.send_message("Days must be 1..30.", ephemeral=True)
            return
        try:
            await db.execute(
                "UPDATE opportunities SET state = $2 WHERE id = $1",
                UUID(uid),
                "snoozed",
            )
            q = await RedisQ.connect()
            await q.publish(
                Streams.APPLY,
                {
                    "action": "snooze",
                    "opp_id": uid,
                    "user_id": 1,
                    "days": int(days),
                    "source": "slash",
                },
            )
            await interaction.response.send_message(f"{voice.pick('snoozed_confirm')} ({days}d)", ephemeral=True)
        except Exception as e:
            _log.exception("snooze_failed", err=str(e), opp_id=uid)
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

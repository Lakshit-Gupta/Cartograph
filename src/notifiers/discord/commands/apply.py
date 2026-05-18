"""/apply <opp_id> — transition opp into `applied` and enqueue send."""

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
    @bot.tree.command(name="apply", description="Mark an opportunity as applied.")
    @app_commands.describe(opp_id="UUID of the opportunity")
    async def apply_cmd(interaction: discord.Interaction, opp_id: str):
        try:
            uid = str(UUID(opp_id))
        except ValueError:
            await interaction.response.send_message(f"`{opp_id}` is not a valid UUID.", ephemeral=True)
            return
        try:
            await db.execute(
                "UPDATE opportunities SET state = $2 WHERE id = $1",
                UUID(uid),
                "applied",
            )
            q = await RedisQ.connect()
            await q.publish(
                Streams.APPLY,
                {"action": "apply", "opp_id": uid, "user_id": 1, "source": "slash"},
            )
            await interaction.response.send_message(voice.pick("applied_confirm"), ephemeral=True)
        except Exception as e:
            _log.exception("apply_failed", err=str(e), opp_id=uid)
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

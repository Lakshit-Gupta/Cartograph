"""/pin <opp_id> — keep an opp at the top of next digest."""

from __future__ import annotations

from uuid import UUID

import discord
from discord import app_commands

from src.common.logger import get_logger
from src.common.queue import RedisQ, Streams
from src.notifiers.discord import voice

_log = get_logger(__name__)


def setup(bot) -> None:  # type: ignore[no-untyped-def]
    @bot.tree.command(name="pin", description="Pin an opportunity to next digest top.")
    @app_commands.describe(opp_id="UUID of the opportunity")
    async def pin_cmd(interaction: discord.Interaction, opp_id: str):
        try:
            uid = str(UUID(opp_id))
        except ValueError:
            await interaction.response.send_message(f"`{opp_id}` is not a valid UUID.", ephemeral=True)
            return
        try:
            q = await RedisQ.connect()
            await q.publish(
                Streams.APPLY,
                {"action": "pin", "opp_id": uid, "user_id": 1, "source": "slash"},
            )
            await interaction.response.send_message(voice.pick("pinned_confirm"), ephemeral=True)
        except Exception as e:
            _log.exception("pin_failed", err=str(e), opp_id=uid)
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

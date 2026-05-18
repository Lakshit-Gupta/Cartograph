"""/followup <opp_id> — draft a polite follow-up email."""

from __future__ import annotations

from uuid import UUID

import discord
from discord import app_commands

from src.common.logger import get_logger
from src.common.queue import RedisQ, Streams

_log = get_logger(__name__)


def setup(bot) -> None:  # type: ignore[no-untyped-def]
    @bot.tree.command(name="followup", description="Queue a follow-up email for an applied opp.")
    @app_commands.describe(opp_id="UUID of the applied opportunity")
    async def followup_cmd(interaction: discord.Interaction, opp_id: str):
        try:
            uid = str(UUID(opp_id))
        except ValueError:
            await interaction.response.send_message(f"`{opp_id}` is not a valid UUID.", ephemeral=True)
            return
        try:
            q = await RedisQ.connect()
            await q.publish(
                Streams.APPLY,
                {"action": "followup", "opp_id": uid, "user_id": 1, "source": "slash"},
            )
            await interaction.response.send_message("Follow-up queued. You'll see a draft to approve.", ephemeral=True)
        except Exception as e:
            _log.exception("followup_failed", err=str(e), opp_id=uid)
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

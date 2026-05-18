"""/skip <opp_id> — drop opp from future digests."""

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
    @bot.tree.command(name="skip", description="Skip an opp (won't show again).")
    @app_commands.describe(opp_id="UUID of the opportunity")
    async def skip_cmd(interaction: discord.Interaction, opp_id: str):
        try:
            uid = str(UUID(opp_id))
        except ValueError:
            await interaction.response.send_message(f"`{opp_id}` is not a valid UUID.", ephemeral=True)
            return
        try:
            await db.execute(
                "UPDATE opportunities SET state = $2 WHERE id = $1",
                UUID(uid),
                "seen",
            )
            q = await RedisQ.connect()
            await q.publish(
                Streams.APPLY,
                {"action": "skip", "opp_id": uid, "user_id": 1, "source": "slash"},
            )
            await interaction.response.send_message(voice.pick("skipped_confirm"), ephemeral=True)
        except Exception as e:
            _log.exception("skip_failed", err=str(e), opp_id=uid)
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

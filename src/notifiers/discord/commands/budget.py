"""/budget set | today | status — comp-floor + daily target management."""

from __future__ import annotations

import discord
from discord import app_commands

from src.common import db
from src.common.logger import get_logger
from src.common.queue import RedisQ, Streams

_log = get_logger(__name__)


def setup(bot) -> None:  # type: ignore[no-untyped-def]
    group = app_commands.Group(name="budget", description="Comp floor and daily app target.")

    @group.command(name="set", description="Set comp floor (USD/hr for freelance).")
    @app_commands.describe(usd_per_hr="Minimum hourly rate in USD")
    async def set_floor(interaction: discord.Interaction, usd_per_hr: float):
        try:
            await db.execute(
                """
                INSERT INTO profiles (user_id, min_comp_usd_hr)
                VALUES (1, $1)
                ON CONFLICT (user_id) DO UPDATE SET min_comp_usd_hr = EXCLUDED.min_comp_usd_hr,
                                                  updated_at = NOW()
                """,
                usd_per_hr,
            )
            await interaction.response.send_message(f"Comp floor set to ${usd_per_hr:g}/hr.", ephemeral=True)
        except Exception as e:
            _log.exception("budget_set_failed", err=str(e))
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

    @group.command(name="today", description="Set today's app-send target.")
    @app_commands.describe(target="Target number of applications to send today")
    async def today(interaction: discord.Interaction, target: int):
        try:
            q = await RedisQ.connect()
            await q.publish(
                Streams.APPLY,
                {"action": "set_daily_target", "user_id": 1, "target": int(target)},
            )
            await interaction.response.send_message(f"Today's target = {target} applications.", ephemeral=True)
        except Exception as e:
            _log.exception("budget_today_failed", err=str(e))
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

    @group.command(name="status", description="Show current budget + daily target.")
    async def status(interaction: discord.Interaction):
        try:
            row = await db.fetch_one("SELECT min_comp_usd_hr FROM profiles WHERE user_id = 1")
            floor = row["min_comp_usd_hr"] if row else None
            applied_today = await db.fetch_one("SELECT COUNT(*) AS c FROM applications WHERE user_id = 1 AND sent_at::date = CURRENT_DATE")
            n = int(applied_today["c"]) if applied_today else 0
            await interaction.response.send_message(
                f"Comp floor: ${floor or '—'}/hr · Sent today: {n}",
                ephemeral=True,
            )
        except Exception as e:
            _log.exception("budget_status_failed", err=str(e))
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

    bot.tree.add_command(group)

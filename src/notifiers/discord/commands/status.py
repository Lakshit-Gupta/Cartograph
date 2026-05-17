"""/status — pipeline summary embed."""
from __future__ import annotations

from datetime import UTC, datetime

import discord

from src.common import db
from src.common.logger import get_logger

_log = get_logger(__name__)


def setup(bot) -> None:  # type: ignore[no-untyped-def]
    @bot.tree.command(name="status", description="Pipeline summary.")
    async def status_cmd(interaction: discord.Interaction):
        try:
            opps_24h = await db.fetch_one(
                "SELECT COUNT(*) AS c FROM opportunities WHERE first_seen > NOW() - INTERVAL '24 hours'"
            )
            applied_today = await db.fetch_one(
                "SELECT COUNT(*) AS c FROM applications WHERE user_id = 1 AND sent_at::date = CURRENT_DATE"
            )
            sources = await db.fetch_one(
                """
                SELECT
                  COUNT(*) FILTER (WHERE status = 'active')       AS active,
                  COUNT(*) FILTER (WHERE status = 'paused')       AS paused,
                  COUNT(*) FILTER (WHERE status = 'quarantined')  AS quarantined
                FROM sources
                """
            )
            cost = await db.fetch_one(
                """
                SELECT COALESCE(SUM(cost_usd_micros), 0) / 1000000.0 AS usd
                FROM usage_ledger
                WHERE ts::date = CURRENT_DATE AND user_id = 1
                """
            )
            ban = await db.fetch_one(
                """
                SELECT
                  COUNT(*) FILTER (WHERE ban_status = 'healthy')      AS healthy,
                  COUNT(*) FILTER (WHERE ban_status = 'suspect')      AS suspect,
                  COUNT(*) FILTER (WHERE ban_status = 'quarantined')  AS quarantined,
                  COUNT(*) FILTER (WHERE ban_status = 'banned')       AS banned
                FROM identities
                """
            )

            embed = discord.Embed(
                title="Marked_Path — pipeline status",
                color=discord.Color(0x10B981),
                timestamp=datetime.now(UTC),
            )
            embed.add_field(name="opps last 24h", value=str(opps_24h["c"] if opps_24h else 0))
            embed.add_field(
                name="applied today",
                value=str(applied_today["c"] if applied_today else 0),
            )
            embed.add_field(
                name="sources",
                value=(
                    f"active {sources['active'] if sources else 0} · "
                    f"paused {sources['paused'] if sources else 0} · "
                    f"quarantined {sources['quarantined'] if sources else 0}"
                ),
                inline=False,
            )
            embed.add_field(
                name="cost today",
                value=f"${float(cost['usd'] if cost else 0):.4f}",
            )
            embed.add_field(
                name="identities",
                value=(
                    f"healthy {ban['healthy'] if ban else 0} · "
                    f"suspect {ban['suspect'] if ban else 0} · "
                    f"quarantined {ban['quarantined'] if ban else 0} · "
                    f"banned {ban['banned'] if ban else 0}"
                ),
                inline=False,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            _log.exception("status_failed", err=str(e))
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

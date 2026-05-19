"""/cost today | cap — cost ledger view + cap update."""

from __future__ import annotations

import discord
from discord import app_commands

from src.common import db
from src.common.logger import get_logger
from src.common.queue import RedisQ, Streams
from src.notifiers.discord.tenant import refuse_unonboarded, resolve_tenant

_log = get_logger(__name__)


def setup(bot) -> None:  # type: ignore[no-untyped-def]
    group = app_commands.Group(name="cost", description="LLM + proxy cost ledger.")

    @group.command(name="today", description="Show today's LLM spend.")
    async def today(interaction: discord.Interaction):
        tenant = await resolve_tenant(interaction)
        if tenant is None:
            await refuse_unonboarded(interaction)
            return
        try:
            rows = await db.fetch_all(
                """
                SELECT kind, model, COALESCE(SUM(cost_usd_micros), 0) / 1000000.0 AS usd,
                       SUM(input_tokens) AS in_tok, SUM(output_tokens) AS out_tok
                FROM usage_ledger
                WHERE ts::date = CURRENT_DATE AND user_id = $1
                GROUP BY kind, model
                ORDER BY usd DESC
                """,
                tenant.user_id,
            )
            if not rows:
                await interaction.response.send_message("$0.00 today.", ephemeral=True)
                return
            total = sum(float(r["usd"]) for r in rows)
            lines = [f"`{r['kind']}` ({r['model'] or '—'}): ${float(r['usd']):.4f} ({r['in_tok']}/{r['out_tok']} tok)" for r in rows]
            lines.append(f"\n**total ${total:.4f}**")
            await interaction.response.send_message("\n".join(lines), ephemeral=True)
        except Exception as e:
            _log.exception("cost_today_failed", err=str(e))
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

    @group.command(name="cap", description="Set daily LLM cap (USD).")
    @app_commands.describe(usd="New daily cap in USD")
    async def cap(interaction: discord.Interaction, usd: float):
        tenant = await resolve_tenant(interaction)
        if tenant is None:
            await refuse_unonboarded(interaction)
            return
        try:
            q = await RedisQ.connect()
            await q.publish(
                Streams.APPLY,
                {"action": "set_cost_cap_daily", "user_id": tenant.user_id, "usd": float(usd)},
            )
            await interaction.response.send_message(f"Daily cap → ${usd:.2f}", ephemeral=True)
        except Exception as e:
            _log.exception("cost_cap_failed", err=str(e))
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

    bot.tree.add_command(group)

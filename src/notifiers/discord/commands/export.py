"""/export <range> — emit a CSV of applied opps + outcomes."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands

from src.common import db
from src.common.logger import get_logger
from src.notifiers.discord.tenant import refuse_unonboarded, resolve_tenant

_log = get_logger(__name__)

_RANGES = {"7d": 7, "30d": 30, "90d": 90, "all": 36500}


def setup(bot) -> None:  # type: ignore[no-untyped-def]
    @bot.tree.command(name="export", description="Export applications as CSV.")
    @app_commands.describe(range="Range: 7d | 30d | 90d | all")
    async def export_cmd(interaction: discord.Interaction, range: str = "30d"):
        tenant = await resolve_tenant(interaction)
        if tenant is None:
            await refuse_unonboarded(interaction)
            return
        days = _RANGES.get(range)
        if days is None:
            await interaction.response.send_message(f"Range must be one of {', '.join(_RANGES)}.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            since = datetime.now(UTC) - timedelta(days=days)
            rows = await db.fetch_all(
                """
                SELECT a.sent_at, o.title, o.company, o.canonical_url, o.category,
                       a.method, a.response_status, a.response_at
                FROM applications a
                JOIN opportunities o ON o.id = a.opportunity_id
                WHERE a.user_id = $1 AND a.sent_at >= $2
                ORDER BY a.sent_at DESC
                """,
                tenant.user_id,
                since,
            )
            if not rows:
                await interaction.followup.send("Nothing to export.", ephemeral=True)
                return

            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(
                [
                    "sent_at",
                    "title",
                    "company",
                    "url",
                    "category",
                    "method",
                    "response_status",
                    "response_at",
                ]
            )
            for r in rows:
                writer.writerow(
                    [
                        r["sent_at"].isoformat() if r["sent_at"] else "",
                        r["title"] or "",
                        r["company"] or "",
                        r["canonical_url"] or "",
                        r["category"] or "",
                        r["method"] or "",
                        r["response_status"] or "",
                        r["response_at"].isoformat() if r["response_at"] else "",
                    ]
                )
            buf.seek(0)
            file = discord.File(
                io.BytesIO(buf.getvalue().encode("utf-8")),
                filename=f"applications_{range}.csv",
            )
            await interaction.followup.send(f"Exported {len(rows)} rows.", file=file, ephemeral=True)
        except Exception as e:
            _log.exception("export_failed", err=str(e))
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

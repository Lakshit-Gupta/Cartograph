"""/digest now | preview | schedule — daily digest controls."""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands

from src.common import db
from src.common.logger import get_logger
from src.common.queue import RedisQ, Streams

_log = get_logger(__name__)


def setup(bot) -> None:  # type: ignore[no-untyped-def]
    group = app_commands.Group(name="digest", description="Daily digest controls.")

    @group.command(name="now", description="Force the digest to run right now.")
    async def now(interaction: discord.Interaction):
        try:
            q = await RedisQ.connect()
            await q.publish(Streams.NOTIFY, {"kind": "digest", "force_digest": True, "user_id": 1})
            await interaction.response.send_message("Digest queued.", ephemeral=True)
        except Exception as e:
            _log.exception("digest_now_failed", err=str(e))
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

    @group.command(name="preview", description="Show what would land in the next digest.")
    async def preview(interaction: discord.Interaction):
        try:
            rows = await db.fetch_all(
                """
                SELECT o.title, o.company, s.score
                FROM opportunities o
                JOIN opportunity_scores s ON s.opportunity_id = o.id
                WHERE s.user_id = 1
                  AND o.state IN ('ranked','digested')
                  AND o.first_seen > NOW() - INTERVAL '36 hours'
                ORDER BY s.score DESC
                LIMIT 10
                """
            )
            if not rows:
                await interaction.response.send_message("Nothing queued.", ephemeral=True)
                return
            lines = [f"{i + 1}. {r['title']} — {r['company'] or '—'} (score {r['score']:.2f})" for i, r in enumerate(rows)]
            await interaction.response.send_message("\n".join(lines), ephemeral=True)
        except Exception as e:
            _log.exception("digest_preview_failed", err=str(e))
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

    @group.command(name="schedule", description="Set digest send time (HHMM, 24h, local TZ).")
    @app_commands.describe(hhmm="HHMM, e.g. 0830")
    async def schedule(interaction: discord.Interaction, hhmm: str):
        try:
            if len(hhmm) != 4 or not hhmm.isdigit():
                raise ValueError("expected HHMM digits")
            local_hh, local_mm = int(hhmm[:2]), int(hhmm[2:])
            if not (0 <= local_hh < 24 and 0 <= local_mm < 60):
                raise ValueError("out of range")

            # Read user timezone, convert local HHMM → UTC.
            tz_rec = await db.fetch_one("SELECT timezone FROM users WHERE id = 1")
            tz_name = tz_rec["timezone"] if tz_rec else "UTC"
            try:
                user_tz = ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                user_tz = ZoneInfo("UTC")
            today = datetime.now(user_tz).date()
            local_dt = datetime.combine(today, time(hour=local_hh, minute=local_mm), tzinfo=user_tz)
            utc_dt = local_dt.astimezone(UTC)

            await db.execute(
                """
                UPDATE users
                SET digest_hour_utc   = $1,
                    digest_minute_utc = $2,
                    digest_updated_at = NOW()
                WHERE id = 1
                """,
                utc_dt.hour,
                utc_dt.minute,
            )
            await interaction.response.send_message(
                f"Digest schedule set to {local_hh:02d}:{local_mm:02d} {tz_name} "
                f"(= {utc_dt.hour:02d}:{utc_dt.minute:02d} UTC). "
                f"Scheduler picks up the change within 60s.",
                ephemeral=True,
            )
        except Exception as e:
            _log.exception("digest_schedule_failed", err=str(e))
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

    bot.tree.add_command(group)

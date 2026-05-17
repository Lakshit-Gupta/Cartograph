"""/source list | pause | resume | add — source management."""
from __future__ import annotations

import discord
from discord import app_commands

from src.common import db
from src.common.logger import get_logger
from src.notifiers.discord.handlers.modals import SourceAddModal

_log = get_logger(__name__)


def setup(bot) -> None:  # type: ignore[no-untyped-def]
    group = app_commands.Group(name="source", description="Manage sources.")

    @group.command(name="list", description="List sources with their health.")
    @app_commands.describe(status_filter="Filter: active | paused | quarantined | all")
    async def list_(interaction: discord.Interaction, status_filter: str = "active"):
        try:
            params: tuple = ()
            sql = (
                "SELECT slug, category, status, opps_extracted_30d, last_successful_crawl_at "
                "FROM sources"
            )
            if status_filter and status_filter != "all":
                sql += " WHERE status = $1"
                params = (status_filter,)
            sql += " ORDER BY status, slug LIMIT 50"

            rows = await db.fetch_all(sql, *params)
            if not rows:
                await interaction.response.send_message("No sources match.", ephemeral=True)
                return
            lines = [
                f"`{r['slug']}` [{r['category']}/{r['status']}] — "
                f"opps30d={r['opps_extracted_30d']}"
                for r in rows
            ]
            chunks: list[str] = []
            cur = ""
            for line in lines:
                if len(cur) + len(line) + 1 > 1800:
                    chunks.append(cur)
                    cur = line
                else:
                    cur = f"{cur}\n{line}" if cur else line
            if cur:
                chunks.append(cur)
            await interaction.response.send_message(chunks[0], ephemeral=True)
            for c in chunks[1:]:
                await interaction.followup.send(c, ephemeral=True)
        except Exception as e:
            _log.exception("source_list_failed", err=str(e))
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

    @group.command(name="pause", description="Pause a source.")
    @app_commands.describe(slug="Source slug")
    async def pause(interaction: discord.Interaction, slug: str):
        try:
            await db.execute("UPDATE sources SET status = 'paused' WHERE slug = $1", slug)
            await interaction.response.send_message(f"Paused `{slug}`.", ephemeral=True)
        except Exception as e:
            _log.exception("source_pause_failed", err=str(e))
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

    @group.command(name="resume", description="Resume a paused/quarantined source.")
    @app_commands.describe(slug="Source slug")
    async def resume(interaction: discord.Interaction, slug: str):
        try:
            await db.execute("UPDATE sources SET status = 'active' WHERE slug = $1", slug)
            await interaction.response.send_message(f"Resumed `{slug}`.", ephemeral=True)
        except Exception as e:
            _log.exception("source_resume_failed", err=str(e))
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

    @group.command(name="add", description="Add a new source via modal.")
    @app_commands.describe(url="Source URL", lane="Lane (fulltime|internship|fellowship|freelance|contract)")
    async def add(interaction: discord.Interaction, url: str | None = None, lane: str | None = None):
        try:
            modal = SourceAddModal()
            if url:
                modal.url.default = url
            if lane:
                modal.lane.default = lane
            await interaction.response.send_modal(modal)
        except Exception as e:
            _log.exception("source_add_failed", err=str(e))
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

    bot.tree.add_command(group)

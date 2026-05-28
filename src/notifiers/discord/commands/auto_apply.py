"""/auto-apply slash command — batch Phase 4 auto-apply.

Two sub-commands:
  /auto-apply run [count]      — fire send_application on top-N matches.
  /auto-apply preview [count]  — same query, no fire; lists candidates.

The applier-worker still consumes `stream:apply` and runs the per-opp
policy gate, so every opp gets audited individually. The slash command
just decides BATCH-LEVEL caps (how many to enqueue at once) and surfaces
the summary to the user.

Cron-driven counterpart lives in src/workers/scheduler.py
(`emit_daily_auto_apply`) and calls the same `engine.dispatch()`.
"""

from __future__ import annotations

import discord
from discord import app_commands

from src.application.auto_apply_engine import dispatch, find_eligible
from src.common.db import current_tenant
from src.common.logger import get_logger

_log = get_logger(__name__)


def _ellipsis(s: str, n: int = 60) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def setup(bot) -> None:  # type: ignore[no-untyped-def]
    group = app_commands.Group(name="auto-apply", description="Phase 4 auto-apply batch operations")

    @group.command(name="run", description="Fire send_application on top-N matching opps.")
    @app_commands.describe(count="Optional: how many to fire (defaults to remaining daily cap).")
    async def auto_apply_run(interaction: discord.Interaction, count: int | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            summary = await dispatch(
                user_id=current_tenant(),
                requested_count=count,
                source="discord_slash",
            )
        except Exception as e:
            _log.exception("auto_apply_run_failed", err=str(e))
            await interaction.followup.send(f"Error: {e}", ephemeral=True)
            return

        skipped = ", ".join(f"{k}={v}" for k, v in summary.skipped_reasons.items()) or "—"
        dry_run_marker = " (DRY RUN)" if summary.dry_run else ""
        msg = (
            f"**/auto-apply{dry_run_marker}** fired={summary.fired_count} "
            f"candidates={summary.candidates_found} "
            f"daily={summary.daily_count_before}/{summary.daily_cap} "
            f"skipped: {skipped}"
        )
        await interaction.followup.send(msg, ephemeral=True)

    @group.command(name="preview", description="List opps that would be applied to (no fire).")
    @app_commands.describe(count="Optional: how many to preview (default 10).")
    async def auto_apply_preview(interaction: discord.Interaction, count: int | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            user_id = current_tenant()
            limit = count if count is not None else 10
            candidates = await find_eligible(user_id=user_id, limit=limit)
        except Exception as e:
            _log.exception("auto_apply_preview_failed", err=str(e))
            await interaction.followup.send(f"Error: {e}", ephemeral=True)
            return

        if not candidates:
            await interaction.followup.send(
                "No eligible opps found. Either auto_apply.enabled is false, "
                "no source is whitelisted, or no opps clear the filter/score gates.",
                ephemeral=True,
            )
            return

        lines = [
            f"`{c.score:.3f}`  **{_ellipsis(c.title)}** @ {_ellipsis(c.company, 30)}  ({c.source_slug})  `{str(c.opportunity_id)[:8]}…`"
            for c in candidates
        ]
        embed = discord.Embed(
            title=f"Auto-apply preview — {len(candidates)} candidate(s)",
            description="\n".join(lines),
            color=0x3498DB,
        )
        embed.set_footer(text="No applications fired. Run `/auto-apply run <count>` to actually apply.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    bot.tree.add_command(group)

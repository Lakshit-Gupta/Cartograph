"""Lane-split auto-apply slash commands — batch Phase 4 auto-apply.

Two command groups, one per vertical, so internships and jobs never share a
preview/run pool (they have different floors + submitters):

  /auto-apply-inter run [count]      — fire on top-N INTERNSHIP matches.
  /auto-apply-inter preview [count]  — list internship candidates (no fire).
  /auto-apply-job   run [count]      — fire on top-N JOB (full-time) matches.
  /auto-apply-job   preview [count]  — list job candidates (no fire).

Each group scopes the engine to its `OppCategory` (`internship` / `fulltime`)
via `find_eligible(category=...)` / `dispatch(category=...)`. The applier-worker
still runs the per-opp policy gate, so every opp is audited individually; the
slash command only decides batch-level caps and surfaces the summary.

Cron-driven counterpart lives in src/workers/scheduler.py
(`emit_daily_auto_apply`) and calls the same `engine.dispatch()` (un-scoped).
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


def _build_lane_group(*, group_name: str, category: str, lane_label: str) -> app_commands.Group:
    """Build one lane-scoped `/auto-apply-<lane>` group with run + preview."""
    group = app_commands.Group(name=group_name, description=f"Phase 4 auto-apply batch ops — {lane_label}")

    @group.command(name="run", description=f"Fire send_application on top-N matching {lane_label} opps.")
    @app_commands.describe(count="Optional: how many to fire (defaults to remaining daily cap).")
    async def auto_apply_run(interaction: discord.Interaction, count: int | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            summary = await dispatch(
                user_id=current_tenant(),
                requested_count=count,
                source="discord_slash",
                category=category,
            )
        except Exception as e:
            _log.exception("auto_apply_run_failed", err=str(e), lane=lane_label)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)
            return

        skipped = ", ".join(f"{k}={v}" for k, v in summary.skipped_reasons.items()) or "—"
        dry_run_marker = " (DRY RUN)" if summary.dry_run else ""
        msg = (
            f"**/{group_name}{dry_run_marker}** fired={summary.fired_count} "
            f"candidates={summary.candidates_found} "
            f"daily={summary.daily_count_before}/{summary.daily_cap} "
            f"skipped: {skipped}"
        )
        await interaction.followup.send(msg, ephemeral=True)

    @group.command(name="preview", description=f"List {lane_label} opps that would be applied to (no fire).")
    @app_commands.describe(count="Optional: how many to preview (default 10).")
    async def auto_apply_preview(interaction: discord.Interaction, count: int | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            user_id = current_tenant()
            limit = count if count is not None else 10
            candidates = await find_eligible(user_id=user_id, limit=limit, category=category)
        except Exception as e:
            _log.exception("auto_apply_preview_failed", err=str(e), lane=lane_label)
            await interaction.followup.send(f"Error: {e}", ephemeral=True)
            return

        if not candidates:
            await interaction.followup.send(
                f"No eligible {lane_label} opps found. Either auto_apply.enabled is false, "
                f"the {lane_label} source isn't whitelisted, or none clear the filter/score gates.",
                ephemeral=True,
            )
            return

        lines = [
            f"`{c.score:.3f}`  **{_ellipsis(c.title)}** @ {_ellipsis(c.company, 30)}  ({c.source_slug})  `{str(c.opportunity_id)[:8]}…`"
            for c in candidates
        ]
        embed = discord.Embed(
            title=f"Auto-apply {lane_label} preview — {len(candidates)} candidate(s)",
            description="\n".join(lines),
            color=0x3498DB,
        )
        embed.set_footer(text=f"No applications fired. Run `/{group_name} run <count>` to actually apply.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    return group


def setup(bot) -> None:  # type: ignore[no-untyped-def]
    bot.tree.add_command(_build_lane_group(group_name="auto-apply-inter", category="internship", lane_label="internship"))
    bot.tree.add_command(_build_lane_group(group_name="auto-apply-job", category="fulltime", lane_label="job"))

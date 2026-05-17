"""/explain <opp_id> — show the score breakdown for an opp."""
from __future__ import annotations

import json
from uuid import UUID

import discord
from discord import app_commands

from src.common import db
from src.common.logger import get_logger
from src.notifiers.discord import voice

_log = get_logger(__name__)


def setup(bot) -> None:  # type: ignore[no-untyped-def]
    @bot.tree.command(name="explain", description="Show why an opp matched (score breakdown).")
    @app_commands.describe(opp_id="UUID of the opportunity")
    async def explain_cmd(interaction: discord.Interaction, opp_id: str):
        try:
            uid = str(UUID(opp_id))
        except ValueError:
            await interaction.response.send_message(
                f"`{opp_id}` is not a valid UUID.", ephemeral=True
            )
            return
        try:
            row = await db.fetch_one(
                """
                SELECT score, score_components, ranker_version, scored_at
                FROM opportunity_scores
                WHERE opportunity_id = $1
                ORDER BY scored_at DESC
                LIMIT 1
                """,
                UUID(uid),
            )
            if not row:
                await interaction.response.send_message(
                    "No score for that opp yet.", ephemeral=True
                )
                return
            comps = row["score_components"]
            if isinstance(comps, str):
                try:
                    comps = json.loads(comps)
                except Exception:
                    comps = {}
            parts = ", ".join(f"{k}={v:.2f}" for k, v in (comps or {}).items())
            intro = voice.pick("explain_intro")
            await interaction.response.send_message(
                f"{intro} **score {row['score']:.2f}** ({row['ranker_version']}) — {parts}",
                ephemeral=True,
            )
        except Exception as e:
            _log.exception("explain_failed", err=str(e), opp_id=uid)
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

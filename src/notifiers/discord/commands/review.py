"""/review — v2 dark-source discovery candidates.

Phase 1 stub: surfaces an explanatory message. Phase 3 wires this to the
`candidate_sources` table populated by the discovery worker.
"""

from __future__ import annotations

import discord

from src.common.logger import get_logger

_log = get_logger(__name__)

_STUB_MSG = (
    "**/review** is a v2 feature.\n"
    "Phase 3 will populate `candidate_sources` from the discovery worker "
    "(Google dorking + Reddit + HN + GitHub awesome-lists + Common Crawl)."
    " For now: add sources manually via `/source add`."
)


def setup(bot) -> None:  # type: ignore[no-untyped-def]
    @bot.tree.command(name="review", description="(v2) Review dark-source candidates.")
    async def review_cmd(interaction: discord.Interaction):
        try:
            await interaction.response.send_message(_STUB_MSG, ephemeral=True)
        except Exception as e:
            _log.exception("review_failed", err=str(e))
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

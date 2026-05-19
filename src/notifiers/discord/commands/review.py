"""/review — Phase 3.2 dark-source candidate review.

Lists `candidate_sources WHERE status='pending'` paginated 10 per page. Each
candidate is one field on a single embed. Approve / Reject / Snooze buttons
mutate the candidate row + (on approve) materialise a `sources` row.

When the queue is empty: returns an "all caught up" ephemeral message so the
command works regardless of whether the discovery worker has produced
anything yet.
"""

from __future__ import annotations

import discord
from discord import app_commands

from src.common import db
from src.common.logger import get_logger
from src.notifiers.discord.handlers.buttons import CandidateReviewView

_log = get_logger(__name__)

PAGE_SIZE = 10


async def _fetch_pending(offset: int, limit: int) -> list:
    return await db.fetch_all(
        """
        SELECT id, url, title, snippet, discovered_via,
               classifier_confidence, classifier_category, classifier_rationale,
               created_at
        FROM candidate_sources
        WHERE status = 'pending'
        ORDER BY classifier_confidence DESC NULLS LAST, created_at DESC
        LIMIT $1 OFFSET $2
        """,
        limit,
        offset,
    )


async def _count_pending() -> int:
    rec = await db.fetch_one("SELECT COUNT(*)::int AS n FROM candidate_sources WHERE status = 'pending'")
    return int(rec["n"]) if rec else 0


def _build_embed(rows: list, total: int, offset: int) -> discord.Embed:
    """One embed per page. Each candidate is one field. Title shows page x/y."""
    page = (offset // PAGE_SIZE) + 1
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    embed = discord.Embed(
        title=f"Dark-source candidates — page {page}/{pages}",
        description=f"{total} pending. Approve / Reject / Snooze with the buttons.",
        color=0x9B59B6,
    )
    for r in rows:
        conf = r["classifier_confidence"]
        conf_s = f"{conf:.2f}" if conf is not None else "n/a"
        cat = r["classifier_category"] or "?"
        via = r["discovered_via"] or "?"
        title = (r["title"] or r["url"])[:80]
        snippet = (r["classifier_rationale"] or r["snippet"] or "")[:150]
        embed.add_field(
            name=f"#{r['id']} • {title}",
            value=(f"`{r['url']}`\ncategory=`{cat}` confidence=`{conf_s}` via=`{via}`\n_{snippet}_")[:1024],
            inline=False,
        )
    return embed


def setup(bot) -> None:  # type: ignore[no-untyped-def]
    @bot.tree.command(name="review", description="Review dark-source discovery candidates.")
    @app_commands.describe(page="Page number (1-indexed). Defaults to 1.")
    async def review_cmd(interaction: discord.Interaction, page: int = 1) -> None:
        try:
            total = await _count_pending()
            if total == 0:
                await interaction.response.send_message(
                    "All caught up — no candidate sources pending review.\n"
                    "The discovery worker runs Sundays at 04:00 IST. "
                    "Enable it via `MP_DARK_SOURCE_DISCOVERY_ENABLED=true`.",
                    ephemeral=True,
                )
                return
            page = max(1, page)
            offset = (page - 1) * PAGE_SIZE
            rows = await _fetch_pending(offset, PAGE_SIZE)
            if not rows:
                await interaction.response.send_message(
                    f"Page {page} is empty — only {total} candidate(s) pending.",
                    ephemeral=True,
                )
                return
            embed = _build_embed(rows, total=total, offset=offset)
            # Per-candidate-row button rows would exceed Discord's 5-row limit
            # for embeds with 10 fields. Instead we attach ONE view with a
            # candidate-id picker per page. Each button uses the first matching
            # candidate; for the rest the user re-runs /review or uses
            # /review id:<n>. Simplest UX that fits Discord's 5-component
            # constraint while still letting the operator action each row.
            view = CandidateReviewView(candidate_ids=[int(r["id"]) for r in rows])
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            _log.exception("review_failed", err=str(e))
            try:
                await interaction.response.send_message(f"Error: {e}", ephemeral=True)
            except discord.InteractionResponded:
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

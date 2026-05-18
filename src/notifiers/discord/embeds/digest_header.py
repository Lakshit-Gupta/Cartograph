"""Header embed posted at the top of each daily digest run."""

from __future__ import annotations

from datetime import UTC, date, datetime

import discord

from src.notifiers.discord import voice
from src.notifiers.discord.routing import DEFAULT_COLOR


def build_digest_header(
    when: date | datetime,
    count: int,
    top_score: float | None = None,
    *,
    color: int = DEFAULT_COLOR,
) -> discord.Embed:
    title = voice.pick("daily_digest_header")
    if isinstance(when, datetime):
        date_str = when.date().isoformat()
    else:
        date_str = when.isoformat()

    embed = discord.Embed(
        title=f"{title} — {date_str}",
        color=discord.Color(color),
        timestamp=datetime.now(UTC),
    )
    embed.add_field(name="opps", value=str(count), inline=True)
    embed.add_field(
        name="top score",
        value=(f"{top_score:.2f}" if top_score is not None else "—"),
        inline=True,
    )
    if count == 0:
        embed.description = voice.pick("daily_digest_empty")
    return embed

"""Priority-push embed — red border, ⚡ prefix, used for freelance hot leads
and time-sensitive opps that score above the priority threshold."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import discord

from src.notifiers.discord import voice
from src.notifiers.discord.embeds.opp_card import _comp, _truncate

_RED = 0xEF4444


def build_priority_push(
    opp_row: dict[str, Any],
    *,
    score: float | None = None,
    reason: str | None = None,
) -> discord.Embed:
    title = (opp_row.get("title") or "(untitled)").strip()
    company = opp_row.get("company") or "—"
    url = opp_row.get("canonical_url") or opp_row.get("apply_url")

    header = voice.pick("priority_push_header")
    embed = discord.Embed(
        title=f"⚡ {title}"[:256],
        url=url,
        description=_truncate(opp_row.get("description"), n=400),
        color=discord.Color(_RED),
        timestamp=datetime.now(UTC),
    )
    embed.set_author(name=f"{header} — {company}"[:256])

    embed.add_field(name="comp", value=_comp(opp_row), inline=True)
    embed.add_field(name="location", value=(opp_row.get("location") or "—")[:1024], inline=True)
    embed.add_field(
        name="remote",
        value=(opp_row.get("remote_type") or "unspecified"),
        inline=True,
    )
    if score is not None:
        embed.add_field(name="score", value=f"{score:.2f}", inline=True)
    if reason:
        embed.add_field(name="why", value=reason[:1024], inline=False)
    embed.set_footer(text=f"opp_id={opp_row.get('id', '?')} — act fast")
    return embed

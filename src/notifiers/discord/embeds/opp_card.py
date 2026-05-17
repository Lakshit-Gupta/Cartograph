"""Standard opportunity embed used in lane forums + tracker threads."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import discord

from src.notifiers.discord.routing import color_for_lane

_MAX_DESC = 600


def _age(posted_at: Any) -> str:
    if not posted_at:
        return "—"
    if isinstance(posted_at, str):
        try:
            posted_at = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        except ValueError:
            return posted_at
    if not isinstance(posted_at, datetime):
        return str(posted_at)
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - posted_at
    secs = int(delta.total_seconds())
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _comp(opp: dict[str, Any]) -> str:
    lo = opp.get("comp_min")
    hi = opp.get("comp_max")
    cur = opp.get("comp_currency") or ""
    period = opp.get("comp_period") or ""
    if lo is None and hi is None:
        return "—"
    if lo is not None and hi is not None and lo != hi:
        return f"{cur}{lo:g}–{cur}{hi:g}/{period}".strip("/")
    val = lo if lo is not None else hi
    return f"{cur}{val:g}/{period}".strip("/")


def _truncate(text: str | None, n: int = _MAX_DESC) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= n:
        return text
    return text[:n].rstrip() + "…"


def build_opp_card(
    opp_row: dict[str, Any],
    *,
    score: float | None = None,
    score_components: dict[str, float] | None = None,
) -> discord.Embed:
    """Build the canonical opportunity embed.

    `opp_row` is expected to be a dict (asyncpg.Record converted via dict()).
    """
    title = opp_row.get("title") or "(untitled)"
    company = opp_row.get("company") or "—"
    category = (opp_row.get("category") or "unknown").lower()
    url = opp_row.get("canonical_url") or opp_row.get("apply_url") or None
    color = color_for_lane(category)

    embed = discord.Embed(
        title=title[:256],
        url=url,
        description=_truncate(opp_row.get("description")),
        color=discord.Color(color),
        timestamp=datetime.now(UTC),
    )
    embed.set_author(name=company[:256])

    embed.add_field(name="comp", value=_comp(opp_row), inline=True)
    embed.add_field(
        name="location",
        value=(opp_row.get("location") or "—")[:1024],
        inline=True,
    )
    embed.add_field(
        name="remote",
        value=(opp_row.get("remote_type") or "unspecified"),
        inline=True,
    )
    embed.add_field(name="posted", value=_age(opp_row.get("posted_at")), inline=True)
    embed.add_field(name="category", value=category, inline=True)
    if score is not None:
        embed.add_field(name="score", value=f"{score:.2f}", inline=True)
    if score_components:
        breakdown = ", ".join(f"{k}={v:.2f}" for k, v in list(score_components.items())[:6])
        embed.set_footer(text=breakdown[:2048])
    else:
        opp_id = opp_row.get("id")
        if opp_id:
            embed.set_footer(text=f"opp_id={opp_id}")
    return embed

"""Embed for `applied` NOTIFY kind.

Posted as the thread starter in #✅-applied after `src/application/sender.py`
fires an EMAIL application. The forum thread itself is created by the caller
(`Bot._post_applied`); this module just renders the embed.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import discord

_GREEN = 0x10B981


def _truncate(text: str | None, n: int) -> str:
    if not text:
        return "—"
    text = str(text).strip()
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + "…"


def thread_title(title: str | None, company: str | None) -> str:
    """`<title> @ <company>`, total length ≤ 90 chars."""
    t = (title or "(untitled)").strip()
    c = (company or "—").strip()
    full = f"{t} @ {c}"
    if len(full) <= 90:
        return full
    # Trim title first — keep company readable.
    spare = 90 - len(c) - 3  # ' @ '
    if spare < 10:
        return full[:90]
    return f"{t[: spare - 1].rstrip()}… @ {c}"


def build_applied(payload: dict[str, Any]) -> discord.Embed:
    """Build the green confirmation embed for a sent application."""
    title = payload.get("title") or "(untitled)"
    company = payload.get("company") or "—"
    method = payload.get("method") or "?"
    target = payload.get("target") or "—"
    sent_at = payload.get("sent_at") or datetime.now(UTC).isoformat()
    application_id = payload.get("application_id")

    embed = discord.Embed(
        title=_truncate(f"{title} @ {company}", 256),
        color=discord.Color(_GREEN),
        timestamp=datetime.now(UTC),
    )
    embed.set_author(name=_truncate(str(company), 256))
    embed.add_field(name="Method", value=_truncate(str(method), 1024), inline=True)
    embed.add_field(name="Target", value=_truncate(str(target), 1024), inline=True)
    embed.add_field(name="Sent At", value=_truncate(str(sent_at), 1024), inline=False)
    if application_id is not None:
        embed.add_field(
            name="Application ID",
            value=_truncate(str(application_id), 1024),
            inline=True,
        )
    embed.set_footer(text="Logged. Outcome tracked via Gmail.")
    return embed


def build_view(apply_url: str | None) -> discord.ui.View | None:
    """A simple "View opp" link button. Returns None if no URL."""
    if not apply_url:
        return None
    view = discord.ui.View(timeout=None)
    try:
        view.add_item(
            discord.ui.Button(
                label="View opp",
                style=discord.ButtonStyle.link,
                url=_truncate(str(apply_url), 512) if len(str(apply_url)) <= 512 else str(apply_url)[:512],
            )
        )
    except Exception:
        return None
    return view


__all__ = ["build_applied", "build_view", "thread_title"]

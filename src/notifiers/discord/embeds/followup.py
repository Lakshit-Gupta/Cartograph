"""Embed builder for the Phase 2.3 follow-up flow.

The Discord notifier consumes ``kind=followup_ready`` from Streams.NOTIFY
and dispatches here. The user sees one embed per draft with three
buttons (Send / Edit / Skip) — implemented in
``src/notifiers/discord/handlers/buttons.py::FollowupActionView``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import discord

_FOLLOWUP_COLOR = 0xF59E0B  # amber — middle of the urgency scale
_BODY_PREVIEW_MAX = 1500
_TITLE_MAX = 80


def _truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def build_followup_ready(payload: dict[str, Any]) -> discord.Embed:
    """Render the draft for review. Caller attaches a FollowupActionView."""
    title = payload.get("title") or "(untitled)"
    company = payload.get("company") or "—"
    days_silent = payload.get("days_silent")
    body = payload.get("body_markdown") or ""
    target = payload.get("target") or "(no recipient on file)"

    embed = discord.Embed(
        title=_truncate(f"Follow-up draft — {title} @ {company}", _TITLE_MAX),
        description=_truncate(body, _BODY_PREVIEW_MAX),
        color=_FOLLOWUP_COLOR,
        timestamp=datetime.now(UTC),
    )

    embed.add_field(name="Reply to", value=str(target)[:200], inline=False)
    if days_silent is not None:
        embed.add_field(name="Silent for", value=f"{int(days_silent)} day(s)", inline=True)
    word_count = len([w for w in body.split() if w])
    embed.add_field(name="Words", value=str(word_count), inline=True)
    embed.set_footer(text="Send → fires via Resend (threaded). Edit → modal. Skip → no email.")
    return embed


def thread_title(opp_title: str | None, company: str | None) -> str:
    base = f"Follow-up — {opp_title or '(untitled)'} @ {company or '—'}"
    return base[:90]

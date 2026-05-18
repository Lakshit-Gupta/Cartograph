"""Reaction-based shortcuts. Emoji on an opp embed = button action.

Routed identically to clicking the corresponding button.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import discord

from src.common.logger import get_logger
from src.common.queue import RedisQ, Streams
from src.notifiers.discord.handlers.buttons import _enqueue, _transition_state

_log = get_logger(__name__)

# emoji → action mapping
_EMOJI_MAP: dict[str, str] = {
    "✅": "apply",
    "❌": "skip",
    "🔖": "pin",
    "💬": "explain",
    "🔁": "snooze",
}

_OPP_ID_RE = re.compile(r"opp_id=([0-9a-fA-F-]{36})")


async def handle_raw_reaction_add(
    payload: discord.RawReactionActionEvent,
    bot: discord.Client,
) -> None:
    if payload.user_id == (bot.user.id if bot.user else 0):
        return
    emoji = str(payload.emoji)
    action = _EMOJI_MAP.get(emoji)
    if not action:
        return

    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(payload.channel_id)
        except Exception:
            return

    try:
        message = await channel.fetch_message(payload.message_id)  # type: ignore[attr-defined]
    except Exception as e:
        _log.warning("reaction_fetch_msg_failed", err=str(e))
        return

    opp_id = _extract_opp_id(message)
    if not opp_id:
        return

    user_id = 1  # solo phase
    try:
        if action == "apply":
            await _transition_state(opp_id, "applied")
            await _enqueue("apply", opp_id, user_id, source="reaction")
        elif action == "skip":
            await _transition_state(opp_id, "seen")
            await _enqueue("skip", opp_id, user_id, source="reaction")
        elif action == "snooze":
            await _transition_state(opp_id, "snoozed")
            await _enqueue("snooze", opp_id, user_id, days=1, source="reaction")
        elif action == "pin":
            await _enqueue("pin", opp_id, user_id, source="reaction")
        elif action == "explain":
            # explain over reactions = quietly publish for an explainer DM
            q = await RedisQ.connect()
            await q.publish(
                Streams.NOTIFY,
                {
                    "kind": "explain_dm",
                    "user_id": user_id,
                    "opp_id": opp_id,
                    "channel_id": payload.channel_id,
                },
            )
    except Exception as e:
        _log.exception("reaction_dispatch_failed", err=str(e), action=action, opp_id=opp_id)


def _extract_opp_id(message: discord.Message) -> str | None:
    # Footer pattern preferred.
    for emb in message.embeds:
        if emb.footer and emb.footer.text:
            m = _OPP_ID_RE.search(emb.footer.text)
            if m:
                return m.group(1)
    # Fallback to "opp_id=" in main content.
    if message.content:
        m = _OPP_ID_RE.search(message.content)
        if m:
            return m.group(1)
    return None


# Re-exported names so the bot can register cleanly.
__all__ = ["handle_raw_reaction_add"]


def _ensure_uuid(s: str) -> str:
    """Best-effort validation."""
    try:
        return str(UUID(s))
    except ValueError:
        return s


_ = Any  # keep import used for stable diffing

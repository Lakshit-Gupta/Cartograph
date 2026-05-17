"""Buttons under each opportunity embed.

Each button publishes the user's intent onto `stream:apply`. The
appliers worker decides what to do (transition state, fire Resend, etc.).
This keeps the Discord gateway thread snappy.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import discord

from src.common import db
from src.common.logger import get_logger
from src.common.queue import RedisQ, Streams
from src.notifiers.discord import voice

_log = get_logger(__name__)


def _custom_id(action: str, opp_id: str | UUID) -> str:
    return f"opp:{action}:{opp_id}"


async def _enqueue(action: str, opp_id: str, user_id: int, **extra: Any) -> None:
    q = await RedisQ.connect()
    payload = {
        "action": action,
        "opp_id": str(opp_id),
        "user_id": user_id,
        "ts": datetime.now(UTC).isoformat(),
        **extra,
    }
    await q.publish(Streams.APPLY, payload)


async def _transition_state(opp_id: str, target_state: str) -> bool:
    """Direct DB transition — relies on V004 trigger to validate + audit."""
    try:
        await db.execute(
            "UPDATE opportunities SET state = $2 WHERE id = $1",
            UUID(opp_id),
            target_state,
        )
        return True
    except Exception as e:
        _log.warning("transition_failed", opp_id=opp_id, target=target_state, err=str(e))
        return False


class OppActionView(discord.ui.View):
    """Persistent view with Apply / Skip / Snooze / Pin / Explain buttons."""

    def __init__(self, opp_id: str | UUID, *, timeout: float | None = None):
        super().__init__(timeout=timeout)
        opp = str(opp_id)
        # We register all 5 buttons with stable custom_ids so the View can be
        # rebuilt on bot restart and still respond.
        self.add_item(_btn("Apply",   "apply",   opp, discord.ButtonStyle.success, "✅"))
        self.add_item(_btn("Skip",    "skip",    opp, discord.ButtonStyle.secondary, "❌"))
        self.add_item(_btn("Snooze",  "snooze",  opp, discord.ButtonStyle.secondary, "🔁"))
        self.add_item(_btn("Pin",     "pin",     opp, discord.ButtonStyle.primary,   "🔖"))
        self.add_item(_btn("Explain", "explain", opp, discord.ButtonStyle.secondary, "💬"))


class OppReviewView(discord.ui.View):
    """Persistent view attached to `manual_apply_ready` review threads.

    Two buttons: Mark applied → state stays at 'applied' (already set by
    sender.py via the V004 trigger); Cancel → state back to 'digested'.
    Both publish onto Streams.APPLY so the applier worker handles auditing.
    """

    def __init__(self, opp_id: str | UUID, *, timeout: float | None = None):
        super().__init__(timeout=timeout)
        opp = str(opp_id)
        self.add_item(_btn("Mark applied", "apply",  opp, discord.ButtonStyle.success,   "✅"))
        self.add_item(_btn("Cancel",       "cancel", opp, discord.ButtonStyle.secondary, "↩️"))


def _btn(
    label: str,
    action: str,
    opp_id: str,
    style: discord.ButtonStyle,
    emoji: str | None = None,
) -> discord.ui.Button:
    button = discord.ui.Button(
        label=label,
        style=style,
        custom_id=_custom_id(action, opp_id),
        emoji=emoji,
    )

    async def _cb(interaction: discord.Interaction) -> None:
        await dispatch_button(interaction)

    button.callback = _cb  # type: ignore[assignment]
    return button


async def dispatch_button(interaction: discord.Interaction) -> None:
    """Single entry point — parses custom_id and acts."""
    raw = interaction.data.get("custom_id", "") if interaction.data else ""
    try:
        _, action, opp_id = raw.split(":", 2)
    except ValueError:
        await _ephemeral(interaction, f"Bad button id: `{raw}`")
        return

    user_id = 1  # solo phase; Phase 4 resolves from discord user → users.id
    try:
        if action == "apply":
            await _transition_state(opp_id, "applied")
            await _enqueue("apply", opp_id, user_id, source="button")
            await _ephemeral(interaction, voice.pick("applied_confirm"))
        elif action == "skip":
            await _transition_state(opp_id, "seen")
            await _enqueue("skip", opp_id, user_id, source="button")
            await _ephemeral(interaction, voice.pick("skipped_confirm"))
        elif action == "snooze":
            await _enqueue("snooze", opp_id, user_id, days=1, source="button")
            await _transition_state(opp_id, "snoozed")
            await _ephemeral(interaction, voice.pick("snoozed_confirm"))
        elif action == "pin":
            await _enqueue("pin", opp_id, user_id, source="button")
            await _ephemeral(interaction, voice.pick("pinned_confirm"))
        elif action == "explain":
            text = await _explain_text(opp_id)
            await _ephemeral(interaction, text)
        elif action == "cancel":
            await _transition_state(opp_id, "digested")
            await _enqueue("cancel_apply", opp_id, user_id, source="button")
            await _ephemeral(interaction, "Cancelled. Back to digested.")
        else:
            await _ephemeral(interaction, f"Unknown action `{action}`.")
    except Exception as e:
        _log.exception("button_dispatch_failed", err=str(e), action=action, opp_id=opp_id)
        await _ephemeral(interaction, f"Error: {e}")


async def _explain_text(opp_id: str) -> str:
    row = await db.fetch_one(
        """
        SELECT s.score, s.score_components
        FROM opportunity_scores s
        WHERE s.opportunity_id = $1
        ORDER BY s.scored_at DESC
        LIMIT 1
        """,
        UUID(opp_id),
    )
    if not row:
        return "No score yet for this opp."
    comps = row["score_components"]
    if isinstance(comps, str):
        try:
            comps = json.loads(comps)
        except Exception:
            comps = {}
    parts = ", ".join(f"{k}={v:.2f}" for k, v in (comps or {}).items())
    intro = voice.pick("explain_intro")
    return f"{intro} score={row['score']:.2f} ({parts})"


async def _ephemeral(interaction: discord.Interaction, msg: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception as e:
        _log.warning("ephemeral_send_failed", err=str(e))

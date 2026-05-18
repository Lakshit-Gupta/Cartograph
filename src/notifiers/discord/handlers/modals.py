"""Modals — multi-field user input. Used by freelance proposal flow,
manual source add, and budget edit shortcuts.
"""

from __future__ import annotations

from typing import Any

import discord

from src.common import db
from src.common.logger import get_logger
from src.common.queue import RedisQ, Streams

_log = get_logger(__name__)


class ProposalEditModal(discord.ui.Modal, title="Freelance proposal"):
    """Used in the freelance speed lane — drafted by LLM, edited by user, then sent."""

    pitch = discord.ui.TextInput(
        label="Pitch (1–2 sentences)",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=600,
    )
    rate = discord.ui.TextInput(
        label="Rate (e.g. $50/hr or ₹4000/day)",
        required=True,
        max_length=64,
    )
    cta = discord.ui.TextInput(
        label="Call to action",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=200,
    )

    def __init__(self, opp_id: str, prefill: dict[str, str] | None = None):
        super().__init__()
        self.opp_id = opp_id
        if prefill:
            self.pitch.default = prefill.get("pitch", "")[:600]
            self.rate.default = prefill.get("rate", "")[:64]
            self.cta.default = prefill.get("cta", "")[:200]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        q = await RedisQ.connect()
        await q.publish(
            Streams.APPLY,
            {
                "action": "freelance_send_proposal",
                "opp_id": self.opp_id,
                "user_id": 1,
                "pitch": str(self.pitch.value),
                "rate": str(self.rate.value),
                "cta": str(self.cta.value),
                "source": "modal",
            },
        )
        await interaction.response.send_message("Proposal queued.", ephemeral=True)


class SourceAddModal(discord.ui.Modal, title="Add a source"):
    url = discord.ui.TextInput(label="Source URL", required=True, max_length=512)
    lane = discord.ui.TextInput(
        label="Lane (fulltime|internship|fellowship|freelance|contract)",
        required=True,
        max_length=32,
    )
    notes = discord.ui.TextInput(
        label="Notes (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=400,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            await db.execute(
                """
                INSERT INTO sources (slug, name, category, base_url, crawler_strategy, created_via, notes)
                VALUES ($1, $2, 'other', $3, 'manual', 'discord_modal', $4)
                ON CONFLICT (slug) DO NOTHING
                """,
                _slug_from_url(str(self.url.value)),
                str(self.url.value),
                str(self.url.value),
                str(self.notes.value or ""),
            )
            await interaction.response.send_message(
                f"Source queued for review: `{self.url.value}` (lane={self.lane.value})",
                ephemeral=True,
            )
        except Exception as e:
            _log.exception("source_add_modal_failed", err=str(e))
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)


class BudgetSetModal(discord.ui.Modal, title="Set comp floor"):
    min_intern = discord.ui.TextInput(label="Intern floor (₹/mo)", required=False, max_length=20)
    min_ft = discord.ui.TextInput(label="Full-time floor (₹/mo)", required=False, max_length=20)
    min_freelance_usd_hr = discord.ui.TextInput(label="Freelance floor ($/hr)", required=False, max_length=20)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        payload: dict[str, Any] = {}
        for field_name, ui_field in (
            ("min_intern", self.min_intern),
            ("min_ft", self.min_ft),
            ("min_freelance_usd_hr", self.min_freelance_usd_hr),
        ):
            v = (ui_field.value or "").strip()
            if v:
                try:
                    payload[field_name] = float(v)
                except ValueError:
                    continue
        if not payload:
            await interaction.response.send_message("No fields set.", ephemeral=True)
            return
        q = await RedisQ.connect()
        await q.publish(Streams.APPLY, {"action": "budget_set", "user_id": 1, **payload})
        await interaction.response.send_message(f"Budget updated: `{payload}`", ephemeral=True)


def _slug_from_url(url: str) -> str:
    """Crude URL → slug. Worker will dedup."""
    s = url.lower()
    for p in ("https://", "http://", "www."):
        if s.startswith(p):
            s = s[len(p) :]
    return s.split("/", 1)[0].replace(".", "_")[:64] or "manual_source"

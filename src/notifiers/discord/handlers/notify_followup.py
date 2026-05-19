"""Handler for `kind=followup_ready` notify payloads (Phase 2.3)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord

from src.common.logger import get_logger
from src.common.metrics import deliver_success_total
from src.notifiers.discord.embeds.followup import build_followup_ready, thread_title
from src.notifiers.discord.handlers.buttons import FollowupActionView
from src.notifiers.discord.routing import channel_id_for

if TYPE_CHECKING:
    from src.notifiers.discord.bot import Bot

_log = get_logger(__name__)


async def post_followup_ready(bot: Bot, payload: dict[str, Any]) -> None:
    """Phase 2.3 — surface the LLM-drafted follow-up with Send/Edit/Skip."""
    try:
        data = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
        data = {**payload, **(data or {})}

        followup_id = data.get("followup_id")
        if not followup_id:
            _log.warning("followup_ready_no_id", payload=payload)
            return

        embed = build_followup_ready(data)
        view = FollowupActionView(followup_id=int(followup_id))

        chan_id = channel_id_for("applied")
        chan = await bot._resolve_channel(chan_id)
        if chan is None:
            _log.warning("followup_channel_missing")
            return

        name = thread_title(data.get("title"), data.get("company"))
        if isinstance(chan, discord.ForumChannel):
            await chan.create_thread(name=name, embed=embed, view=view)
        else:
            msg = await chan.send(embed=embed, view=view)
            try:
                await msg.create_thread(name=name[:100])
            except Exception as e:
                _log.warning("followup_thread_create_fallback_failed", err=str(e))
        deliver_success_total.labels(channel="followup").inc()
    except Exception as e:
        _log.exception("post_followup_ready_failed", err=str(e))
        raise

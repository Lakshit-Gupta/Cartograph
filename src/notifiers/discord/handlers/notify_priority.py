"""Handler for `kind=priority_push` notify payloads."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.common.logger import get_logger
from src.common.metrics import deliver_success_total
from src.notifiers.discord import voice
from src.notifiers.discord.embeds.priority_push import build_priority_push
from src.notifiers.discord.handlers.buttons import OppActionView
from src.notifiers.discord.routing import channel_id_for

if TYPE_CHECKING:
    from src.notifiers.discord.bot import Bot

_log = get_logger(__name__)


async def post_priority(bot: Bot, payload: dict[str, Any]) -> None:
    opp = payload.get("opp") or payload
    chan_id = channel_id_for("priority_push")
    chan = await bot._resolve_channel(chan_id)
    if chan is None:
        _log.warning("priority_channel_missing")
        return
    embed = build_priority_push(opp, score=payload.get("score"), reason=payload.get("reason"))
    view = OppActionView(opp_id=str(opp.get("id") or payload.get("opportunity_id")))
    await chan.send(content=voice.pick("freelance_push"), embed=embed, view=view)
    deliver_success_total.labels(channel="priority").inc()

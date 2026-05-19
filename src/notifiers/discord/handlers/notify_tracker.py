"""Handler for `kind=tracker_update` notify payloads."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from src.common.metrics import deliver_success_total
from src.notifiers.discord.routing import route_for

if TYPE_CHECKING:
    from src.notifiers.discord.bot import Bot


async def post_tracker(bot: Bot, payload: dict[str, Any]) -> None:
    route = route_for({"tracker": payload.get("tracker", "applied")}, kind="tracker")
    chan = await bot._resolve_channel(route["channel_id"])
    if chan is None:
        return
    await chan.send(content=payload.get("message", json.dumps(payload, default=str))[:1900])
    deliver_success_total.labels(channel="tracker").inc()

"""Handler for `kind=alert` notify payloads."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.common.metrics import deliver_success_total
from src.notifiers.discord.routing import route_for

if TYPE_CHECKING:
    from src.notifiers.discord.bot import Bot


async def post_alert(bot: Bot, payload: dict[str, Any]) -> None:
    route = route_for({"alert": payload.get("alert")}, kind="alert")
    chan = await bot._resolve_channel(route["channel_id"])
    if chan is None:
        return
    msg = payload.get("message") or payload.get("alert") or "alert"
    content = f"@here {msg}" if route.get("mention_owner") else msg
    await chan.send(content=content)
    deliver_success_total.labels(channel="alerts").inc()

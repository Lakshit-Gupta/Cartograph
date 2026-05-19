"""Handler for `kind=opp|lane_post|ranked|digested` notify payloads."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.notifiers.discord.embeds.opp_card import build_opp_card
from src.notifiers.discord.handlers.buttons import OppActionView
from src.notifiers.discord.routing import route_for

if TYPE_CHECKING:
    from src.notifiers.discord.bot import Bot

from src.common.logger import get_logger

_log = get_logger(__name__)


async def post_opp(bot: Bot, payload: dict[str, Any]) -> None:
    opp = payload.get("opp") or payload
    route = route_for(opp, kind="lane")
    chan = await bot._resolve_channel(route["channel_id"])
    if chan is None:
        _log.warning("opp_channel_missing", route=route)
        return

    score = payload.get("score")
    score_components = payload.get("score_components") or {}
    embed = build_opp_card(opp, score=score, score_components=score_components)
    view = OppActionView(opp_id=str(opp.get("id") or payload.get("opportunity_id")))
    await bot._send_embed(chan, embed, view=view, route=route)

    # Priority push duplication when score exceeds per-lane threshold.
    try:
        if score is not None and float(score) >= route.get("push_threshold", 1.01):
            from src.notifiers.discord.handlers.notify_priority import post_priority

            await post_priority(bot, {"opp": opp, "score": score})
    except (TypeError, ValueError):
        pass

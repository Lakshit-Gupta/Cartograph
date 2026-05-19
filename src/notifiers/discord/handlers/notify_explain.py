"""Handler for `kind=explain_dm` notify payloads."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.common import db
from src.common.logger import get_logger
from src.notifiers.discord import voice

if TYPE_CHECKING:
    from src.notifiers.discord.bot import Bot

_log = get_logger(__name__)


async def post_explain_dm(bot: Bot, payload: dict[str, Any]) -> None:
    try:
        opp_id = payload.get("opp_id")
        if not opp_id:
            return
        row = await db.fetch_one(
            """
            SELECT score, score_components FROM opportunity_scores
            WHERE opportunity_id = $1
            ORDER BY scored_at DESC LIMIT 1
            """,
            UUID(opp_id),
        )
        if not row:
            return
        comps = row["score_components"]
        if isinstance(comps, str):
            try:
                comps = json.loads(comps)
            except Exception:
                comps = {}
        text = f"{voice.pick('explain_intro')} score={row['score']:.2f} — " + ", ".join(f"{k}={v:.2f}" for k, v in (comps or {}).items())
        chan = await bot._resolve_channel(payload.get("channel_id"))
        if chan is not None:
            await chan.send(text)
    except Exception as e:
        _log.exception("explain_dm_failed", err=str(e))

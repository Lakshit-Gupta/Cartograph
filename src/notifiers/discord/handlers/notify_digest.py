"""Handler for `kind=digest` notify payloads.

The original `_post_digest` had cx=20 from the linear DB load + header send +
loop send + state-flip ladder. The work is now broken into four helpers:

- `_resolve_tenant_id` — best-effort cast of `user_id`/`db.current_tenant()`
- `_load_top_opps` — SELECT top-10 ranked opps in last 36h
- `_send_header` — post the digest header embed
- `_send_opp_cards` — fan out opp_card embeds, return list of posted ids
- `_flip_to_digested` — UPDATE state for the ids that posted successfully

`post_digest` orchestrates them; each helper is small and single-purpose.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import discord

from src.common import db
from src.common.logger import get_logger
from src.common.metrics import deliver_success_total
from src.notifiers.discord.embeds.digest_header import build_digest_header
from src.notifiers.discord.embeds.opp_card import build_opp_card
from src.notifiers.discord.handlers.buttons import OppActionView
from src.notifiers.discord.routing import channel_id_for

if TYPE_CHECKING:
    from src.notifiers.discord.bot import Bot

_log = get_logger(__name__)


def _resolve_tenant_id(payload: dict[str, Any]) -> int:
    """Scheduler enqueues `user_id` since Phase 4.2; fall back to
    `db.current_tenant()` (default 1) for backwards compat with older payloads."""
    try:
        return int(payload.get("user_id") or db.current_tenant())
    except (TypeError, ValueError):
        return db.current_tenant()


async def _load_top_opps(tenant_id: int) -> list[dict[str, Any]]:
    """Top-K ranked opps from the last 36h that haven't been digested yet.

    The 36h window absorbs missed digest cron runs (power-fail, restart);
    the state flip in `_flip_to_digested` prevents duplicate posts on later
    triggers."""
    rows = await db.fetch_all(
        """
        SELECT o.id, o.title, o.company, o.description, o.canonical_url,
               o.apply_url, o.comp_min, o.comp_max, o.comp_currency,
               o.comp_period, o.location, o.remote_type, o.category,
               o.posted_at, s.score, s.score_components
        FROM opportunities o
        JOIN opportunity_scores s ON s.opportunity_id = o.id
        WHERE s.user_id = $1
          AND o.state = 'ranked'
          AND o.first_seen > NOW() - INTERVAL '36 hours'
          AND (o.expires_at IS NULL OR o.expires_at > NOW())
        ORDER BY s.score DESC
        LIMIT 10
        """,
        tenant_id,
    )
    return [dict(r) for r in rows]


async def _send_header(
    chan: discord.abc.Messageable,
    chan_id: int | None,
    payload: dict[str, Any],
    opp_rows: list[dict[str, Any]],
) -> None:
    count = int(payload.get("count") or len(opp_rows))
    top = payload.get("top_score") or (opp_rows[0]["score"] if opp_rows else None)
    header = build_digest_header(datetime.now(UTC), count=count, top_score=top)
    await chan.send(embed=header)
    deliver_success_total.labels(channel="digest").inc()
    _log.info("digest_posted", count=count, top_score=top, channel_id=chan_id)


def _normalize_score_components(opp: dict[str, Any]) -> None:
    """Mutate `opp` in place — JSON-decode `score_components` if asyncpg returned a str."""
    comps_raw = opp.get("score_components")
    if isinstance(comps_raw, str):
        try:
            opp["score_components"] = json.loads(comps_raw)
        except Exception:
            opp["score_components"] = {}


async def _send_opp_cards(
    chan: discord.abc.Messageable,
    opp_rows: list[dict[str, Any]],
) -> list[Any]:
    """Send opp_card embeds inline; return ids that posted successfully."""
    posted_ids: list[Any] = []
    for opp in opp_rows:
        _normalize_score_components(opp)
        embed = build_opp_card(
            opp,
            score=opp.get("score"),
            score_components=opp.get("score_components") or {},
        )
        view = OppActionView(opp_id=str(opp["id"]))
        try:
            await chan.send(embed=embed, view=view)
            deliver_success_total.labels(channel="opp").inc()
            posted_ids.append(opp["id"])
        except Exception as e:
            _log.exception("digest_opp_send_failed", err=str(e), opp_id=str(opp["id"]))
    return posted_ids


async def _flip_to_digested(posted_ids: list[Any]) -> None:
    if not posted_ids:
        return
    await db.execute(
        "UPDATE opportunities SET state = 'digested' WHERE id = ANY($1::uuid[])",
        posted_ids,
    )
    _log.info("digest_opps_posted", count=len(posted_ids))


async def post_digest(bot: Bot, payload: dict[str, Any]) -> None:
    chan_id = channel_id_for("daily_digest")
    chan = await bot._resolve_channel(chan_id)
    if chan is None:
        _log.warning("digest_channel_missing")
        return

    tenant_id = _resolve_tenant_id(payload)
    opp_rows = await _load_top_opps(tenant_id)

    await _send_header(chan, chan_id, payload, opp_rows)

    if not opp_rows:
        _log.info("digest_empty", note="no ranked opps in last 36h")
        return

    posted_ids = await _send_opp_cards(chan, opp_rows)
    await _flip_to_digested(posted_ids)

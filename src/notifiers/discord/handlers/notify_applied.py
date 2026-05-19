"""Handler for `kind=applied` notify payloads.

Confirms a sent application by opening a forum thread in #✅-applied. The
original `_post_applied` hit cx=22 from the merged-payload normalization,
nested ForumChannel branch, and the post-thread DB update. Refactored into
small named helpers:

- `_merge_payload` — flatten nested `payload.payload`
- `_build_embed_payload` — build the dict consumed by `applied_embed.build_applied`
- `_create_or_send_thread` — branch on ForumChannel vs text channel, return thread id
- `_persist_thread_id` — UPDATE applications.discord_thread_id
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import discord

from src.common import db
from src.common.logger import get_logger
from src.common.metrics import deliver_success_total
from src.notifiers.discord.embeds import applied as applied_embed
from src.notifiers.discord.handlers._opp_metadata import resolve_opp_metadata
from src.notifiers.discord.routing import channel_id_for

if TYPE_CHECKING:
    from src.notifiers.discord.bot import Bot

_log = get_logger(__name__)


def _merge_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Flatten nested `payload.payload` so callers can read keys uniformly.

    Mirrors the in-place dict merge from the original handler: top-level
    fields first, nested `payload` overrides on conflict.
    """
    nested = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
    return {**payload, **(nested or {})}


def _build_embed_payload(
    data: dict[str, Any],
    opp_row: dict[str, Any],
) -> tuple[dict[str, Any], str | None, str, str]:
    """Compose the dict consumed by `applied_embed.build_applied` plus the
    apply_url + the title/company used for the thread name."""
    title = data.get("title") or opp_row.get("title") or "(untitled)"
    company = data.get("company") or opp_row.get("company") or "—"
    apply_url = data.get("review_url") or data.get("apply_url") or opp_row.get("apply_url")
    embed_payload = {
        "title": title,
        "company": company,
        "method": data.get("method"),
        "target": data.get("target"),
        "sent_at": data.get("sent_at") or datetime.now(UTC).isoformat(),
        "application_id": data.get("application_id"),
    }
    return embed_payload, apply_url, title, company


async def _create_or_send_thread(
    chan: discord.abc.Messageable,
    embed: discord.Embed,
    view: discord.ui.View,
    thread_name: str,
) -> int | None:
    """Create a forum thread when `chan` is a ForumChannel, otherwise send the
    embed and spin up a thread off the resulting message. Returns the new
    thread id (or None on text-channel fallback failure)."""
    if isinstance(chan, discord.ForumChannel):
        thread_with_msg = await chan.create_thread(name=thread_name, embed=embed, view=view)
        return getattr(getattr(thread_with_msg, "thread", thread_with_msg), "id", None)

    msg = await chan.send(embed=embed, view=view)
    try:
        th = await msg.create_thread(name=thread_name[:100])
        return th.id
    except Exception as e:
        _log.warning("applied_thread_create_fallback_failed", err=str(e))
        return None


async def _persist_thread_id(thread_id: int | None, application_id: Any) -> None:
    if not (thread_id and application_id):
        return
    try:
        await db.execute(
            "UPDATE applications SET discord_thread_id = $1 WHERE id = $2",
            int(thread_id),
            int(application_id),
        )
    except Exception as e:
        _log.warning("applications_thread_id_update_failed", err=str(e))


async def post_applied(bot: Bot, payload: dict[str, Any]) -> None:
    """Confirm a sent application by opening a forum thread in #✅-applied."""
    try:
        data = _merge_payload(payload)
        opp_id = data.get("opportunity_id") or data.get("opp_id")
        opp_row = await resolve_opp_metadata(opp_id)

        embed_payload, apply_url, title, company = _build_embed_payload(data, opp_row)
        embed = applied_embed.build_applied(embed_payload)
        view = applied_embed.build_view(apply_url)

        chan_id = channel_id_for("applied")
        chan = await bot._resolve_channel(chan_id)
        if chan is None:
            _log.warning("applied_channel_missing")
            return

        thread_name = applied_embed.thread_title(title, company)
        thread_id = await _create_or_send_thread(chan, embed, view, thread_name)
        await _persist_thread_id(thread_id, data.get("application_id"))

        deliver_success_total.labels(channel="applied").inc()
    except Exception as e:
        _log.exception("post_applied_failed", err=str(e))
        raise

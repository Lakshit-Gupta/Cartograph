"""Handler for `kind=manual_apply_ready` notify payloads.

Opens a [REVIEW] thread with bullets + cover letter and Mark applied/Cancel
buttons. The original `_post_manual_apply` hit cx=27 from nested payload
merging, the ForumChannel branch, and the per-chunk cover-letter send loop.
Refactored into:

- `_merge_payload` — flatten nested `payload.payload`
- `_build_embed_payload` — extract title/company/apply_url/bullets/cover
- `_create_review_thread` — branch on ForumChannel vs text channel
- `_send_cover_letter_chunks` — fan out chunked cover letter into the thread
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord

from src.common.logger import get_logger
from src.common.metrics import deliver_success_total
from src.notifiers.discord.embeds import manual_apply as manual_apply_embed
from src.notifiers.discord.handlers._opp_metadata import resolve_opp_metadata
from src.notifiers.discord.handlers.buttons import OppReviewView
from src.notifiers.discord.routing import channel_id_for

if TYPE_CHECKING:
    from src.notifiers.discord.bot import Bot

_log = get_logger(__name__)


def _merge_payload(payload: dict[str, Any]) -> dict[str, Any]:
    nested = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
    return {**payload, **(nested or {})}


def _build_embed_payload(
    data: dict[str, Any],
    opp_row: dict[str, Any],
) -> tuple[dict[str, Any], str, str, str]:
    """Compose the embed input dict plus title, company, cover_md returned for
    downstream thread naming and chunk-send."""
    title = data.get("title") or opp_row.get("title") or "(untitled)"
    company = data.get("company") or opp_row.get("company") or "—"
    apply_url = data.get("apply_url") or data.get("review_url") or data.get("target") or opp_row.get("apply_url") or ""
    cover_md = data.get("cover_letter_markdown") or ""
    bullets = data.get("tailored_bullets") or []
    embed_payload = {
        "title": title,
        "company": company,
        "apply_url": apply_url,
        "tailored_bullets": bullets,
        "cover_letter_markdown": cover_md,
    }
    return embed_payload, title, company, cover_md


async def _create_review_thread(
    chan: discord.abc.Messageable,
    embed: discord.Embed,
    view: discord.ui.View,
    thread_name: str,
) -> discord.abc.Messageable | None:
    """Create a forum thread, or fall back to text-channel send + create_thread."""
    if isinstance(chan, discord.ForumChannel):
        created = await chan.create_thread(name=thread_name, embed=embed, view=view)
        return getattr(created, "thread", created)

    msg = await chan.send(embed=embed, view=view)
    try:
        return await msg.create_thread(name=thread_name[:100])
    except Exception as e:
        _log.warning("manual_apply_thread_create_fallback_failed", err=str(e))
        return None


async def _send_cover_letter_chunks(
    thread: discord.abc.Messageable | None,
    cover_md: str,
) -> None:
    if thread is None or not cover_md:
        return
    for chunk in manual_apply_embed.chunk_cover_letter(cover_md, max_len=1900):
        try:
            await thread.send(content=f"```\n{chunk}\n```"[:2000])
        except Exception as e:
            _log.warning("manual_apply_chunk_send_failed", err=str(e))
            break


async def post_manual_apply(bot: Bot, payload: dict[str, Any]) -> None:
    """Open a [REVIEW] thread with bullets + cover letter + Mark applied/Cancel buttons."""
    try:
        data = _merge_payload(payload)
        opp_id = data.get("opportunity_id") or data.get("opp_id")
        opp_row = await resolve_opp_metadata(opp_id)

        embed_payload, title, company, cover_md = _build_embed_payload(data, opp_row)
        embed = manual_apply_embed.build_manual_apply(embed_payload)
        view = OppReviewView(opp_id=str(opp_id or "00000000-0000-0000-0000-000000000000"))

        chan_id = channel_id_for("applied")
        chan = await bot._resolve_channel(chan_id)
        if chan is None:
            _log.warning("manual_apply_channel_missing")
            return

        thread_name = manual_apply_embed.thread_title(title, company)
        thread = await _create_review_thread(chan, embed, view, thread_name)
        await _send_cover_letter_chunks(thread, cover_md)

        deliver_success_total.labels(channel="applied").inc()
    except Exception as e:
        _log.exception("post_manual_apply_failed", err=str(e))
        raise

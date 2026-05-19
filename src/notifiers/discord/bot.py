"""Discord Bot subclass.

Responsibilities:
- Register slash commands (guild-scoped sync on `on_ready`).
- Wire reaction handler.
- Run a background task consuming `stream:notify` and posting embeds.

The worker entrypoint `src/workers/notifier_worker.py` instantiates and
runs this class. No global state created at import time.

Per-kind notify handlers live under `src/notifiers/discord/handlers/notify_*.py`
and are invoked through the `_DISPATCH_TABLE` below. This keeps `Bot` itself a
thin dispatcher — see CLAUDE.md ("Split any file exceeding ~300 lines.").
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import discord
from discord import app_commands

from src.common.logger import get_logger
from src.common.metrics import deliver_success_total
from src.common.queue import Groups, RedisQ, Streams
from src.common.secrets import get_settings
from src.notifiers.discord.commands import register_all as register_all_commands
from src.notifiers.discord.handlers.buttons import FollowupActionView, OppActionView, OppReviewView
from src.notifiers.discord.handlers.notify_alert import post_alert
from src.notifiers.discord.handlers.notify_applied import post_applied
from src.notifiers.discord.handlers.notify_digest import post_digest
from src.notifiers.discord.handlers.notify_explain import post_explain_dm
from src.notifiers.discord.handlers.notify_followup import post_followup_ready
from src.notifiers.discord.handlers.notify_manual_apply import post_manual_apply
from src.notifiers.discord.handlers.notify_opp import post_opp
from src.notifiers.discord.handlers.notify_priority import post_priority
from src.notifiers.discord.handlers.notify_tracker import post_tracker
from src.notifiers.discord.handlers.reactions import handle_raw_reaction_add

_log = get_logger(__name__)


def _default_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.message_content = False
    intents.reactions = True
    intents.guilds = True
    intents.members = False
    return intents


async def _ack_digest_schedule(bot: Bot, payload: dict[str, Any]) -> None:
    """`digest_schedule` is persisted by another worker; nothing to do here."""
    _log.info("digest_schedule_ack", payload=payload)


_NotifyHandler = Callable[["Bot", dict[str, Any]], Awaitable[None]]

# kind (lower-case) → handler. Single dict lookup replaces the cx-13 if/elif
# ladder that used to live in `_dispatch`.
_DISPATCH_TABLE: dict[str, _NotifyHandler] = {
    "opp": post_opp,
    "lane_post": post_opp,
    "ranked": post_opp,
    "digested": post_opp,
    "digest": post_digest,
    "priority_push": post_priority,
    "alert": post_alert,
    "tracker_update": post_tracker,
    "explain_dm": post_explain_dm,
    "applied": post_applied,
    "manual_apply_ready": post_manual_apply,
    "followup_ready": post_followup_ready,
    "digest_schedule": _ack_digest_schedule,
}


class Bot(discord.Client):
    """Cartograph notifier bot (display name: Hop). Owns gateway + notify-stream consumer."""

    def __init__(self, *, intents: discord.Intents | None = None) -> None:
        super().__init__(intents=intents or _default_intents())
        self.tree = app_commands.CommandTree(self)
        self._consumer_task: asyncio.Task | None = None
        self._redis: RedisQ | None = None

    # ---- discord.py lifecycle hooks ----------------------------------------
    async def setup_hook(self) -> None:
        # Register all slash commands.
        register_all_commands(self)

        settings = get_settings()
        # Fail loud if Discord channel IDs missing — silent posts to channel 0
        # waste an entire day of debugging.
        settings.assert_channels_configured(required=("daily_digest", "priority_push", "alerts", "applied"))
        if settings.discord_guild_id:
            guild_obj = discord.Object(id=settings.discord_guild_id)
            self.tree.copy_global_to(guild=guild_obj)
            await self.tree.sync(guild=guild_obj)
            _log.info("slash_commands_synced", guild_id=settings.discord_guild_id)
        else:
            # Global sync is slow (can be ~1h to propagate). Prefer guild-scoped.
            await self.tree.sync()
            _log.info("slash_commands_synced_globally")

        # Re-register persistent views so old messages still respond after restart.
        self.add_view(OppActionView(opp_id="00000000-0000-0000-0000-000000000000"))
        self.add_view(OppReviewView(opp_id="00000000-0000-0000-0000-000000000000"))
        # Phase 2.3 follow-up buttons. Real custom_id is `followup:<action>:<id>`;
        # discord.py matches the prefix when re-binding callbacks on restart.
        self.add_view(FollowupActionView(followup_id=0))

    async def on_ready(self) -> None:
        _log.info("discord_ready", user=str(self.user), guilds=len(self.guilds))
        # Spin up notify-stream consumer once.
        if self._consumer_task is None:
            self._consumer_task = asyncio.create_task(self._notify_consumer(), name="notify-consumer")

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await handle_raw_reaction_add(payload, self)

    # ---- notify-stream consumer --------------------------------------------
    async def _notify_consumer(self) -> None:
        """Pulls NotificationTask payloads from `stream:notify` and dispatches."""
        try:
            self._redis = await RedisQ.connect()
        except Exception as e:
            _log.exception("redis_connect_failed", err=str(e))
            return

        _log.info("notify_consumer_started")
        async for msg in self._redis.consume(Streams.NOTIFY, Groups.NOTIFIERS):
            payload = msg.fields
            try:
                await self._dispatch(payload)
                await self._redis.ack(Streams.NOTIFY, Groups.NOTIFIERS, msg.msg_id)
            except Exception as e:
                _log.exception("notify_dispatch_failed", err=str(e), payload=payload)
                try:
                    await self._redis.dlq(Streams.NOTIFY, msg.msg_id, payload, str(e))
                    await self._redis.ack(Streams.NOTIFY, Groups.NOTIFIERS, msg.msg_id)
                except Exception:
                    pass

    async def _dispatch(self, payload: dict[str, Any]) -> None:
        kind = (payload.get("kind") or "").lower()
        handler = _DISPATCH_TABLE.get(kind)
        if handler is None:
            _log.warning("unknown_notify_kind", kind=kind)
            return
        await handler(self, payload)

    # ---- helpers ------------------------------------------------------------
    async def _resolve_channel(self, channel_id: int | None) -> discord.abc.Messageable | None:
        if not channel_id:
            return None
        chan = self.get_channel(channel_id)
        if chan is None:
            try:
                chan = await self.fetch_channel(channel_id)
            except Exception as e:
                _log.warning("channel_fetch_failed", id=channel_id, err=str(e))
                return None
        return chan  # type: ignore[return-value]

    async def _send_embed(
        self,
        channel: discord.abc.Messageable,
        embed: discord.Embed,
        *,
        view: discord.ui.View | None = None,
        route: dict[str, Any] | None = None,
    ) -> None:
        try:
            if route and route.get("forum") and isinstance(channel, discord.ForumChannel):
                title = embed.title or "opp"
                await channel.create_thread(name=title[:90], embed=embed, view=view)
            else:
                await channel.send(embed=embed, view=view)
            deliver_success_total.labels(channel="opp").inc()
        except Exception as e:
            _log.exception("send_embed_failed", err=str(e))

    # ---- runner -------------------------------------------------------------
    async def start_default(self) -> None:
        settings = get_settings()
        if not settings.discord_bot_token:
            raise RuntimeError("DISCORD_BOT_TOKEN not configured")
        await self.start(settings.discord_bot_token)

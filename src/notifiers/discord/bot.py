"""Discord Bot subclass.

Responsibilities:
- Register slash commands (guild-scoped sync on `on_ready`).
- Wire reaction handler.
- Run a background task consuming `stream:notify` and posting embeds.

The worker entrypoint `src/workers/notifier_worker.py` instantiates and
runs this class. No global state created at import time.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import discord
from discord import app_commands

from src.common import db
from src.common.logger import get_logger
from src.common.metrics import deliver_success_total
from src.common.queue import Groups, RedisQ, Streams
from src.common.secrets import get_settings
from src.notifiers.discord import voice
from src.notifiers.discord.commands import register_all as register_all_commands
from src.notifiers.discord.embeds import applied as applied_embed
from src.notifiers.discord.embeds import manual_apply as manual_apply_embed
from src.notifiers.discord.embeds.digest_header import build_digest_header
from src.notifiers.discord.embeds.opp_card import build_opp_card
from src.notifiers.discord.embeds.priority_push import build_priority_push
from src.notifiers.discord.handlers.buttons import FollowupActionView, OppActionView, OppReviewView
from src.notifiers.discord.handlers.reactions import handle_raw_reaction_add
from src.notifiers.discord.routing import channel_id_for, route_for

_log = get_logger(__name__)


def _default_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.message_content = False
    intents.reactions = True
    intents.guilds = True
    intents.members = False
    return intents


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
        if kind in ("opp", "lane_post", "ranked", "digested"):
            await self._post_opp(payload)
        elif kind == "digest":
            await self._post_digest(payload)
        elif kind == "priority_push":
            await self._post_priority(payload)
        elif kind == "alert":
            await self._post_alert(payload)
        elif kind == "tracker_update":
            await self._post_tracker(payload)
        elif kind == "explain_dm":
            await self._post_explain_dm(payload)
        elif kind == "digest_schedule":
            # Persisted by another worker; nothing to do here.
            _log.info("digest_schedule_ack", payload=payload)
        elif kind == "applied":
            await self._post_applied(payload)
        elif kind == "manual_apply_ready":
            await self._post_manual_apply(payload)
        elif kind == "followup_ready":
            await self._post_followup_ready(payload)
        else:
            _log.warning("unknown_notify_kind", kind=kind)

    # ---- handlers per kind --------------------------------------------------
    async def _post_opp(self, payload: dict[str, Any]) -> None:
        opp = payload.get("opp") or payload
        route = route_for(opp, kind="lane")
        chan = await self._resolve_channel(route["channel_id"])
        if chan is None:
            _log.warning("opp_channel_missing", route=route)
            return

        score = payload.get("score")
        score_components = payload.get("score_components") or {}
        embed = build_opp_card(opp, score=score, score_components=score_components)
        view = OppActionView(opp_id=str(opp.get("id") or payload.get("opportunity_id")))
        await self._send_embed(chan, embed, view=view, route=route)

        # Priority push duplication when score exceeds per-lane threshold.
        try:
            if score is not None and float(score) >= route.get("push_threshold", 1.01):
                await self._post_priority({"opp": opp, "score": score})
        except (TypeError, ValueError):
            pass

    async def _post_digest(self, payload: dict[str, Any]) -> None:
        chan_id = channel_id_for("daily_digest")
        chan = await self._resolve_channel(chan_id)
        if chan is None:
            _log.warning("digest_channel_missing")
            return

        # Pull top-K ranked opps from the last 36h that haven't been digested yet.
        # The 36h window absorbs missed digest cron runs (power-fail, restart);
        # the state flip below prevents duplicate posts on subsequent triggers.
        rows = await db.fetch_all(
            """
            SELECT o.id, o.title, o.company, o.description, o.canonical_url,
                   o.apply_url, o.comp_min, o.comp_max, o.comp_currency,
                   o.comp_period, o.location, o.remote_type, o.category,
                   o.posted_at, s.score, s.score_components
            FROM opportunities o
            JOIN opportunity_scores s ON s.opportunity_id = o.id
            WHERE s.user_id = 1
              AND o.state = 'ranked'
              AND o.first_seen > NOW() - INTERVAL '36 hours'
            ORDER BY s.score DESC
            LIMIT 10
            """
        )
        opp_rows = [dict(r) for r in rows]
        count = int(payload.get("count") or len(opp_rows))
        top = payload.get("top_score") or (opp_rows[0]["score"] if opp_rows else None)

        header = build_digest_header(
            datetime.now(UTC),
            count=count,
            top_score=top,
        )
        await chan.send(embed=header)
        deliver_success_total.labels(channel="digest").inc()
        _log.info("digest_posted", count=count, top_score=top, channel_id=chan_id)

        if not opp_rows:
            _log.info("digest_empty", note="no ranked opps in last 36h")
            return

        # Send opp_card embeds inline + flip state to digested.
        posted_ids: list[Any] = []
        for opp in opp_rows:
            comps_raw = opp.get("score_components")
            if isinstance(comps_raw, str):
                try:
                    opp["score_components"] = json.loads(comps_raw)
                except Exception:
                    opp["score_components"] = {}
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

        if posted_ids:
            await db.execute(
                "UPDATE opportunities SET state = 'digested' WHERE id = ANY($1::uuid[])",
                posted_ids,
            )
            _log.info("digest_opps_posted", count=len(posted_ids))

    async def _post_priority(self, payload: dict[str, Any]) -> None:
        opp = payload.get("opp") or payload
        chan_id = channel_id_for("priority_push")
        chan = await self._resolve_channel(chan_id)
        if chan is None:
            _log.warning("priority_channel_missing")
            return
        embed = build_priority_push(opp, score=payload.get("score"), reason=payload.get("reason"))
        view = OppActionView(opp_id=str(opp.get("id") or payload.get("opportunity_id")))
        await chan.send(content=voice.pick("freelance_push"), embed=embed, view=view)
        deliver_success_total.labels(channel="priority").inc()

    async def _post_alert(self, payload: dict[str, Any]) -> None:
        route = route_for({"alert": payload.get("alert")}, kind="alert")
        chan = await self._resolve_channel(route["channel_id"])
        if chan is None:
            return
        msg = payload.get("message") or payload.get("alert") or "alert"
        content = f"@here {msg}" if route.get("mention_owner") else msg
        await chan.send(content=content)
        deliver_success_total.labels(channel="alerts").inc()

    async def _post_tracker(self, payload: dict[str, Any]) -> None:
        route = route_for({"tracker": payload.get("tracker", "applied")}, kind="tracker")
        chan = await self._resolve_channel(route["channel_id"])
        if chan is None:
            return
        await chan.send(content=payload.get("message", json.dumps(payload, default=str))[:1900])
        deliver_success_total.labels(channel="tracker").inc()

    async def _post_explain_dm(self, payload: dict[str, Any]) -> None:
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
            text = f"{voice.pick('explain_intro')} score={row['score']:.2f} — " + ", ".join(
                f"{k}={v:.2f}" for k, v in (comps or {}).items()
            )
            chan = await self._resolve_channel(payload.get("channel_id"))
            if chan is not None:
                await chan.send(text)
        except Exception as e:
            _log.exception("explain_dm_failed", err=str(e))

    async def _post_applied(self, payload: dict[str, Any]) -> None:
        """Confirm a sent application by opening a forum thread in #✅-applied."""
        try:
            data = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
            data = {**payload, **(data or {})}  # merge: nested overrides top-level
            opp_id = data.get("opportunity_id") or data.get("opp_id")
            application_id = data.get("application_id")

            opp_row: dict[str, Any] = {}
            if opp_id:
                row = await db.fetch_one(
                    "SELECT title, company, apply_url FROM opportunities WHERE id = $1",
                    UUID(str(opp_id)),
                )
                if row is not None:
                    opp_row = dict(row)

            title = data.get("title") or opp_row.get("title") or "(untitled)"
            company = data.get("company") or opp_row.get("company") or "—"
            apply_url = data.get("review_url") or data.get("apply_url") or opp_row.get("apply_url")

            embed_payload = {
                "title": title,
                "company": company,
                "method": data.get("method"),
                "target": data.get("target"),
                "sent_at": data.get("sent_at") or datetime.now(UTC).isoformat(),
                "application_id": application_id,
            }
            embed = applied_embed.build_applied(embed_payload)
            view = applied_embed.build_view(apply_url)

            chan_id = channel_id_for("applied")
            chan = await self._resolve_channel(chan_id)
            if chan is None:
                _log.warning("applied_channel_missing")
                return

            thread_name = applied_embed.thread_title(title, company)
            thread_id: int | None = None
            if isinstance(chan, discord.ForumChannel):
                thread_with_msg = await chan.create_thread(name=thread_name, embed=embed, view=view)
                thread_id = getattr(getattr(thread_with_msg, "thread", thread_with_msg), "id", None)
            else:
                msg = await chan.send(embed=embed, view=view)
                try:
                    th = await msg.create_thread(name=thread_name[:100])
                    thread_id = th.id
                except Exception as e:
                    _log.warning("applied_thread_create_fallback_failed", err=str(e))

            if thread_id and application_id:
                try:
                    await db.execute(
                        "UPDATE applications SET discord_thread_id = $1 WHERE id = $2",
                        int(thread_id),
                        int(application_id),
                    )
                except Exception as e:
                    _log.warning("applications_thread_id_update_failed", err=str(e))

            deliver_success_total.labels(channel="applied").inc()
        except Exception as e:
            _log.exception("post_applied_failed", err=str(e))
            raise

    async def _post_manual_apply(self, payload: dict[str, Any]) -> None:
        """Open a [REVIEW] thread with bullets + cover letter + Mark applied/Cancel buttons."""
        try:
            data = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
            data = {**payload, **(data or {})}
            opp_id = data.get("opportunity_id") or data.get("opp_id")

            opp_row: dict[str, Any] = {}
            if opp_id:
                row = await db.fetch_one(
                    "SELECT title, company, apply_url FROM opportunities WHERE id = $1",
                    UUID(str(opp_id)),
                )
                if row is not None:
                    opp_row = dict(row)

            title = data.get("title") or opp_row.get("title") or "(untitled)"
            company = data.get("company") or opp_row.get("company") or "—"
            apply_url = data.get("apply_url") or data.get("review_url") or data.get("target") or opp_row.get("apply_url") or ""
            cover_md = data.get("cover_letter_markdown") or ""
            bullets = data.get("tailored_bullets") or []

            embed = manual_apply_embed.build_manual_apply(
                {
                    "title": title,
                    "company": company,
                    "apply_url": apply_url,
                    "tailored_bullets": bullets,
                    "cover_letter_markdown": cover_md,
                }
            )
            view = OppReviewView(opp_id=str(opp_id or "00000000-0000-0000-0000-000000000000"))

            chan_id = channel_id_for("applied")
            chan = await self._resolve_channel(chan_id)
            if chan is None:
                _log.warning("manual_apply_channel_missing")
                return

            thread_name = manual_apply_embed.thread_title(title, company)
            thread = None
            if isinstance(chan, discord.ForumChannel):
                created = await chan.create_thread(name=thread_name, embed=embed, view=view)
                thread = getattr(created, "thread", created)
            else:
                msg = await chan.send(embed=embed, view=view)
                try:
                    thread = await msg.create_thread(name=thread_name[:100])
                except Exception as e:
                    _log.warning("manual_apply_thread_create_fallback_failed", err=str(e))

            # Follow up with chunked cover letter inside the thread.
            if thread is not None and cover_md:
                for chunk in manual_apply_embed.chunk_cover_letter(cover_md, max_len=1900):
                    try:
                        await thread.send(content=f"```\n{chunk}\n```"[:2000])
                    except Exception as e:
                        _log.warning("manual_apply_chunk_send_failed", err=str(e))
                        break

            deliver_success_total.labels(channel="applied").inc()
        except Exception as e:
            _log.exception("post_manual_apply_failed", err=str(e))
            raise

    async def _post_followup_ready(self, payload: dict[str, Any]) -> None:
        """Phase 2.3 — surface the LLM-drafted follow-up with Send/Edit/Skip."""
        try:
            from src.notifiers.discord.embeds.followup import build_followup_ready, thread_title

            data = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
            data = {**payload, **(data or {})}

            followup_id = data.get("followup_id")
            if not followup_id:
                _log.warning("followup_ready_no_id", payload=payload)
                return

            embed = build_followup_ready(data)
            view = FollowupActionView(followup_id=int(followup_id))

            chan_id = channel_id_for("applied")
            chan = await self._resolve_channel(chan_id)
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

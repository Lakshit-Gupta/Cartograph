"""Handler for the Internshala browser-discovery cycle report.

The ThinkPad discovery worker publishes exactly one `discovery_cycle_report`
onto `stream:notify` per cycle (healthy or not — see the design spec
§2 amendment 9). This handler renders it for Discord:

  healthy=True   — low-noise single line posted to #🛠-source-health. Just
                   the pre-formatted `summary` string carried on a slim green
                   embed. At the 3-minute testing cadence this is ~20
                   posts/hour, so it must stay quiet.

  healthy=False  — a richer red embed carrying the failing combos, selector
                   misses, scrape/publish/reject counts, and the selector +
                   matrix versions, with the worker's screenshot attached
                   (present only on degraded/failed cycles). Always posted to
                   #🛠-source-health. For *hard* failures — a cycle that
                   published nothing AND is unhealthy, or any selector miss
                   (UI drift) — it is ALSO escalated to #🔔-alerts so the
                   operator is paged rather than left to scan source-health.

Screenshots ride the embed via `discord.File`, mirroring
`notify_auto_apply`. CLAUDE.md hard rule #5 (no PDF to Discord) does not
apply to PNG screenshots — they expose nothing beyond a public listing page.
"""

from __future__ import annotations

import base64
import io
from typing import TYPE_CHECKING, Any

import discord

from src.common.logger import get_logger
from src.common.metrics import deliver_success_total
from src.notifiers.discord.routing import channel_id_for

if TYPE_CHECKING:
    from src.notifiers.discord.bot import Bot

_log = get_logger(__name__)

_HEALTHY_COLOR = 0x2ECC71  # green
_DEGRADED_COLOR = 0xE74C3C  # red

_SOURCE_HEALTH_KEY = "source_health"
_ALERTS_KEY = "alerts"

# Discord embed-field values cap at 1024 chars; keep list renders well under.
_FIELD_CHAR_CAP = 1000


def _screenshot_file(b64_data: str | None) -> discord.File | None:
    """Decode a base64 PNG into a `discord.File`, or None when absent/garbage."""
    if not b64_data:
        return None
    try:
        raw = base64.b64decode(b64_data)
    except Exception as e:  # malformed payload — surface but do not crash the cycle card
        _log.warning("discovery_cycle_screenshot_decode_failed", err=str(e))
        return None
    return discord.File(io.BytesIO(raw), filename="discovery_miss.png")


def _fmt_list(items: list[Any] | None) -> str:
    """Render a string list for an embed field, truncating to the field cap."""
    if not items:
        return "—"
    rendered = ", ".join(str(i) for i in items)
    if len(rendered) > _FIELD_CHAR_CAP:
        rendered = rendered[: _FIELD_CHAR_CAP - 1] + "…"
    return rendered


def _build_degraded_embed(payload: dict[str, Any]) -> discord.Embed:
    details: dict[str, Any] = payload.get("details") or {}
    started_at = payload.get("started_at")
    duration = payload.get("duration_sec")

    embed = discord.Embed(
        title=f"Discovery cycle degraded — {payload.get('source_slug') or 'unknown'}",
        description=str(payload.get("summary") or ""),
        color=_DEGRADED_COLOR,
    )

    combos_attempted = details.get("combos_attempted")
    combos_succeeded = details.get("combos_succeeded")
    if combos_attempted is not None or combos_succeeded is not None:
        embed.add_field(
            name="Combos",
            value=f"{combos_succeeded if combos_succeeded is not None else '?'}"
            f"/{combos_attempted if combos_attempted is not None else '?'}",
            inline=True,
        )
    if duration is not None:
        embed.add_field(name="Duration", value=f"{float(duration):.0f}s", inline=True)

    embed.add_field(name="Combo timeouts", value=_fmt_list(details.get("combo_timeouts")), inline=False)
    embed.add_field(name="Selector misses", value=_fmt_list(details.get("selector_misses")), inline=False)

    embed.add_field(
        name="Cards",
        value=(f"scraped {details.get('cards_scraped', 0)} · published {details.get('cards_published', 0)}"),
        inline=False,
    )
    embed.add_field(
        name="Rejected",
        value=(
            f"sub-floor {details.get('cards_rejected_subfloor', 0)} · "
            f"dedup {details.get('cards_rejected_dedup', 0)} · "
            f"parse {details.get('cards_rejected_parse', 0)} · "
            f"expired {details.get('cards_rejected_expired', 0)} · "
            f"experience {details.get('cards_rejected_experience', 0)}"
        ),
        inline=False,
    )

    footer_bits = []
    sv = details.get("selectors_version")
    mv = details.get("matrix_version")
    if sv:
        footer_bits.append(f"selectors {sv}")
    if mv:
        footer_bits.append(f"matrix {mv}")
    if payload.get("cycle_id"):
        footer_bits.append(f"cycle {payload['cycle_id']}")
    if started_at:
        footer_bits.append(str(started_at))
    if footer_bits:
        embed.set_footer(text=" · ".join(footer_bits))
    return embed


def _is_hard_failure(payload: dict[str, Any]) -> bool:
    """Hard failure warrants an #🔔-alerts page on top of the source-health post.

    Two triggers:
      • UI drift — any selector miss means the dropdown/card DOM moved and the
        combo silently yields nothing until selectors are patched.
      • Dry cycle — an unhealthy cycle that published zero cards (session
        expiry, captcha, total selector breakage).
    """
    details: dict[str, Any] = payload.get("details") or {}
    if details.get("selector_misses"):
        return True
    published = details.get("cards_published", 0) or 0
    return bool(payload.get("healthy") is False and published == 0)


async def _send(chan: discord.abc.Messageable, *, content: str | None, embed: discord.Embed, file: discord.File | None) -> None:
    """Send to a text channel, or open a thread when the target is a forum.

    source-health / alerts are TEXT channels in the locked server layout, so
    the plain `chan.send` path is the norm; the ForumChannel branch mirrors
    `notify_auto_apply` purely as a safety net against config drift.
    """
    if isinstance(chan, discord.ForumChannel):
        await chan.create_thread(
            name=(embed.title or content or "discovery cycle")[:100],
            content=content or discord.utils.MISSING,
            embed=embed,
            file=file or discord.utils.MISSING,
        )
    else:
        await chan.send(content=content or None, embed=embed, file=file or discord.utils.MISSING)


async def post_discovery_cycle(bot: Bot, payload: dict[str, Any]) -> None:
    """Render one `discovery_cycle_report` and post it.

    Healthy cycles get a single quiet line in #🛠-source-health. Unhealthy
    cycles get a red detail embed there, plus an #🔔-alerts page on hard
    failures (selector miss or a dry unhealthy cycle). Missing channels are
    logged and skipped; send failures re-raise so the notifier worker DLQs
    the message.
    """
    healthy = bool(payload.get("healthy"))
    summary = str(payload.get("summary") or "discovery cycle complete")

    health_id = channel_id_for(_SOURCE_HEALTH_KEY)
    health_chan = await bot._resolve_channel(health_id)
    if health_chan is None:
        _log.warning("discovery_cycle_channel_missing", channel_key=_SOURCE_HEALTH_KEY)
        return

    if healthy:
        embed = discord.Embed(description=summary, color=_HEALTHY_COLOR)
        try:
            await _send(health_chan, content=None, embed=embed, file=None)
            deliver_success_total.labels(channel=_SOURCE_HEALTH_KEY).inc()
        except Exception as e:
            _log.exception("discovery_cycle_post_failed", channel_key=_SOURCE_HEALTH_KEY, err=str(e))
            raise
        return

    # Unhealthy — detail embed + screenshot to source-health.
    embed = _build_degraded_embed(payload)
    screenshot = _screenshot_file(payload.get("screenshot_b64"))
    try:
        await _send(health_chan, content=None, embed=embed, file=screenshot)
        deliver_success_total.labels(channel=_SOURCE_HEALTH_KEY).inc()
    except Exception as e:
        _log.exception("discovery_cycle_post_failed", channel_key=_SOURCE_HEALTH_KEY, err=str(e))
        raise

    if not _is_hard_failure(payload):
        return

    alerts_id = channel_id_for(_ALERTS_KEY)
    alerts_chan = await bot._resolve_channel(alerts_id)
    if alerts_chan is None:
        _log.warning("discovery_cycle_alert_channel_missing", channel_key=_ALERTS_KEY)
        return

    # A fresh File object is required — a discord.File is single-use once sent.
    alert_shot = _screenshot_file(payload.get("screenshot_b64"))
    try:
        await _send(alerts_chan, content=f"@here Internshala discovery degraded — {summary}", embed=embed, file=alert_shot)
        deliver_success_total.labels(channel=_ALERTS_KEY).inc()
    except Exception as e:
        _log.exception("discovery_cycle_post_failed", channel_key=_ALERTS_KEY, err=str(e))
        raise


__all__ = ["post_discovery_cycle"]

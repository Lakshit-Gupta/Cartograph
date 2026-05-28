"""Handler for Phase 4 auto-apply notify kinds.

Three kinds share one handler, dispatched by `payload['kind']`:

  auto_applied        — sidecar successfully submitted Easy Apply.
                        Post to #✅-applied with a green embed +
                        screenshot of the Internshala confirmation.
  auto_apply_dry_run  — sidecar filled the modal then STOPPED before
                        clicking Submit (verification window). Post to
                        #🛠-source-health (closest existing channel —
                        no #auto-apply-dryrun forum yet) with the
                        screenshot for human review.
  auto_apply_failed   — sidecar errored. Roll-back state already
                        recorded by apply_result_worker; this handler
                        surfaces the error + screenshot in #🔔-alerts
                        so the user can patch selectors / retry manually.

Screenshots are pulled from `payload.screenshot_b64`, decoded back to
PNG bytes, and attached via `discord.File`. CLAUDE.md hard rule #5
(no PDF to Discord) does NOT apply to PNG screenshots — they contain
no candidate PII beyond what the user already pasted into the resume.
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


_KIND_CONFIG: dict[str, dict[str, Any]] = {
    "auto_applied": {
        "channel_key": "applied",
        "color": 0x2ECC71,  # green
        "title_prefix": "Auto-applied",
        "footer": "Sidecar submitted Easy Apply successfully.",
    },
    "auto_apply_dry_run": {
        "channel_key": "source_health",
        "color": 0xF1C40F,  # amber
        "title_prefix": "Dry-run captured",
        "footer": "Sidecar filled the modal — review screenshot, no submit fired.",
    },
    "auto_apply_failed": {
        "channel_key": "alerts",
        "color": 0xE74C3C,  # red
        "title_prefix": "Auto-apply failed",
        "footer": "Opp rolled back to queued; /apply again to retry manually.",
    },
}


def _merge_payload(payload: dict[str, Any]) -> dict[str, Any]:
    nested = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
    return {**payload, **(nested or {})}


def _build_embed(kind: str, data: dict[str, Any]) -> discord.Embed:
    cfg = _KIND_CONFIG[kind]
    thread_title = data.get("thread_title") or "(unknown opp)"
    embed = discord.Embed(
        title=f"{cfg['title_prefix']} — {thread_title}",
        description=data.get("browser_error") or "",
        color=cfg["color"],
    )
    embed.add_field(name="Platform", value=str(data.get("platform") or "—"), inline=True)
    embed.add_field(name="Task", value=str(data.get("task_id") or "—"), inline=True)
    apply_url = data.get("apply_url")
    if apply_url:
        embed.add_field(name="Apply URL", value=f"[Open]({apply_url})", inline=False)
    if data.get("submitted_at"):
        embed.add_field(name="Submitted at", value=str(data["submitted_at"]), inline=False)
    selectors_version = data.get("selectors_version")
    if selectors_version:
        embed.set_footer(text=f"{cfg['footer']} · selectors {selectors_version}")
    else:
        embed.set_footer(text=cfg["footer"])
    return embed


def _screenshot_file(b64_data: str | None) -> discord.File | None:
    if not b64_data:
        return None
    try:
        raw = base64.b64decode(b64_data)
    except Exception as e:
        _log.warning("auto_apply_screenshot_decode_failed", err=str(e))
        return None
    return discord.File(io.BytesIO(raw), filename="apply_capture.png")


async def post_auto_apply(bot: Bot, payload: dict[str, Any]) -> None:
    """Single dispatch entrypoint for auto_applied / auto_apply_dry_run /
    auto_apply_failed. Reads `payload.kind` to pick the embed colour +
    channel + footer text."""
    kind = str(payload.get("kind") or "auto_apply_failed")
    if kind not in _KIND_CONFIG:
        _log.warning("auto_apply_unknown_kind", kind=kind)
        return
    data = _merge_payload(payload)
    cfg = _KIND_CONFIG[kind]
    chan_id = channel_id_for(cfg["channel_key"])
    chan = await bot._resolve_channel(chan_id)
    if chan is None:
        _log.warning("auto_apply_channel_missing", kind=kind, channel_key=cfg["channel_key"])
        return

    embed = _build_embed(kind, data)
    screenshot = _screenshot_file(data.get("screenshot_b64"))

    try:
        if isinstance(chan, discord.ForumChannel):
            await chan.create_thread(
                name=f"{cfg['title_prefix']} — {data.get('thread_title') or 'opp'}"[:100],
                embed=embed,
                file=screenshot,
            )
        else:
            await chan.send(embed=embed, file=screenshot or discord.utils.MISSING)
        deliver_success_total.labels(channel=cfg["channel_key"]).inc()
    except Exception as e:
        _log.exception("auto_apply_post_failed", kind=kind, err=str(e))
        raise


__all__ = ["post_auto_apply"]

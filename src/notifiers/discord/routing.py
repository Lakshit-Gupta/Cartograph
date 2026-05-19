"""Map opportunity → Discord channel + embed color.

Reads `config/routing_rules.yaml` at runtime (so ops can edit without
redeploying) for the rule taxonomy (per_lane / tracker / alerts buckets),
and resolves channel-name → Discord ID via the per-tenant route table in
`notification_routes` (with a 5min in-process cache backed by
`src/notifiers/discord/routing_db.py`). When the DB has no row for a
logical channel name yet — fresh deployment that has not promoted IDs out
of env into DB — we fall through to `settings.discord_channel(name)`,
which mirrors the SOPS-encrypted env vars (`discord_channel_<name>`).

This two-tier read keeps the bot boot-safe (a brand-new DB with no V020
seed still routes via env) AND lets the operator override per-tenant
without a redeploy (`mp routes set <name> <id>` writes the DB row +
invalidates the cache).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from src.common.logger import get_logger
from src.common.secrets import get_settings
from src.notifiers.discord.routing_db import get_cached_routes

_log = get_logger(__name__)

DEFAULT_COLOR = 0x6B7280


@lru_cache(maxsize=1)
def _rules() -> dict[str, Any]:
    settings = get_settings()
    p = Path(settings.config_root) / "routing_rules.yaml"
    if not p.exists():
        _log.warning("routing_rules_missing", path=str(p))
        return {}
    with p.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def channel_id_for(name: str) -> int | None:
    """Channel name → numeric ID.

    Read order (each tier falls through to the next on miss):
      1. Per-tenant `notification_routes` cache — populated by an explicit
         `await load_routes(user_id)` (e.g. at bot startup or after every
         `mp routes` write). Cache TTL = 5min.
      2. `settings.discord_channel(<name>)` — the env-backed default; the
         SOPS exec-env pipeline exports `DISCORD_CHANNEL_<NAME>` for each
         logical channel in `Settings._CHANNEL_NAMES`.

    Returns None when the name is unknown OR the resolved ID is 0
    (unset). Callers fail loudly via
    `get_settings().assert_channels_configured(...)` at startup, so a None
    return mid-flight is treated as a hard error by the consumer.
    """
    routes = get_cached_routes()
    if routes is not None:
        row = routes.get(name)
        if row is not None and row.enabled and row.discord_channel_id:
            return int(row.discord_channel_id)
        # Row exists but discord_channel_id is NULL (V020 seed without the
        # operator promoting an env ID into DB), or row.enabled=False, or
        # row missing entirely — fall through to settings so the bot
        # keeps working on a freshly-seeded but un-configured DB.

    try:
        cid = get_settings().discord_channel(name)
    except KeyError:
        return None
    return cid or None


def _embed_color_for(channel_name: str, section_default: int) -> int:
    """Prefer DB embed_color when present, else the YAML rule's color."""
    routes = get_cached_routes()
    if routes is not None:
        row = routes.get(channel_name)
        if row is not None and row.embed_color is not None:
            return int(row.embed_color)
    return int(section_default)


def route_for(opp: dict[str, Any], kind: str = "lane") -> dict[str, Any]:
    """Return {channel_id, channel_name, embed_color, forum, push_threshold}.

    `kind` controls which routing-rules section is used:
      - "lane"    → per_lane.<category>
      - "tracker" → tracker.<state-or-key>
      - "alert"   → alerts.<key> (caller supplies opp={"alert": <key>})
    """
    rules = _rules()
    defaults = rules.get("defaults", {}) or {}

    if kind == "lane":
        cat = (opp.get("category") or "unknown").lower()
        section = (rules.get("per_lane") or {}).get(cat) or (rules.get("per_lane") or {}).get("unknown") or {}
    elif kind == "tracker":
        key = (opp.get("tracker") or opp.get("state") or "applied").lower()
        section = (rules.get("tracker") or {}).get(key) or {}
    elif kind == "alert":
        key = (opp.get("alert") or "pipeline_silent_5m").lower()
        section = (rules.get("alerts") or {}).get(key) or {}
    else:
        section = {}

    channel_name = section.get("channel") or defaults.get("daily_digest_channel") or "daily_digest"
    section_color = int(section.get("embed_color", DEFAULT_COLOR))
    return {
        "channel_id": channel_id_for(channel_name),
        "channel_name": channel_name,
        "embed_color": _embed_color_for(channel_name, section_color),
        "forum": bool(section.get("forum", False)),
        "push_threshold": float(section.get("push_threshold", 1.01)),
        "mention_owner": bool(section.get("mention_owner", False)),
    }


def color_for_lane(category: str) -> int:
    rules = _rules()
    section = (rules.get("per_lane") or {}).get((category or "unknown").lower()) or {}
    section_color = int(section.get("embed_color", DEFAULT_COLOR))
    # If the DB has a row for the lane's channel name and that row carries
    # an embed_color, that wins (tenant override).
    channel_name = section.get("channel") or "daily_digest"
    return _embed_color_for(channel_name, section_color)

"""Map opportunity → Discord channel + embed color.

Reads `config/routing_rules.yaml` at runtime (so ops can edit without
redeploying) and the channel-id table from settings env (sourced from
SOPS-encrypted `secrets.yaml`).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from src.common.logger import get_logger
from src.common.secrets import get_settings

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
    """Channel name → numeric ID, sourced from `discord_channel_<name>` Settings.

    pydantic BaseSettings auto-loads `DISCORD_CHANNEL_<NAME>` env vars
    (case-insensitive) which compose.yaml exports from SOPS. Returns None if
    the channel is unset (0); callers should fail loudly via
    `get_settings().assert_channels_configured(...)` at startup.
    """
    try:
        cid = get_settings().discord_channel(name)
    except KeyError:
        return None
    return cid or None


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
        section = (rules.get("per_lane") or {}).get(cat) \
                  or (rules.get("per_lane") or {}).get("unknown") \
                  or {}
    elif kind == "tracker":
        key = (opp.get("tracker") or opp.get("state") or "applied").lower()
        section = (rules.get("tracker") or {}).get(key) or {}
    elif kind == "alert":
        key = (opp.get("alert") or "pipeline_silent_5m").lower()
        section = (rules.get("alerts") or {}).get(key) or {}
    else:
        section = {}

    channel_name = section.get("channel") or defaults.get("daily_digest_channel") or "daily_digest"
    return {
        "channel_id": channel_id_for(channel_name),
        "channel_name": channel_name,
        "embed_color": section.get("embed_color", DEFAULT_COLOR),
        "forum": bool(section.get("forum", False)),
        "push_threshold": float(section.get("push_threshold", 1.01)),
        "mention_owner": bool(section.get("mention_owner", False)),
    }


def color_for_lane(category: str) -> int:
    rules = _rules()
    section = (rules.get("per_lane") or {}).get((category or "unknown").lower()) or {}
    return int(section.get("embed_color", DEFAULT_COLOR))

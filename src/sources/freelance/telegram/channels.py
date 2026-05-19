"""Channel config + source-id resolution.

Reads `freelance.telegram_channels` from `config/profile/prefs.yaml` (empty
list when the file is absent, malformed, or the key is missing — never
crashes). Also resolves the `sources.id` row for `crawler_strategy =
'freelance_telegram'` so the loop has a stable source_id at boot.

All logging keys (`tg_*`) are byte-identical to pre-refactor telegram_fetcher
because Grafana dashboards key on them.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from src.common.db import acquire
from src.common.logger import get_logger
from src.common.secrets import get_settings

from .parser import _normalise_channel

_log = get_logger(__name__)


def load_channels_from_prefs() -> list[str]:
    """Read freelance.telegram_channels from prefs.yaml. Empty list on miss."""
    settings = get_settings()
    prefs_path = Path(settings.config_root) / "profile" / "prefs.yaml"
    if not prefs_path.exists():
        return []
    try:
        data = yaml.safe_load(prefs_path.read_text()) or {}
    except yaml.YAMLError as e:
        _log.warning("tg_prefs_parse_failed", err=str(e))
        return []
    raw = (data.get("freelance") or {}).get("telegram_channels") or []
    if not isinstance(raw, list):
        return []
    return [c for c in (_normalise_channel(str(x)) for x in raw) if c]


async def resolve_source_id() -> int | None:
    """Look up the sources row for the freelance_telegram strategy. None on miss."""
    async with acquire() as conn:
        rec = await conn.fetchrow("SELECT id FROM sources WHERE crawler_strategy = 'freelance_telegram' LIMIT 1")
    return int(rec["id"]) if rec else None

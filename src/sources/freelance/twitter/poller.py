"""HTTP fetch + per-iteration poll loop.

`fetch_handle` walks the mirror rotator until one returns 2xx, then parses
the page and filters for hiring intent. `_poll_once` walks every configured
handle for the current poll cycle, respecting the per-mirror cool-down +
per-handle daily cap.

All logging keys (`tw_*`) are byte-identical to pre-refactor twitter_fetcher
because Grafana dashboards key on them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml

from src.common.db import acquire
from src.common.logger import get_logger
from src.common.queue import RedisQ
from src.common.secrets import get_settings

from .cap import _DailyBudget
from .mirrors import _PER_MIRROR_MIN_GAP_SECONDS, NITTER_INSTANCES, _MirrorRotator
from .parser import TweetMatch, _normalise_handle, matches_hiring, parse_tweet_html

_log = get_logger(__name__)


# ---- config + db lookups ---------------------------------------------------


def load_handles_from_prefs() -> list[str]:
    """Read freelance.twitter_handles from prefs.yaml. Empty list on miss."""
    settings = get_settings()
    prefs_path = Path(settings.config_root) / "profile" / "prefs.yaml"
    if not prefs_path.exists():
        return []
    try:
        data = yaml.safe_load(prefs_path.read_text()) or {}
    except yaml.YAMLError as e:
        _log.warning("tw_prefs_parse_failed", err=str(e))
        return []
    raw = (data.get("freelance") or {}).get("twitter_handles") or []
    if not isinstance(raw, list):
        return []
    return [h for h in (_normalise_handle(str(x)) for x in raw) if h]


async def resolve_source_id() -> int | None:
    async with acquire() as conn:
        rec = await conn.fetchrow("SELECT id FROM sources WHERE crawler_strategy = 'twitter_founder_signal' LIMIT 1")
    return int(rec["id"]) if rec else None


# ---- fetch_handle decomposition --------------------------------------------


def _build_fetch_url(mirror: str, handle: str) -> str:
    """Compose the canonical `<mirror>/<handle>` URL (no trailing slash)."""
    return f"{mirror.rstrip('/')}/{handle}"


async def _fetch_with_retry(
    *,
    http_client: httpx.AsyncClient,
    rotator: _MirrorRotator,
    handle: str,
) -> tuple[httpx.Response, str] | None:
    """Pick mirrors until one returns a 2xx; cools the bad ones along the way.

    Returns `(response, mirror)` on success, None if every mirror failed.
    On 404 (handle-specific) we return None without burning more mirrors.
    """
    last_err: str | None = None
    for _ in range(len(NITTER_INSTANCES)):
        mirror = rotator.pick()
        if mirror is None:
            break  # nothing ready yet; let caller back off
        url = _build_fetch_url(mirror, handle)
        try:
            resp = await http_client.get(url)
        except httpx.HTTPError as e:
            last_err = f"transport:{type(e).__name__}"
            _log.info("tw_mirror_transport_error", mirror=mirror, handle=handle, err=str(e))
            rotator.cool(mirror)
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            last_err = f"http:{resp.status_code}"
            _log.info(
                "tw_mirror_http_error",
                mirror=mirror,
                handle=handle,
                status=resp.status_code,
            )
            rotator.cool(mirror)
            continue
        if resp.status_code == 404:
            _log.info("tw_handle_missing_on_mirror", mirror=mirror, handle=handle)
            rotator.cool(mirror, gap=_PER_MIRROR_MIN_GAP_SECONDS)
            # 404 is handle-specific, not mirror-specific — return None so
            # caller doesn't burn the daily budget retrying everywhere.
            return None
        if resp.status_code >= 400:
            last_err = f"http:{resp.status_code}"
            _log.info("tw_mirror_4xx", mirror=mirror, handle=handle, status=resp.status_code)
            rotator.cool(mirror)
            continue
        # 2xx — success. Mark mirror cooled so we rotate next call.
        rotator.cool(mirror)
        return resp, mirror

    _log.warning("tw_all_mirrors_failed", handle=handle, last_err=last_err)
    return None


def _filter_hiring(matches: list[TweetMatch]) -> list[TweetMatch]:
    """Keep only TweetMatch entries that hit the hiring-intent regex."""
    return [m for m in matches if matches_hiring(m.text)]


async def fetch_handle(
    handle: str,
    *,
    http_client: httpx.AsyncClient,
    rotator: _MirrorRotator,
) -> list[TweetMatch]:
    """Fetch + parse one handle from the first available healthy mirror.

    Returns the matched TweetMatch list (empty if nothing matches or every
    mirror is down). Never raises — all transport errors are logged and
    swallowed; the caller treats an empty list as "try again later".
    """
    fetched = await _fetch_with_retry(http_client=http_client, rotator=rotator, handle=handle)
    if fetched is None:
        return []
    resp, mirror = fetched
    matches = parse_tweet_html(resp.text, handle=handle)
    hits = _filter_hiring(matches)
    _log.info(
        "tw_handle_fetched",
        mirror=mirror,
        handle=handle,
        tweets_parsed=len(matches),
        hiring_hits=len(hits),
    )
    return hits


# ---- _poll_once + PollContext ----------------------------------------------


@dataclass(frozen=True, slots=True)
class PollContext:
    """Bundle of per-cycle collaborators handed to `_poll_once`.

    Collapses what was previously 6 kwargs into a single dependency object
    so callers can extend without churn (and the function signature stays
    under the project's 5-param cap).
    """

    source_id: int
    q: RedisQ
    http_client: httpx.AsyncClient
    rotator: _MirrorRotator
    budget: _DailyBudget

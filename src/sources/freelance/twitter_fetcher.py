"""Freelance Twitter/X founder-signal fetcher (Phase 3.1).

Polls a configurable list of Nitter mirrors (public Twitter front-ends that
don't require auth) for a user-curated set of founder/recruiter handles in
`config/profile/prefs.yaml -> freelance.twitter_handles`. Filters each handle's
recent tweets for hiring-intent keywords, parses matches into
`Opportunity` payloads, and publishes them directly onto `stream:rank` via
`persist_and_publish` — bypassing the crawler / extractor tiers (Nitter
output is structured enough that selectolax + regex is sufficient).

Hard constraints (see task brief + CLAUDE.md):
  * No Twitter API key required — Nitter only. All mirrors down ⇒ log +
    retry; never crash the loop.
  * Worker boots cleanly with `freelance.twitter_handles: []` — logs
    `tw_no_handles_configured` and idles.
  * Read-only: never reply / DM / follow / like. Just GET <mirror>/<handle>.
  * Rate-limit: 1 request per Nitter instance per 30s (per-instance lock).
  * Daily-fetch budget: 10 polls per handle per day.
  * Fingerprint = sha256(f"twitter:{handle}:{tweet_id}") — restart-safe.
  * Import-clean even when the network is down (httpx is lazy at run-time).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from selectolax.parser import HTMLParser

from src.common.db import acquire, close_pool, init_pool
from src.common.logger import get_logger
from src.common.queue import RedisQ
from src.common.secrets import get_settings
from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType
from src.extractors.persist import persist_and_publish

_log = get_logger(__name__)

# ---- constants --------------------------------------------------------------

# Canonical Nitter mirrors. Curated list of currently-reachable instances;
# updates land here (not in config) because the upstream wiki rotates fast
# and we keep the worker import-time deterministic. If the entire list goes
# dark the worker logs `tw_all_mirrors_failed` per poll cycle and idles.
NITTER_INSTANCES: tuple[str, ...] = (
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
)

# Hiring-intent vocabulary. Word-boundary anchored; case-insensitive. Keep
# patterns short + recall-biased: ranker + LLM rerank filter false positives.
_HIRING_PATTERNS = re.compile(
    r"\b("
    r"hiring|"
    r"looking for|"
    r"we'?re recruiting|"
    r"join us|"
    r"we need|"
    r"paid (?:project|gig)|"
    r"contract role|"
    r"freelance project"
    r")\b",
    re.IGNORECASE,
)

# Category hints. Checked in order: most-specific first so 'full-time' wins
# over 'freelance' when both appear.
_RX_FULLTIME = re.compile(r"\b(full[-\s]?time|FT|permanent role)\b", re.IGNORECASE)
_RX_INTERN = re.compile(r"\b(intern(?:ship)?s?)\b", re.IGNORECASE)

# Loop cadence. 30 min poll matches `sources.fetch_freq_minutes` row in V015.
_POLL_INTERVAL_SECONDS = 30 * 60

# Per-mirror minimum gap between requests, to play nice with operators.
_PER_MIRROR_MIN_GAP_SECONDS = 30.0

# Per-handle hard cap per UTC day. 24 handles * 10 = 240 fetches/day worst case.
_PER_HANDLE_DAILY_MAX = 10

# HTTP request settings.
_HTTP_TIMEOUT = 12.0
_HTTP_USER_AGENT = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Cartograph-Hop/1.0 (+contact via repo owner)"

# Sizing caps (mirrors telegram_fetcher).
_DESC_MAX = 2000
_TITLE_MAX = 200

# Idle loop tick when no handles are configured.
_IDLE_SLEEP_SECONDS = 300


@dataclass(frozen=True, slots=True)
class TweetMatch:
    """Structured view of a single Nitter-page tweet that hit the filter."""

    tweet_id: str
    handle: str
    text: str
    link: str
    posted_at: datetime | None
    fingerprint_hash: str


# ---- pure helpers (unit-tested without network) -----------------------------


def _normalise_handle(handle: str) -> str:
    """Strip @, twitter.com prefix, trailing slash. Lowercase preserved? No.

    Twitter handles are case-insensitive but display preserves case; we
    canonicalise to lowercase so the fingerprint is stable regardless of
    how the user typed it in prefs.yaml.
    """
    h = handle.strip()
    if not h:
        return ""
    for prefix in (
        "https://twitter.com/",
        "https://x.com/",
        "twitter.com/",
        "x.com/",
    ):
        if h.startswith(prefix):
            h = h[len(prefix) :]
            break
    return h.lstrip("@").rstrip("/").lower()


def _fingerprint(handle: str, tweet_id: str) -> str:
    """sha256(twitter:handle:tweet_id) — deterministic, restart-safe."""
    return hashlib.sha256(f"twitter:{handle}:{tweet_id}".encode()).hexdigest()


def matches_hiring(text: str) -> bool:
    """True if text contains any hiring-intent keyword."""
    return bool(_HIRING_PATTERNS.search(text or ""))


def infer_category(text: str) -> OppCategory:
    """Pick the most-specific category for a hiring tweet."""
    if _RX_INTERN.search(text or ""):
        return OppCategory.INTERNSHIP
    if _RX_FULLTIME.search(text or ""):
        return OppCategory.FULLTIME
    return OppCategory.FREELANCE


def parse_tweet_html(html: str, *, handle: str) -> list[TweetMatch]:
    """Extract (tweet_id, text, link, posted_at) tuples from a Nitter handle page.

    Nitter renders tweets as `<div class="timeline-item">` blocks. Each block
    has:
      * `.tweet-link` href like `/{handle}/status/{tweet_id}#m`.
      * `.tweet-content` body text.
      * `.tweet-date a` `title` attribute with the absolute timestamp.

    We avoid DOM-fragile attribute matching: only the three stable class names
    above are required. Missing pieces silently skip the tweet (no crash).
    """
    if not html:
        return []

    handle_lc = handle.lower()
    out: list[TweetMatch] = []
    tree = HTMLParser(html)

    for item in tree.css(".timeline-item"):
        link_node = item.css_first(".tweet-link")
        if link_node is None:
            continue
        href = (link_node.attributes.get("href") or "").strip()
        # href looks like '/handle/status/1234567890#m' — extract id.
        m = re.search(r"/status/(\d+)", href)
        if not m:
            continue
        tweet_id = m.group(1)

        body_node = item.css_first(".tweet-content")
        text = (body_node.text(separator=" ", strip=True) if body_node else "").strip()
        if not text:
            continue

        # Posted-at: best-effort, never blocks the match.
        posted_at: datetime | None = None
        date_anchor = item.css_first(".tweet-date a")
        if date_anchor is not None:
            title = (date_anchor.attributes.get("title") or "").strip()
            posted_at = _parse_nitter_timestamp(title)

        link = f"https://twitter.com/{handle_lc}/status/{tweet_id}"
        out.append(
            TweetMatch(
                tweet_id=tweet_id,
                handle=handle_lc,
                text=text,
                link=link,
                posted_at=posted_at,
                fingerprint_hash=_fingerprint(handle_lc, tweet_id),
            )
        )
    return out


def _parse_nitter_timestamp(raw: str) -> datetime | None:
    """Nitter titles look like 'Jan 15, 2026 · 4:32 PM UTC'. Best-effort parse."""
    if not raw:
        return None
    cleaned = raw.replace("·", " ").strip()
    # Try a few well-known formats; never crash.
    for fmt in ("%b %d, %Y  %I:%M %p UTC", "%b %d, %Y %I:%M %p UTC", "%b %d, %Y"):
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def tweet_to_opportunity(match: TweetMatch, *, source_id: int) -> Opportunity:
    """Wrap a `TweetMatch` in the canonical `Opportunity` payload."""
    title = match.text.splitlines()[0].strip()[:_TITLE_MAX] or match.text.strip()[:_TITLE_MAX]
    description = match.text[:_DESC_MAX]
    category = infer_category(match.text)
    return Opportunity(
        source_id=source_id,
        canonical_url=match.link,
        title=title,
        company=None,
        description=description,
        comp_min=None,
        comp_max=None,
        comp_currency=None,
        comp_period=None,
        location=None,
        remote_type=RemoteType.UNSPECIFIED,
        category=category,
        posted_at=match.posted_at,
        apply_url=match.link,
        apply_method=ApplyMethod.EXTERNAL,
        fingerprint_hash=match.fingerprint_hash,
        extraction_tier=0,
        extraction_confidence=0.5,
    )


# ---- config + db lookups ----------------------------------------------------


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


# ---- per-mirror + per-handle rate limiting ----------------------------------


class _MirrorRotator:
    """Round-robin mirror picker that enforces a per-mirror cool-down.

    The simple invariant: each mirror's *next* fetch may not begin earlier
    than (last_fetched + _PER_MIRROR_MIN_GAP_SECONDS). On 4xx/5xx we mark
    the mirror as cooled for the same gap; the rotator naturally rotates
    away to the next healthy one.
    """

    def __init__(self, mirrors: tuple[str, ...]) -> None:
        self._mirrors = list(mirrors)
        # monotonic timestamps; 0.0 = never used.
        self._next_ok: dict[str, float] = {m: 0.0 for m in mirrors}

    def pick(self) -> str | None:
        """Return the mirror with the earliest next_ok <= now; else None.

        Caller can `await asyncio.sleep(rotator.wait_hint())` then retry.
        """
        now = time.monotonic()
        # Sort by readiness, ascending — earliest-ready wins.
        in_order = sorted(self._mirrors, key=lambda m: self._next_ok.get(m, 0.0))
        head = in_order[0]
        if self._next_ok.get(head, 0.0) <= now:
            return head
        return None

    def cool(self, mirror: str, *, gap: float = _PER_MIRROR_MIN_GAP_SECONDS) -> None:
        self._next_ok[mirror] = time.monotonic() + gap

    def wait_hint(self) -> float:
        """Seconds until the soonest mirror is ready. 0 if one is ready now."""
        now = time.monotonic()
        soonest = min(self._next_ok.values()) if self._next_ok else now
        return max(0.0, soonest - now)


class _DailyBudget:
    """Tracks per-handle fetch count per UTC day. Resets at midnight UTC."""

    def __init__(self, cap: int = _PER_HANDLE_DAILY_MAX) -> None:
        self._cap = cap
        self._day: date = datetime.now(UTC).date()
        self._counts: dict[str, int] = {}

    def _rollover_if_needed(self) -> None:
        today = datetime.now(UTC).date()
        if today != self._day:
            self._day = today
            self._counts.clear()

    def allowed(self, handle: str) -> bool:
        self._rollover_if_needed()
        return self._counts.get(handle, 0) < self._cap

    def increment(self, handle: str) -> None:
        self._rollover_if_needed()
        self._counts[handle] = self._counts.get(handle, 0) + 1


# ---- HTTP fetch -------------------------------------------------------------


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
    last_err: str | None = None
    # Try up to len(mirrors) times — each unhealthy mirror cools out.
    for _ in range(len(NITTER_INSTANCES)):
        mirror = rotator.pick()
        if mirror is None:
            break  # nothing ready yet; let caller back off
        url = f"{mirror.rstrip('/')}/{handle}"
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
            # 404 is handle-specific, not mirror-specific — return empty so
            # caller doesn't burn the daily budget retrying everywhere.
            return []
        if resp.status_code >= 400:
            last_err = f"http:{resp.status_code}"
            _log.info("tw_mirror_4xx", mirror=mirror, handle=handle, status=resp.status_code)
            rotator.cool(mirror)
            continue
        # 2xx — success. Mark mirror cooled so we rotate next call.
        rotator.cool(mirror)
        matches = parse_tweet_html(resp.text, handle=handle)
        hits = [m for m in matches if matches_hiring(m.text)]
        _log.info(
            "tw_handle_fetched",
            mirror=mirror,
            handle=handle,
            tweets_parsed=len(matches),
            hiring_hits=len(hits),
        )
        return hits

    _log.warning("tw_all_mirrors_failed", handle=handle, last_err=last_err)
    return []


# ---- runtime --------------------------------------------------------------


async def _publish_with_dedupe(
    q: RedisQ,
    opp: Opportunity,
    *,
    handle: str,
    tweet_id: str,
) -> None:
    """Persist + publish. Swallow unique-violation (dedupe) at debug level."""
    try:
        opp_id = await persist_and_publish(q, opp)
        if opp_id is None:
            _log.debug("tw_dedupe_skip", handle=handle, tweet_id=tweet_id)
            return
        _log.info(
            "tw_opportunity_published",
            handle=handle,
            tweet_id=tweet_id,
            opportunity_id=str(opp_id),
        )
    except Exception as e:
        sqlstate = getattr(e, "sqlstate", None)
        if sqlstate == "23505":
            _log.debug(
                "tw_dedupe_skip",
                handle=handle,
                tweet_id=tweet_id,
                sqlstate=sqlstate,
            )
            return
        _log.exception("tw_publish_failed", handle=handle, tweet_id=tweet_id, err=str(e))


async def _poll_once(
    handles: list[str],
    *,
    source_id: int,
    q: RedisQ,
    http_client: httpx.AsyncClient,
    rotator: _MirrorRotator,
    budget: _DailyBudget,
) -> None:
    """One pass over every configured handle. Respects daily budget + per-mirror gap."""
    for handle in handles:
        if not budget.allowed(handle):
            _log.debug("tw_handle_budget_exhausted", handle=handle)
            continue
        # Wait for a mirror to be ready (per-mirror cool-down).
        wait = rotator.wait_hint()
        if wait > 0:
            await asyncio.sleep(min(wait, _PER_MIRROR_MIN_GAP_SECONDS))
        budget.increment(handle)
        matches = await fetch_handle(handle, http_client=http_client, rotator=rotator)
        for tm in matches:
            opp = tweet_to_opportunity(tm, source_id=source_id)
            await _publish_with_dedupe(q, opp, handle=handle, tweet_id=tm.tweet_id)


async def run() -> None:
    """Worker entrypoint. Idempotent + restart-safe."""
    _log.info("tw_fetcher_started", mirrors=NITTER_INSTANCES)

    handles = load_handles_from_prefs()
    if not handles:
        _log.info("tw_no_handles_configured")
    else:
        _log.info("tw_handles_configured", count=len(handles), handles=handles)

    # DB + Redis come up regardless so health checks pass + the worker can
    # idle harmlessly when handles is empty (matches telegram_fetcher).
    await init_pool()
    q = await RedisQ.connect()

    source_id = await resolve_source_id()
    if source_id is None:
        _log.warning("tw_source_id_missing", strategy="twitter_founder_signal")

    rotator = _MirrorRotator(NITTER_INSTANCES)
    budget = _DailyBudget()

    headers = {"User-Agent": _HTTP_USER_AGENT, "Accept": "text/html"}

    try:
        async with httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT,
            headers=headers,
            follow_redirects=True,
        ) as client:
            while True:
                # Re-read prefs each loop so the user can append handles
                # without a worker restart.
                current_handles = load_handles_from_prefs()
                if not current_handles or source_id is None:
                    _log.debug(
                        "tw_idle_tick",
                        reason="no_handles" if not current_handles else "no_source_id",
                    )
                    await asyncio.sleep(_IDLE_SLEEP_SECONDS)
                    continue

                try:
                    await _poll_once(
                        current_handles,
                        source_id=source_id,
                        q=q,
                        http_client=client,
                        rotator=rotator,
                        budget=budget,
                    )
                except (asyncio.CancelledError, KeyboardInterrupt):
                    raise
                except Exception as e:
                    _log.exception("tw_poll_error", err=str(e))

                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    except (asyncio.CancelledError, KeyboardInterrupt):
        _log.info("tw_shutdown")
    finally:
        await close_pool()


# Re-export for tests/back-compat. Tests touch `_publish_with_dedupe`,
# `parse_tweet_html`, `matches_hiring`, `infer_category`, `_fingerprint`,
# `tweet_to_opportunity`, `load_handles_from_prefs`, `_normalise_handle`,
# `_MirrorRotator`, `_DailyBudget`, `run`.
__all__: tuple[str, ...] = (
    "NITTER_INSTANCES",
    "TweetMatch",
    "fetch_handle",
    "infer_category",
    "load_handles_from_prefs",
    "matches_hiring",
    "parse_tweet_html",
    "resolve_source_id",
    "run",
    "tweet_to_opportunity",
)


# Touch unused name to keep mypy happy when Any import is unused; harmless.
_ = Any

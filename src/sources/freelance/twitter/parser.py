"""Nitter HTML parser + tweet → Opportunity adapter.

All pure: zero network, zero DB. Safe to import at module-load time even
when the network is down. Behaviour MUST stay byte-identical to the
original `twitter_fetcher.py` — the hiring-keyword regex, category hints,
and fingerprint hashing are load-bearing across Grafana dashboards.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from selectolax.parser import HTMLParser

from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType

# ---- constants (byte-identical to pre-refactor twitter_fetcher.py) ---------

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

# Sizing caps (mirrors telegram_fetcher).
_DESC_MAX = 2000
_TITLE_MAX = 200


@dataclass(frozen=True, slots=True)
class TweetMatch:
    """Structured view of a single Nitter-page tweet that hit the filter."""

    tweet_id: str
    handle: str
    text: str
    link: str
    posted_at: datetime | None
    fingerprint_hash: str


# ---- pure helpers ----------------------------------------------------------


def _normalise_handle(handle: str) -> str:
    """Strip @, twitter.com prefix, trailing slash; lowercase the result.

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


# ---- Nitter HTML parsing ---------------------------------------------------


def _select_tweet_nodes(html: str) -> list:
    """Return the `.timeline-item` blocks from a Nitter handle page."""
    if not html:
        return []
    tree = HTMLParser(html)
    return tree.css(".timeline-item")


def _extract_tweet_meta(item) -> tuple[str, str] | None:
    """Return (tweet_id, href) for a timeline-item, or None if unparseable.

    Nitter `.tweet-link` href looks like `/handle/status/1234567890#m`.
    """
    link_node = item.css_first(".tweet-link")
    if link_node is None:
        return None
    href = (link_node.attributes.get("href") or "").strip()
    m = re.search(r"/status/(\d+)", href)
    if not m:
        return None
    return m.group(1), href


def _extract_tweet_text(item) -> str:
    """Return the `.tweet-content` body text, stripped. Empty on miss."""
    body_node = item.css_first(".tweet-content")
    return (body_node.text(separator=" ", strip=True) if body_node else "").strip()


def _extract_posted_at(item) -> datetime | None:
    """Best-effort timestamp parse from `.tweet-date a[title]`. None on miss."""
    date_anchor = item.css_first(".tweet-date a")
    if date_anchor is None:
        return None
    title = (date_anchor.attributes.get("title") or "").strip()
    return _parse_nitter_timestamp(title)


def _normalise_tweet_dict(*, tweet_id: str, handle_lc: str, text: str, posted_at: datetime | None) -> TweetMatch:
    """Build the canonical TweetMatch payload (twitter.com link, fingerprint)."""
    link = f"https://twitter.com/{handle_lc}/status/{tweet_id}"
    return TweetMatch(
        tweet_id=tweet_id,
        handle=handle_lc,
        text=text,
        link=link,
        posted_at=posted_at,
        fingerprint_hash=_fingerprint(handle_lc, tweet_id),
    )


def parse_tweet_html(html: str, *, handle: str) -> list[TweetMatch]:
    """Extract `TweetMatch` records from a Nitter handle page.

    Nitter renders tweets as `<div class="timeline-item">` blocks. Each block
    has:
      * `.tweet-link` href like `/{handle}/status/{tweet_id}#m`.
      * `.tweet-content` body text.
      * `.tweet-date a` `title` attribute with the absolute timestamp.

    Missing pieces silently skip the tweet (no crash).
    """
    handle_lc = handle.lower()
    out: list[TweetMatch] = []
    for item in _select_tweet_nodes(html):
        meta = _extract_tweet_meta(item)
        if meta is None:
            continue
        tweet_id, _href = meta
        text = _extract_tweet_text(item)
        if not text:
            continue
        out.append(
            _normalise_tweet_dict(
                tweet_id=tweet_id,
                handle_lc=handle_lc,
                text=text,
                posted_at=_extract_posted_at(item),
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

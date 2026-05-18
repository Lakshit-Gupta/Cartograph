"""Generic RSS / Atom feed extractor.

Strategy: ``rss_generic`` — covers RemoteOK, WeWorkRemotely, and any other
feed-shaped source seeded in V003. Falls back to tier-2 LLM only if the feed
fails to parse or yields zero entries.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from time import struct_time

import feedparser

from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType
from src.extractors.base import ExtractInput, ExtractOutput
from src.extractors.tier1_selectors import register


def _fp(*parts: str) -> str:
    return hashlib.sha1("|".join(p.lower() for p in parts).encode()).hexdigest()  # noqa: S324


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# Match "<Role> at <Company>" — pull the company tail.
_AT_RE = re.compile(r"\s+at\s+(.+?)\s*$", re.IGNORECASE)


def _strip_html(html: str) -> str:
    text = _TAG_RE.sub(" ", html or "")
    return _WS_RE.sub(" ", text).strip()


def _to_utc(parsed: struct_time | None) -> datetime | None:
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def _guess_company(entry: dict, title: str) -> str | None:
    author = entry.get("author")
    if author and isinstance(author, str) and author.strip():
        return author.strip()
    m = _AT_RE.search(title)
    if m:
        return m.group(1).strip()
    return None


def _clean_title(title: str) -> str:
    # If we matched "Role at Company", strip the trailing " at Company"
    return _AT_RE.sub("", title).strip()


@register("rss_generic")
async def extract(inp: ExtractInput) -> ExtractOutput:
    parsed = feedparser.parse(inp.content)
    entries = parsed.get("entries") or []
    if not entries:
        return ExtractOutput(opps=[], tier_used=1, confidence=0.0)

    opps: list[Opportunity] = []
    for entry in entries:
        title_raw = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not title_raw or not link:
            continue
        company = _guess_company(entry, title_raw)
        title = _clean_title(title_raw)
        desc = _strip_html(entry.get("summary") or "")[:1200]
        posted = _to_utc(entry.get("published_parsed") or entry.get("updated_parsed"))

        haystack = (title + " " + desc).lower()
        category = OppCategory.INTERNSHIP if "intern" in title.lower() else OppCategory.FULLTIME
        remote = RemoteType.REMOTE if "remote" in haystack else RemoteType.UNSPECIFIED

        opps.append(
            Opportunity(
                source_id=inp.source_id,
                canonical_url=link,
                title=title,
                company=company,
                description=desc,
                remote_type=remote,
                category=category,
                posted_at=posted,
                apply_url=link,
                apply_method=ApplyMethod.EXTERNAL,
                fingerprint_hash=_fp(company or "", title, "", str(posted)[:10] if posted else ""),
                extraction_tier=1,
                extraction_confidence=0.82,
            )
        )
    return ExtractOutput(opps=opps, tier_used=1, confidence=0.82 if opps else 0.0)

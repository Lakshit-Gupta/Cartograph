"""r/forhire post extractor (and r/remotejs, r/freelance_forhire)."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime

from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType
from src.extractors.base import ExtractInput, ExtractOutput
from src.extractors.tier1_selectors import register


def _fp(*parts: str) -> str:
    return hashlib.sha1("|".join(p.lower() for p in parts).encode()).hexdigest()  # noqa: S324


_HIRING_RE = re.compile(r"^\[hiring\]", re.IGNORECASE)


@register("reddit_oauth")
@register("reddit_oauth_push")
async def extract(inp: ExtractInput) -> ExtractOutput:
    try:
        data = json.loads(inp.content)
    except json.JSONDecodeError:
        return ExtractOutput(opps=[], tier_used=1, confidence=0.0)

    children = (data.get("data") or {}).get("children") or []
    opps: list[Opportunity] = []
    for c in children:
        post = c.get("data") or {}
        title = post.get("title") or ""
        # Only include [Hiring] posts on r/forhire
        if "forhire" in (post.get("subreddit", "").lower()) and not _HIRING_RE.match(title):
            continue
        body = post.get("selftext") or ""
        perma = f"https://reddit.com{post.get('permalink', '')}"
        created = post.get("created_utc")
        posted = datetime.fromtimestamp(created, tz=UTC) if created else None
        remote = RemoteType.REMOTE if "remote" in (title + body).lower() else RemoteType.UNSPECIFIED
        category = OppCategory.FREELANCE if "freelance" in title.lower() or "contract" in title.lower() else OppCategory.FULLTIME
        opps.append(
            Opportunity(
                source_id=inp.source_id,
                canonical_url=perma,
                title=title[:200],
                company=None,
                description=body[:1200],
                remote_type=remote,
                category=category,
                posted_at=posted,
                apply_url=perma,
                apply_method=ApplyMethod.EXTERNAL,
                fingerprint_hash=_fp("reddit", title[:80], str(posted)[:10] if posted else "", ""),
                extraction_tier=1,
                extraction_confidence=0.7,
            )
        )
    return ExtractOutput(opps=opps, tier_used=1, confidence=0.7 if opps else 0.0)

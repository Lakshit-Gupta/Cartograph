"""HN Algolia 'Who is hiring' comment extractor — splits comment threads into opps."""
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


_LOC_RE = re.compile(r"\b(remote|hybrid|onsite|onsite\s*only|in[\s-]*office)\b", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", re.IGNORECASE)


@register("hn_algolia")
async def extract(inp: ExtractInput) -> ExtractOutput:
    try:
        data = json.loads(inp.content)
    except json.JSONDecodeError:
        return ExtractOutput(opps=[], tier_used=1, confidence=0.0)

    hits = data.get("hits") or []
    opps: list[Opportunity] = []
    for h in hits:
        comment_html = h.get("comment_text") or ""
        if not comment_html or len(comment_html) < 30:
            continue
        plain = re.sub(r"<[^>]+>", " ", comment_html).strip()
        first_line = next((ln for ln in plain.splitlines() if ln.strip()), "")[:200]
        if not first_line:
            continue
        m = _LOC_RE.search(plain)
        remote = (
            RemoteType.REMOTE if m and "remote" in m.group(0).lower() else
            RemoteType.HYBRID if m and "hybrid" in m.group(0).lower() else
            RemoteType.ONSITE if m else
            RemoteType.UNSPECIFIED
        )
        email_m = _EMAIL_RE.search(plain)
        apply_url = f"mailto:{email_m.group(0)}" if email_m else f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        ts = h.get("created_at_i")
        posted = datetime.fromtimestamp(ts, tz=UTC) if ts else None
        opps.append(Opportunity(
            source_id=inp.source_id,
            canonical_url=f"https://news.ycombinator.com/item?id={h.get('objectID')}",
            title=first_line,
            company=None,
            description=plain[:1200],
            remote_type=remote,
            category=OppCategory.FULLTIME,
            posted_at=posted,
            apply_url=apply_url,
            apply_method=ApplyMethod.EMAIL if email_m else ApplyMethod.EXTERNAL,
            fingerprint_hash=_fp("hn", first_line, "", str(posted)[:10] if posted else ""),
            extraction_tier=1,
            extraction_confidence=0.6,
        ))
    return ExtractOutput(opps=opps, tier_used=1, confidence=0.6 if opps else 0.0)

"""Greenhouse JSON API extractor."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType
from src.extractors.base import ExtractInput, ExtractOutput
from src.extractors.tier1_selectors import register


def _fp(*parts: str) -> str:
    return hashlib.sha1("|".join(p.lower() for p in parts).encode()).hexdigest()  # noqa: S324


@register("ats_greenhouse")
async def extract(inp: ExtractInput) -> ExtractOutput:
    try:
        data = json.loads(inp.content)
    except json.JSONDecodeError:
        return ExtractOutput(opps=[], tier_used=1, confidence=0.0)

    opps: list[Opportunity] = []
    jobs = data.get("jobs") or data.get("data") or []
    for j in jobs:
        title = j.get("title") or ""
        if not title:
            continue
        company = (j.get("company") or {}).get("name") or j.get("departments", [{}])[0].get("name")
        location = (j.get("location") or {}).get("name")
        absolute_url = j.get("absolute_url") or j.get("url") or inp.url
        updated_at_str = j.get("updated_at") or j.get("created_at")
        posted: datetime | None = None
        if updated_at_str:
            try:
                posted = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
            except ValueError:
                posted = None
        desc_html = j.get("content") or ""
        # Naive HTML strip
        desc = " ".join(chunk for chunk in desc_html.replace("<", " <").split(" ") if not chunk.startswith("<"))[:1200]

        title_lower = title.lower()
        category = (
            OppCategory.INTERNSHIP
            if "intern" in title_lower
            else OppCategory.FELLOWSHIP
            if "fellow" in title_lower or "residency" in title_lower
            else OppCategory.FULLTIME
        )
        loc_lower = (location or "").lower()
        remote = (
            RemoteType.REMOTE
            if "remote" in loc_lower or "anywhere" in loc_lower
            else RemoteType.HYBRID
            if "hybrid" in loc_lower
            else RemoteType.UNSPECIFIED
        )
        opps.append(
            Opportunity(
                source_id=inp.source_id,
                canonical_url=absolute_url,
                title=title,
                company=company,
                description=desc,
                location=location,
                remote_type=remote,
                category=category,
                posted_at=posted,
                apply_url=absolute_url,
                apply_method=ApplyMethod.ATS_FORM,
                fingerprint_hash=_fp(company or "", title, location or "", str(posted)[:10] if posted else ""),
                extraction_tier=1,
                extraction_confidence=0.92,
            )
        )
    return ExtractOutput(opps=opps, tier_used=1, confidence=0.92 if opps else 0.0)

"""Unstop public JSON API extractor."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType
from src.extractors.base import ExtractInput, ExtractOutput
from src.extractors.tier1_selectors import register


def _fp(*parts: str) -> str:
    return hashlib.sha1("|".join(p.lower() for p in parts).encode()).hexdigest()  # noqa: S324


@register("india_unstop")
async def extract(inp: ExtractInput) -> ExtractOutput:
    try:
        data = json.loads(inp.content)
    except json.JSONDecodeError:
        return ExtractOutput(opps=[], tier_used=1, confidence=0.0)

    items = data.get("data", {}).get("data") or data.get("data") or []
    if not isinstance(items, list):
        return ExtractOutput(opps=[], tier_used=1, confidence=0.0)

    opps: list[Opportunity] = []
    for j in items:
        title = j.get("title") or j.get("name")
        if not title:
            continue
        org = (j.get("organisation") or {}).get("name") if isinstance(j.get("organisation"), dict) else j.get("organization_name")
        is_internship = "intern" in (j.get("type") or "").lower() or "intern" in title.lower()
        cat = OppCategory.INTERNSHIP if is_internship else OppCategory.FELLOWSHIP if "fellowship" in title.lower() else OppCategory.FULLTIME
        slug = j.get("public_url") or j.get("slug")
        url = f"https://unstop.com/o/{slug}" if slug else inp.url
        posted: datetime | None = None
        if j.get("start_date") or j.get("created_at"):
            try:
                posted = datetime.fromisoformat(str(j.get("start_date") or j.get("created_at")).replace("Z", "+00:00"))
            except ValueError:
                posted = None
        opps.append(Opportunity(
            source_id=inp.source_id,
            canonical_url=url,
            title=title,
            company=org,
            description=(j.get("description") or "")[:1200],
            comp_min=float(j.get("prize_amount", 0)) or None,
            comp_currency="INR",
            comp_period="month" if is_internship else "year",
            location=j.get("location"),
            remote_type=RemoteType.UNSPECIFIED,
            category=cat,
            posted_at=posted,
            apply_url=url,
            apply_method=ApplyMethod.IN_PLATFORM,
            fingerprint_hash=_fp(org or "", title, str(posted)[:10] if posted else "", ""),
            extraction_tier=1,
            extraction_confidence=0.84,
        ))
    return ExtractOutput(opps=opps, tier_used=1, confidence=0.84 if opps else 0.0)

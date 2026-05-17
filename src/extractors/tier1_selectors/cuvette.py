"""Cuvette mobile-API JSON extractor."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType
from src.extractors.base import ExtractInput, ExtractOutput
from src.extractors.tier1_selectors import register


def _fp(*parts: str) -> str:
    return hashlib.sha1("|".join(p.lower() for p in parts).encode()).hexdigest()  # noqa: S324


@register("india_cuvette")
async def extract(inp: ExtractInput) -> ExtractOutput:
    try:
        data = json.loads(inp.content)
    except json.JSONDecodeError:
        return ExtractOutput(opps=[], tier_used=1, confidence=0.0)

    jobs = data.get("jobs") or data.get("data") or []
    opps: list[Opportunity] = []
    for j in jobs:
        title = j.get("role") or j.get("title")
        if not title:
            continue
        company = (j.get("company") or {}).get("name") if isinstance(j.get("company"), dict) else j.get("companyName")
        location = j.get("location") or j.get("city")
        remote = (j.get("workMode") or "").lower()
        stipend_low = j.get("stipendMin") or j.get("salaryMin")
        stipend_high = j.get("stipendMax") or j.get("salaryMax")
        currency = j.get("currency") or "INR"
        url = j.get("applicationLink") or j.get("url") or inp.url
        posted: datetime | None = None
        if j.get("createdAt"):
            try:
                posted = datetime.fromisoformat(str(j["createdAt"]).replace("Z", "+00:00"))
            except ValueError:
                posted = None

        category = OppCategory.INTERNSHIP if "intern" in str(j.get("type", "")).lower() or "intern" in title.lower() else OppCategory.FULLTIME
        opps.append(Opportunity(
            source_id=inp.source_id,
            canonical_url=url,
            title=title,
            company=company,
            description=(j.get("description") or "")[:1200],
            comp_min=float(stipend_low) if stipend_low else None,
            comp_max=float(stipend_high) if stipend_high else None,
            comp_currency=currency,
            comp_period="month" if "intern" in title.lower() else "year",
            location=location,
            remote_type=RemoteType.REMOTE if remote == "remote" else (RemoteType.HYBRID if remote == "hybrid" else RemoteType.UNSPECIFIED),
            category=category,
            posted_at=posted,
            apply_url=url,
            apply_method=ApplyMethod.IN_PLATFORM,
            fingerprint_hash=_fp(company or "", title, location or "", str(posted)[:10] if posted else ""),
            extraction_tier=1,
            extraction_confidence=0.86,
        ))
    return ExtractOutput(opps=opps, tier_used=1, confidence=0.86 if opps else 0.0)

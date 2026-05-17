"""Ashby JSON API extractor."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType
from src.extractors.base import ExtractInput, ExtractOutput
from src.extractors.tier1_selectors import register


def _fp(*parts: str) -> str:
    return hashlib.sha1("|".join(p.lower() for p in parts).encode()).hexdigest()  # noqa: S324


@register("ats_ashby")
async def extract(inp: ExtractInput) -> ExtractOutput:
    try:
        data = json.loads(inp.content)
    except json.JSONDecodeError:
        return ExtractOutput(opps=[], tier_used=1, confidence=0.0)

    payload = data.get("jobs") or data.get("postings") or []
    if not isinstance(payload, list):
        return ExtractOutput(opps=[], tier_used=1, confidence=0.0)

    opps: list[Opportunity] = []
    for j in payload:
        title = j.get("title") or ""
        if not title:
            continue
        org = j.get("organizationName") or j.get("teamName")
        location = (j.get("locationName") or j.get("location") or "")
        workplace = (j.get("workplaceType") or "").lower()
        absolute_url = j.get("jobUrl") or j.get("applicationUrl") or inp.url
        posted: datetime | None = None
        if j.get("publishedAt"):
            try:
                posted = datetime.fromisoformat(j["publishedAt"].replace("Z", "+00:00"))
            except ValueError:
                posted = None
        desc = (j.get("descriptionPlain") or j.get("descriptionHtml") or "")[:1200]
        comp = j.get("compensation") or {}
        comp_min = comp.get("compensationTierSummary", {}).get("minValue")
        comp_max = comp.get("compensationTierSummary", {}).get("maxValue")
        comp_cur = comp.get("compensationTierSummary", {}).get("currencyCode")

        category = (
            OppCategory.INTERNSHIP if "intern" in title.lower() else
            OppCategory.FELLOWSHIP if "fellow" in title.lower() else
            OppCategory.FULLTIME
        )
        remote = (
            RemoteType.REMOTE if workplace == "remote" else
            RemoteType.HYBRID if workplace == "hybrid" else
            RemoteType.ONSITE if workplace == "onsite" else
            RemoteType.UNSPECIFIED
        )
        opps.append(Opportunity(
            source_id=inp.source_id,
            canonical_url=absolute_url,
            title=title,
            company=org,
            description=desc,
            comp_min=float(comp_min) if comp_min else None,
            comp_max=float(comp_max) if comp_max else None,
            comp_currency=comp_cur,
            location=location,
            remote_type=remote,
            category=category,
            posted_at=posted,
            apply_url=absolute_url,
            apply_method=ApplyMethod.ATS_FORM,
            fingerprint_hash=_fp(org or "", title, location, str(posted)[:10] if posted else ""),
            extraction_tier=1,
            extraction_confidence=0.93,
        ))
    return ExtractOutput(opps=opps, tier_used=1, confidence=0.93 if opps else 0.0)

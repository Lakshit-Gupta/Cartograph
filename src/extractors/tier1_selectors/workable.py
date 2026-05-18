"""Workable extractor — widget JSON or HTML fallback."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType
from src.extractors.base import ExtractInput, ExtractOutput
from src.extractors.tier1_selectors import register


def _fp(*parts: str) -> str:
    return hashlib.sha1("|".join(p.lower() for p in parts).encode()).hexdigest()  # noqa: S324


@register("ats_workable")
async def extract(inp: ExtractInput) -> ExtractOutput:
    try:
        data = json.loads(inp.content)
    except json.JSONDecodeError:
        return ExtractOutput(opps=[], tier_used=1, confidence=0.0)

    jobs = data.get("jobs") or data.get("results") or []
    opps: list[Opportunity] = []
    for j in jobs:
        title = j.get("title") or j.get("name")
        if not title:
            continue
        company = j.get("company") or j.get("departmentName")
        location = (j.get("location") or {}).get("city") if isinstance(j.get("location"), dict) else j.get("location")
        remote_flag = (j.get("remote") or "").lower() if isinstance(j.get("remote"), str) else j.get("remote")
        absolute_url = j.get("url") or j.get("apply_url") or inp.url
        posted: datetime | None = None
        if j.get("published"):
            try:
                posted = datetime.fromisoformat(str(j["published"]).replace("Z", "+00:00"))
            except ValueError:
                posted = None
        desc = (j.get("description") or "")[:1200]

        opps.append(
            Opportunity(
                source_id=inp.source_id,
                canonical_url=absolute_url,
                title=title,
                company=company,
                description=desc,
                location=location,
                remote_type=RemoteType.REMOTE if remote_flag is True or str(remote_flag).lower() == "true" else RemoteType.UNSPECIFIED,
                category=OppCategory.INTERNSHIP if "intern" in title.lower() else OppCategory.FULLTIME,
                posted_at=posted,
                apply_url=absolute_url,
                apply_method=ApplyMethod.ATS_FORM,
                fingerprint_hash=_fp(company or "", title, location or "", str(posted)[:10] if posted else ""),
                extraction_tier=1,
                extraction_confidence=0.88,
            )
        )
    return ExtractOutput(opps=opps, tier_used=1, confidence=0.88 if opps else 0.0)

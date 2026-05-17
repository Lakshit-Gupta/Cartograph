"""Lever JSON API extractor."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType
from src.extractors.base import ExtractInput, ExtractOutput
from src.extractors.tier1_selectors import register


def _fp(*parts: str) -> str:
    return hashlib.sha1("|".join(p.lower() for p in parts).encode()).hexdigest()  # noqa: S324


@register("ats_lever")
async def extract(inp: ExtractInput) -> ExtractOutput:
    try:
        data = json.loads(inp.content)
    except json.JSONDecodeError:
        return ExtractOutput(opps=[], tier_used=1, confidence=0.0)

    if not isinstance(data, list):
        return ExtractOutput(opps=[], tier_used=1, confidence=0.0)

    opps: list[Opportunity] = []
    for j in data:
        title = j.get("text") or ""
        if not title:
            continue
        team = j.get("categories", {}).get("team")
        location = j.get("categories", {}).get("location")
        commitment = (j.get("categories", {}).get("commitment") or "").lower()
        workplace = (j.get("workplaceType") or "").lower()
        absolute_url = j.get("hostedUrl") or j.get("applyUrl") or inp.url
        ts_ms = j.get("createdAt")
        posted: datetime | None = None
        if ts_ms:
            try:
                posted = datetime.fromtimestamp(int(ts_ms) / 1000, tz=UTC)
            except (ValueError, TypeError):
                posted = None
        desc_lists = j.get("lists") or []
        desc = " ".join(
            (lst.get("content") or "") for lst in desc_lists
        )[:1200]

        category = (
            OppCategory.INTERNSHIP if "intern" in commitment or "intern" in title.lower() else
            OppCategory.FELLOWSHIP if "fellow" in title.lower() else
            OppCategory.FULLTIME
        )
        remote = (
            RemoteType.REMOTE if workplace == "remote" else
            RemoteType.HYBRID if workplace == "hybrid" else
            RemoteType.ONSITE if workplace == "on-site" else
            RemoteType.UNSPECIFIED
        )
        opps.append(Opportunity(
            source_id=inp.source_id,
            canonical_url=absolute_url,
            title=title,
            company=team,
            description=desc,
            location=location,
            remote_type=remote,
            category=category,
            posted_at=posted,
            apply_url=absolute_url,
            apply_method=ApplyMethod.ATS_FORM,
            fingerprint_hash=_fp(team or "", title, location or "", str(posted)[:10] if posted else ""),
            extraction_tier=1,
            extraction_confidence=0.92,
        ))
    return ExtractOutput(opps=opps, tier_used=1, confidence=0.92 if opps else 0.0)

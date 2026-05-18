"""Contra freelance opportunities."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType
from src.extractors.base import ExtractInput, ExtractOutput
from src.extractors.tier1_selectors import register


def _fp(*parts: str) -> str:
    return hashlib.sha1("|".join(p.lower() for p in parts).encode()).hexdigest()  # noqa: S324


@register("freelance_contra")
async def extract(inp: ExtractInput) -> ExtractOutput:
    try:
        data = json.loads(inp.content)
    except json.JSONDecodeError:
        return ExtractOutput(opps=[], tier_used=1, confidence=0.0)

    opps_data = data.get("opportunities") or data.get("data") or []
    opps: list[Opportunity] = []
    for o in opps_data:
        title = o.get("title")
        if not title:
            continue
        # `o.get("budget", {})` returns None if the key is present with a
        # null value — coerce to dict first.
        budget_raw = o.get("budget")
        budget = budget_raw if isinstance(budget_raw, dict) else {}
        budget_min = o.get("budgetMin") or budget.get("min")
        budget_max = o.get("budgetMax") or budget.get("max")
        budget_cur = o.get("budgetCurrency") or "USD"
        period = o.get("rateType") or "hour"
        if str(period).lower() in ("fixed", "project"):
            period = None
        posted: datetime | None = None
        if o.get("createdAt"):
            try:
                posted = datetime.fromisoformat(str(o["createdAt"]).replace("Z", "+00:00"))
            except ValueError:
                posted = None
        slug = o.get("slug") or o.get("id")
        url = f"https://contra.com/opportunity/{slug}" if slug else inp.url
        opps.append(
            Opportunity(
                source_id=inp.source_id,
                canonical_url=url,
                title=title,
                company=o.get("client", {}).get("name") if isinstance(o.get("client"), dict) else None,
                description=(o.get("description") or "")[:1200],
                comp_min=float(budget_min) if budget_min else None,
                comp_max=float(budget_max) if budget_max else None,
                comp_currency=budget_cur,
                comp_period=str(period).lower() if period else None,
                remote_type=RemoteType.REMOTE,
                category=OppCategory.FREELANCE,
                posted_at=posted,
                apply_url=url,
                apply_method=ApplyMethod.IN_PLATFORM,
                fingerprint_hash=_fp("contra", title, "", str(posted)[:10] if posted else ""),
                extraction_tier=1,
                extraction_confidence=0.9,
            )
        )
    return ExtractOutput(opps=opps, tier_used=1, confidence=0.9 if opps else 0.0)

"""Superteam Earn bounty extractor.

Superteam Earn (https://earn.superteam.fun) hosts Solana-ecosystem
bounties. The public listings API at
``/api/listings?type=bounty&order=desc`` returns JSON with title,
rewards (in USDC by default), deadline, slug, and sponsor.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType
from src.extractors.base import ExtractInput, ExtractOutput
from src.extractors.tier1_selectors import register

_STALE_DAYS = 14


def _fp(*parts: str) -> str:
    return hashlib.sha1("|".join(p.lower() for p in parts).encode()).hexdigest()  # noqa: S324


def _is_stale(posted: datetime | None) -> bool:
    if posted is None:
        return False
    return posted < datetime.now(UTC) - timedelta(days=_STALE_DAYS)


@register("bounty_superteam")
async def extract(inp: ExtractInput) -> ExtractOutput:
    try:
        data = json.loads(inp.content)
    except json.JSONDecodeError:
        return ExtractOutput(opps=[], tier_used=1, confidence=0.0)

    items = data if isinstance(data, list) else (data.get("listings") or data.get("data") or [])
    opps: list[Opportunity] = []
    for b in items:
        title = b.get("title")
        if not title:
            continue
        slug = b.get("slug") or b.get("id")
        url = f"https://earn.superteam.fun/listings/bounty/{slug}" if slug else inp.url
        try:
            amount = float(b.get("rewardAmount") or b.get("usdValue") or 0) or None
        except (TypeError, ValueError):
            amount = None
        currency = (b.get("token") or "USDC").upper()
        posted: datetime | None = None
        if b.get("createdAt"):
            try:
                posted = datetime.fromisoformat(str(b["createdAt"]).replace("Z", "+00:00"))
            except ValueError:
                posted = None
        if _is_stale(posted):
            continue
        sponsor = (b.get("sponsor") or {}).get("name") if isinstance(b.get("sponsor"), dict) else None
        opps.append(
            Opportunity(
                source_id=inp.source_id,
                canonical_url=url,
                title=title,
                company=sponsor,
                description=(b.get("description") or "")[:1200],
                comp_min=amount,
                comp_max=amount,
                comp_currency="USD" if currency in ("USDC", "USDT", "DAI") else currency,
                comp_period=None,
                remote_type=RemoteType.REMOTE,
                category=OppCategory.FREELANCE,
                posted_at=posted,
                apply_url=url,
                apply_method=ApplyMethod.IN_PLATFORM,
                fingerprint_hash=_fp("superteam", str(slug or title)),
                extraction_tier=1,
                extraction_confidence=0.85,
            )
        )
    return ExtractOutput(opps=opps, tier_used=1, confidence=0.85 if opps else 0.0)

"""Algora bounty JSON extractor.

Algora's public feed at /api/v1/bounties/feed.json returns a list of
bounty objects with title, description, reward amount, currency,
project info, and a stable `/<org>/bounties/<id>` URL. Each bounty is
either GitHub-issue-backed or a self-contained task.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType
from src.extractors.base import ExtractInput, ExtractOutput
from src.extractors.tier1_selectors import register

# Bounties older than this are usually claimed already; drop them at
# extract time so they don't pollute the digest.
_STALE_DAYS = 14


def _fp(*parts: str) -> str:
    return hashlib.sha1("|".join(p.lower() for p in parts).encode()).hexdigest()  # noqa: S324


def _is_stale(posted: datetime | None) -> bool:
    if posted is None:
        return False
    return posted < datetime.now(UTC) - timedelta(days=_STALE_DAYS)


def _coerce_amount(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@register("bounty_algora")
async def extract(inp: ExtractInput) -> ExtractOutput:
    try:
        data = json.loads(inp.content)
    except json.JSONDecodeError:
        return ExtractOutput(opps=[], tier_used=1, confidence=0.0)

    items = data.get("bounties") or data.get("data") or (data if isinstance(data, list) else [])
    opps: list[Opportunity] = []
    for b in items:
        title = b.get("title") or b.get("name")
        if not title:
            continue
        reward = b.get("reward") if isinstance(b.get("reward"), dict) else {}
        amount = _coerce_amount(b.get("amount") or reward.get("amount") or reward.get("value"))
        currency = (b.get("currency") or reward.get("currency") or "USD").upper()
        posted_raw = b.get("createdAt") or b.get("created_at")
        posted: datetime | None = None
        if posted_raw:
            try:
                posted = datetime.fromisoformat(str(posted_raw).replace("Z", "+00:00"))
            except ValueError:
                posted = None
        if _is_stale(posted):
            continue
        org = (b.get("org") or {}).get("handle") or b.get("organization")
        bounty_id = b.get("id") or b.get("number")
        url = b.get("url") or (f"https://console.algora.io/{org}/bounties/{bounty_id}" if org and bounty_id else inp.url)
        opps.append(
            Opportunity(
                source_id=inp.source_id,
                canonical_url=url,
                title=title,
                company=org,
                description=(b.get("description") or "")[:1200],
                comp_min=amount,
                comp_max=amount,
                comp_currency=currency,
                comp_period=None,
                remote_type=RemoteType.REMOTE,
                category=OppCategory.FREELANCE,
                posted_at=posted,
                apply_url=url,
                apply_method=ApplyMethod.IN_PLATFORM,
                fingerprint_hash=_fp("algora", str(org or ""), str(bounty_id or title)),
                extraction_tier=1,
                extraction_confidence=0.9,
            )
        )
    return ExtractOutput(opps=opps, tier_used=1, confidence=0.9 if opps else 0.0)

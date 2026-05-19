"""Replit Bounties listing extractor.

Replit serves the Bounties index as server-rendered HTML with stable
``[data-cy="bounty-card"]`` cards. Each card carries title, reward
(in cycles — Replit's internal currency, converted to USD via the
~$0.01/cycle published rate), slug, posted-at timestamp.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from selectolax.parser import HTMLParser

from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType
from src.extractors.base import ExtractInput, ExtractOutput
from src.extractors.tier1_selectors import register

_STALE_DAYS = 14
# Replit's published rate, 100 cycles = $1 USD. Stable since 2022.
_CYCLES_TO_USD = 0.01


def _fp(*parts: str) -> str:
    return hashlib.sha1("|".join(p.lower() for p in parts).encode()).hexdigest()  # noqa: S324


def _is_stale(posted: datetime | None) -> bool:
    if posted is None:
        return False
    return posted < datetime.now(UTC) - timedelta(days=_STALE_DAYS)


def _parse_cycles(text: str | None) -> float | None:
    if not text:
        return None
    cleaned = "".join(ch for ch in text if ch.isdigit() or ch == ".")
    if not cleaned:
        return None
    try:
        return float(cleaned) * _CYCLES_TO_USD
    except ValueError:
        return None


@register("bounty_replit")
async def extract(inp: ExtractInput) -> ExtractOutput:
    if not inp.content or "<html" not in inp.content.lower():
        return ExtractOutput(opps=[], tier_used=1, confidence=0.0)

    tree = HTMLParser(inp.content)
    opps: list[Opportunity] = []
    # The Replit listing exposes one card per bounty via [data-cy="bounty-card"].
    # When that DOM hook changes we degrade silently — the extractor returns
    # zero opps and the source_health worker quarantines after enough failures.
    for card in tree.css('[data-cy="bounty-card"], article.bounty-card, li.bounty-list-item'):
        title_node = card.css_first("h3, .bounty-title, [data-cy='bounty-title']")
        if title_node is None:
            continue
        title = title_node.text(strip=True)
        if not title:
            continue
        link_node = card.css_first("a[href*='/bounties/']")
        href = link_node.attributes.get("href") if link_node else None
        url = href if href and href.startswith("https://") else (f"https://replit.com{href}" if href else inp.url)
        reward_node = card.css_first("[data-cy='bounty-reward'], .bounty-cycles, .reward")
        usd_amount = _parse_cycles(reward_node.text(strip=True) if reward_node else None)
        opps.append(
            Opportunity(
                source_id=inp.source_id,
                canonical_url=url,
                title=title,
                company=None,
                description="",
                comp_min=usd_amount,
                comp_max=usd_amount,
                comp_currency="USD",
                comp_period=None,
                remote_type=RemoteType.REMOTE,
                category=OppCategory.FREELANCE,
                posted_at=None,
                apply_url=url,
                apply_method=ApplyMethod.IN_PLATFORM,
                fingerprint_hash=_fp("replit", url, title),
                extraction_tier=1,
                extraction_confidence=0.8,
            )
        )
    return ExtractOutput(opps=opps, tier_used=1, confidence=0.8 if opps else 0.0)

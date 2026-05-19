"""Gitcoin bounty REST extractor.

Gitcoin's public API at /api/v1/bounty/?status=open returns bounty
objects with title, description, value_in_token, token_name,
github_url (or standalone_bounties_metadata.tool_url), and timestamps.
Crypto-denominated bounties convert via a hardcoded ETH→USD rate; a
proper exchange-rate worker is deferred to Phase 4+.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType
from src.extractors.base import ExtractInput, ExtractOutput
from src.extractors.tier1_selectors import register

_STALE_DAYS = 14
# Crude on-disk exchange rate so the ranker can compare bounty rewards
# across currencies. Phase 4+ replaces this with a daily-refreshed worker
# that pulls live rates from CoinGecko or similar. Underestimates ETH
# slightly on purpose so we don't inflate the picker score.
_ETH_TO_USD = 3000.0
_USDC_TO_USD = 1.0


def _fp(*parts: str) -> str:
    return hashlib.sha1("|".join(p.lower() for p in parts).encode()).hexdigest()  # noqa: S324


def _is_stale(posted: datetime | None) -> bool:
    if posted is None:
        return False
    return posted < datetime.now(UTC) - timedelta(days=_STALE_DAYS)


def _to_usd(amount: float | None, token: str | None) -> float | None:
    if amount is None:
        return None
    sym = (token or "USD").upper()
    if sym in ("USD", "USDC", "DAI"):
        return amount * _USDC_TO_USD
    if sym in ("ETH", "WETH"):
        return amount * _ETH_TO_USD
    return amount  # Unknown token — pass through; ranker can compare in raw units.


@register("bounty_gitcoin")
async def extract(inp: ExtractInput) -> ExtractOutput:
    try:
        data = json.loads(inp.content)
    except json.JSONDecodeError:
        return ExtractOutput(opps=[], tier_used=1, confidence=0.0)

    items = data if isinstance(data, list) else (data.get("results") or data.get("data") or [])
    opps: list[Opportunity] = []
    for b in items:
        title = b.get("title") or b.get("name")
        if not title:
            continue
        amount_raw = b.get("value_in_token") or b.get("value_true") or b.get("value_in_usdt")
        try:
            amount = float(amount_raw) if amount_raw is not None else None
        except (TypeError, ValueError):
            amount = None
        token = b.get("token_name") or b.get("payout_token") or "USD"
        usd_amount = _to_usd(amount, token)
        posted_raw = b.get("web3_created") or b.get("created_on")
        posted: datetime | None = None
        if posted_raw:
            try:
                posted = datetime.fromisoformat(str(posted_raw).replace("Z", "+00:00"))
            except ValueError:
                posted = None
        if _is_stale(posted):
            continue
        url = b.get("url") or b.get("github_url") or inp.url
        opps.append(
            Opportunity(
                source_id=inp.source_id,
                canonical_url=url,
                title=title,
                company=b.get("bounty_owner_name") or b.get("bounty_owner_github_username"),
                description=(b.get("issue_description") or b.get("description") or "")[:1200],
                comp_min=usd_amount,
                comp_max=usd_amount,
                comp_currency="USD",
                comp_period=None,
                remote_type=RemoteType.REMOTE,
                category=OppCategory.FREELANCE,
                posted_at=posted,
                apply_url=url,
                apply_method=ApplyMethod.IN_PLATFORM,
                fingerprint_hash=_fp("gitcoin", str(b.get("standard_bounties_id") or b.get("pk") or title), url),
                extraction_tier=1,
                extraction_confidence=0.88,
            )
        )
    return ExtractOutput(opps=opps, tier_used=1, confidence=0.88 if opps else 0.0)

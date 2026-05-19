"""Superteam Earn bounty extractor.

Superteam Earn (https://superteam.fun) is the alive crypto/web3 bounty
+ jobs board for the Solana ecosystem. Public endpoint confirmed
2026-05-19:

    GET https://superteam.fun/api/listings

returns a top-level JSON ARRAY of OPEN listings (13 entries at a time
in practice). A single listing object has this shape (relevant fields
only — fields not listed here are ignored):

    {
      "id": "uuid",
      "title": "...",
      "slug": "...",
      "rewardAmount": 10000,         # in TOKEN UNITS, not minor units
      "token": "USDG" | "USDC" | "USDT" | "SOL" | "ETH" | ...,
      "minRewardAsk": null | number,
      "maxRewardAsk": null | number,
      "compensationType": "fixed" | "range" | "variable",
      "type": "bounty" | "project" | "hackathon",
      "deadline": "2026-06-01T00:00:00.000Z",
      "status": "OPEN" | ...,
      "sponsor": { "name": "...", "slug": "...", "logo": "..." }
    }

Stablecoin tokens (USDC/USDT/USDG/PYUSD/DAI) are 1:1 to USD. Native
ETH / SOL are converted via hardcoded rates — same approach the
Gitcoin extractor uses — until the exchange-rate worker lands.

The API does not expose `createdAt`. Upstream `status=OPEN` already
drops claimed listings, but as defence-in-depth any listing whose
`deadline` is more than 14 days in the past is dropped.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType
from src.extractors.base import ExtractInput, ExtractOutput
from src.extractors.tier1_selectors import register

_STALE_DAYS = 14

# Same hardcoded rate constants as bounty_gitcoin until Phase 4 ships
# the exchange-rate worker. Intentionally conservative so the picker
# never over-scores a bounty.
_ETH_TO_USD = 3000.0
_SOL_TO_USD = 150.0
_STABLECOINS = frozenset({"USD", "USDC", "USDT", "USDG", "PYUSD", "DAI"})


def _fp(*parts: str) -> str:
    return hashlib.sha1("|".join(p.lower() for p in parts).encode()).hexdigest()  # noqa: S324


def _coerce_amount(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _to_usd(amount: float | None, token: str | None) -> float | None:
    if amount is None:
        return None
    sym = (token or "USD").upper()
    if sym in _STABLECOINS:
        return amount
    if sym in ("ETH", "WETH"):
        return amount * _ETH_TO_USD
    if sym == "SOL":
        return amount * _SOL_TO_USD
    return amount  # Unknown token — pass through; comp_currency records the symbol.


def _parse_iso(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_stale_deadline(deadline: datetime | None) -> bool:
    if deadline is None:
        return False
    return deadline < datetime.now(UTC) - timedelta(days=_STALE_DAYS)


def _items_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [b for b in payload if isinstance(b, dict)]
    if not isinstance(payload, dict):
        return []
    for k in ("data", "listings", "items", "results"):
        v = payload.get(k)
        if isinstance(v, list):
            return [b for b in v if isinstance(b, dict)]
    return []


def _amount_range(listing: dict[str, Any]) -> tuple[float | None, float | None, str]:
    """Return (comp_min_usd, comp_max_usd, currency_label).

    `compensationType` controls which field is authoritative:
      - 'fixed'    → rewardAmount
      - 'range'    → minRewardAsk + maxRewardAsk
      - 'variable' → no number, sponsor decides
    """
    token = str(listing.get("token") or "USD").upper()
    fixed = _coerce_amount(listing.get("rewardAmount"))
    lo = _coerce_amount(listing.get("minRewardAsk"))
    hi = _coerce_amount(listing.get("maxRewardAsk"))
    if fixed is not None:
        usd = _to_usd(fixed, token)
        return usd, usd, token
    if lo is not None or hi is not None:
        return _to_usd(lo, token), _to_usd(hi, token), token
    return None, None, token


@register("bounty_superteam")
async def extract(inp: ExtractInput) -> ExtractOutput:
    try:
        data = json.loads(inp.content)
    except json.JSONDecodeError:
        return ExtractOutput(opps=[], tier_used=1, confidence=0.0)

    items = _items_from_payload(data)
    opps: list[Opportunity] = []
    for b in items:
        status = str(b.get("status") or "OPEN").upper()
        if status != "OPEN":
            continue
        title = b.get("title")
        if not title:
            continue
        slug = b.get("slug") or b.get("id")
        kind = str(b.get("type") or "bounty").lower()
        # Canonical URL pattern is /listing/<slug> — verified against
        # `superteam.fun/listing/...` links in the public UI.
        canonical = b.get("url") or (f"https://superteam.fun/listing/{slug}" if slug else inp.url)
        comp_min, comp_max, currency_label = _amount_range(b)
        deadline = _parse_iso(b.get("deadline"))
        if _is_stale_deadline(deadline):
            continue
        sponsor = b.get("sponsor") if isinstance(b.get("sponsor"), dict) else {}
        company = sponsor.get("name") or sponsor.get("slug")
        # `_count.Submission` telegraphs competition. Keep a one-liner.
        count = b.get("_count") if isinstance(b.get("_count"), dict) else {}
        submissions = count.get("Submission")
        suffix = f" — {submissions} submissions so far" if submissions else ""
        description = f"{kind.title()} listing on Superteam Earn{suffix}.".strip()
        opps.append(
            Opportunity(
                source_id=inp.source_id,
                canonical_url=canonical,
                title=title,
                company=company,
                description=description[:1200],
                comp_min=comp_min,
                comp_max=comp_max,
                comp_currency=currency_label,
                comp_period=None,
                remote_type=RemoteType.REMOTE,
                category=OppCategory.FREELANCE,
                posted_at=None,
                expires_at=deadline,
                apply_url=canonical,
                apply_method=ApplyMethod.IN_PLATFORM,
                fingerprint_hash=_fp("superteam", str(slug or title), str(b.get("id") or "")),
                extraction_tier=1,
                extraction_confidence=0.88,
            )
        )
    return ExtractOutput(opps=opps, tier_used=1, confidence=0.88 if opps else 0.0)

"""Algora bounty JSON extractor.

The documented `/api/v1/bounties/feed.json` endpoint is 404 as of
2026-05-19. The live data lives at the tRPC route
`https://console.algora.io/api/trpc/bounty.list` which returns:

    {
      "result": {
        "data": {
          "json": {
            "items": [ { ...bounty... }, ... ],
            "next_cursor": null
          }
        }
      }
    }

A single bounty entry has this shape (relevant fields only):

    {
      "id": "jbTPoQNe8WiwNcRg",
      "status": "open" | "paid" | ...,
      "kind": "dev",
      "org": {
        "handle": "twentyhq",
        "name": "Twenty",
        "github_handle": "twentyhq"
      },
      "created_at": "2026-05-19T06:34:05.313324Z",
      "task": {
        "title": "Reject invalid response header folding",
        "url": "https://github.com/urllib3/urllib3/pull/5034",
        "hash": "urllib3#5034",
        "repo_owner": "urllib3",
        "repo_name": "urllib3"
      },
      "reward": { "currency": "USD", "amount": 10000 },   # amount in cents
      "reward_formatted": "$100"
    }

Reward `amount` is in MINOR UNITS (cents for USD) — confirmed by
cross-checking with `reward_formatted`. We divide by 100 to get the
human-readable USD value the ranker expects.

The extractor also retains backward-compat with the legacy
`/api/v1/bounties/feed.json` shape — if Algora ever republishes it the
extractor still parses it. Both shapes are exercised in tests.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

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


def _parse_iso(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _items_from_payload(payload: Any) -> list[dict[str, Any]]:
    """Pull a list of bounty dicts from either the tRPC envelope or a flat list/feed."""
    if isinstance(payload, list):
        return [b for b in payload if isinstance(b, dict)]
    if not isinstance(payload, dict):
        return []
    # tRPC envelope: { result: { data: { json: { items: [...] } } } }
    try:
        items = payload["result"]["data"]["json"]["items"]
        if isinstance(items, list):
            return [b for b in items if isinstance(b, dict)]
    except (KeyError, TypeError):
        pass
    # Legacy / fallback shapes.
    for k in ("bounties", "items", "data", "results"):
        v = payload.get(k)
        if isinstance(v, list):
            return [b for b in v if isinstance(b, dict)]
    return []


def _amount_usd(bounty: dict[str, Any]) -> float | None:
    """Algora delivers `reward.amount` in minor units (cents for USD).

    Legacy feed shape used `amount` at the top level in major units —
    detect via presence of `reward_formatted` or the reward dict.
    """
    reward = bounty.get("reward") if isinstance(bounty.get("reward"), dict) else {}
    if reward:
        raw = _coerce_amount(reward.get("amount") or reward.get("value"))
        currency = (reward.get("currency") or "USD").upper()
        if raw is None:
            return None
        # tRPC ships cents — `reward_formatted` like "$100" maps to amount=10000.
        if currency in ("USD", "EUR", "GBP"):
            return raw / 100.0
        return raw
    raw = _coerce_amount(bounty.get("amount"))
    return raw


def _currency(bounty: dict[str, Any]) -> str:
    reward = bounty.get("reward") if isinstance(bounty.get("reward"), dict) else {}
    if reward:
        return str(reward.get("currency") or "USD").upper()
    return str(bounty.get("currency") or "USD").upper()


def _title_and_url(bounty: dict[str, Any], default_url: str) -> tuple[str | None, str]:
    """Pull title + canonical URL.

    tRPC: title lives under `task.title`, GitHub URL under `task.url`.
    Legacy: title is top-level, URL synthesized from org + id.
    """
    task = bounty.get("task") if isinstance(bounty.get("task"), dict) else {}
    title = task.get("title") or bounty.get("title") or bounty.get("name")
    url = task.get("url") or bounty.get("url")
    if not url:
        org = bounty.get("org")
        org_handle = org.get("handle") if isinstance(org, dict) else None
        bid = bounty.get("id") or bounty.get("number")
        if org_handle and bid:
            url = f"https://algora.io/{org_handle}/bounties/{bid}"
    return title, url or default_url


@register("bounty_algora")
async def extract(inp: ExtractInput) -> ExtractOutput:
    try:
        data = json.loads(inp.content)
    except json.JSONDecodeError:
        return ExtractOutput(opps=[], tier_used=1, confidence=0.0)

    items = _items_from_payload(data)
    opps: list[Opportunity] = []
    for b in items:
        # Only surface live bounties — the tRPC feed mixes open + paid.
        status = str(b.get("status") or "open").lower()
        if status not in ("open", "active"):
            continue
        title, url = _title_and_url(b, inp.url)
        if not title:
            continue
        amount = _amount_usd(b)
        currency = _currency(b)
        posted = _parse_iso(b.get("created_at") or b.get("createdAt"))
        if _is_stale(posted):
            continue
        org = b.get("org") if isinstance(b.get("org"), dict) else {}
        company = org.get("name") or org.get("handle") or b.get("organization")
        task = b.get("task") if isinstance(b.get("task"), dict) else {}
        description = task.get("body") or task.get("hash") or b.get("description") or ""
        bounty_id = b.get("id") or task.get("hash") or task.get("number") or title
        opps.append(
            Opportunity(
                source_id=inp.source_id,
                canonical_url=url,
                title=title,
                company=company,
                description=str(description)[:1200],
                comp_min=amount,
                comp_max=amount,
                comp_currency=currency,
                comp_period=None,
                remote_type=RemoteType.REMOTE,
                category=OppCategory.FREELANCE,
                posted_at=posted,
                apply_url=url,
                apply_method=ApplyMethod.IN_PLATFORM,
                fingerprint_hash=_fp("algora", str(company or ""), str(bounty_id)),
                extraction_tier=1,
                extraction_confidence=0.9,
            )
        )
    return ExtractOutput(opps=opps, tier_used=1, confidence=0.9 if opps else 0.0)

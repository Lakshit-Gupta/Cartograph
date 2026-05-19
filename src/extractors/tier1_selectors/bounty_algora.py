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
# Minor-unit currencies — `reward.amount` ships as cents and needs /100.
_MINOR_UNIT_CURRENCIES = frozenset({"USD", "EUR", "GBP"})
_MINOR_UNIT_DIVISOR = 100.0
_OPEN_STATUSES = frozenset({"open", "active"})
_DESCRIPTION_LIMIT = 1200


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


def _as_dict(value: object) -> dict[str, Any]:
    """Return value if it's a dict, else empty dict — cheap defensive read."""
    return value if isinstance(value, dict) else {}


# Top-level keys checked in the legacy / fallback shapes when the tRPC
# envelope is absent. Order = priority.
_LEGACY_LIST_KEYS = ("bounties", "items", "data", "results")


def _dict_list(value: object) -> list[dict[str, Any]]:
    """Filter a value down to a list of dicts; empty list if it isn't a list."""
    if isinstance(value, list):
        return [b for b in value if isinstance(b, dict)]
    return []


def _items_from_trpc_envelope(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    """tRPC envelope: { result: { data: { json: { items: [...] } } } }."""
    try:
        items = payload["result"]["data"]["json"]["items"]
    except (KeyError, TypeError):
        return None
    return _dict_list(items)


def _items_from_legacy_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """First key in `_LEGACY_LIST_KEYS` whose value is a list (even empty)."""
    for k in _LEGACY_LIST_KEYS:
        v = payload.get(k)
        if isinstance(v, list):
            return _dict_list(v)
    return []


def _items_from_payload(payload: Any) -> list[dict[str, Any]]:
    """Pull a list of bounty dicts from either the tRPC envelope or a flat list/feed."""
    if isinstance(payload, list):
        return _dict_list(payload)
    if not isinstance(payload, dict):
        return []
    trpc_items = _items_from_trpc_envelope(payload)
    if trpc_items is not None:
        return trpc_items
    return _items_from_legacy_payload(payload)


def _amount_usd(bounty: dict[str, Any]) -> float | None:
    """Algora delivers `reward.amount` in minor units (cents for USD).

    Legacy feed shape used `amount` at the top level in major units —
    detect via presence of `reward_formatted` or the reward dict.
    """
    reward = _as_dict(bounty.get("reward"))
    if reward:
        raw = _coerce_amount(reward.get("amount") or reward.get("value"))
        if raw is None:
            return None
        currency = (reward.get("currency") or "USD").upper()
        # tRPC ships cents — `reward_formatted` like "$100" maps to amount=10000.
        if currency in _MINOR_UNIT_CURRENCIES:
            return raw / _MINOR_UNIT_DIVISOR
        return raw
    return _coerce_amount(bounty.get("amount"))


def _currency(bounty: dict[str, Any]) -> str:
    reward = _as_dict(bounty.get("reward"))
    if reward:
        return str(reward.get("currency") or "USD").upper()
    return str(bounty.get("currency") or "USD").upper()


def _fallback_canonical_url(bounty: dict[str, Any]) -> str | None:
    """Synthesize an Algora URL when no `task.url` is provided (legacy shape)."""
    org_handle = _as_dict(bounty.get("org")).get("handle")
    bid = bounty.get("id") or bounty.get("number")
    if org_handle and bid:
        return f"https://algora.io/{org_handle}/bounties/{bid}"
    return None


def _title_and_url(bounty: dict[str, Any], default_url: str) -> tuple[str | None, str]:
    """Pull title + canonical URL.

    tRPC: title lives under `task.title`, GitHub URL under `task.url`.
    Legacy: title is top-level, URL synthesized from org + id.
    """
    task = _as_dict(bounty.get("task"))
    title = task.get("title") or bounty.get("title") or bounty.get("name")
    url = task.get("url") or bounty.get("url") or _fallback_canonical_url(bounty)
    return title, url or default_url


def _company(bounty: dict[str, Any]) -> str | None:
    org = _as_dict(bounty.get("org"))
    return org.get("name") or org.get("handle") or bounty.get("organization")


def _description(bounty: dict[str, Any]) -> str:
    task = _as_dict(bounty.get("task"))
    raw = task.get("body") or task.get("hash") or bounty.get("description") or ""
    return str(raw)[:_DESCRIPTION_LIMIT]


def _bounty_id(bounty: dict[str, Any], title: str) -> str:
    task = _as_dict(bounty.get("task"))
    return str(bounty.get("id") or task.get("hash") or task.get("number") or title)


def _is_open(bounty: dict[str, Any]) -> bool:
    """tRPC feed mixes open + paid — surface only live bounties."""
    status = str(bounty.get("status") or "open").lower()
    return status in _OPEN_STATUSES


def _to_opportunity(bounty: dict[str, Any], inp: ExtractInput) -> Opportunity | None:
    """Translate one bounty dict into an Opportunity, or None to skip.

    Skip conditions: non-open status, missing title, stale `created_at`.
    """
    if not _is_open(bounty):
        return None
    title, url = _title_and_url(bounty, inp.url)
    if not title:
        return None
    posted = _parse_iso(bounty.get("created_at") or bounty.get("createdAt"))
    if _is_stale(posted):
        return None
    amount = _amount_usd(bounty)
    company = _company(bounty)
    return Opportunity(
        source_id=inp.source_id,
        canonical_url=url,
        title=title,
        company=company,
        description=_description(bounty),
        comp_min=amount,
        comp_max=amount,
        comp_currency=_currency(bounty),
        comp_period=None,
        remote_type=RemoteType.REMOTE,
        category=OppCategory.FREELANCE,
        posted_at=posted,
        apply_url=url,
        apply_method=ApplyMethod.IN_PLATFORM,
        fingerprint_hash=_fp("algora", str(company or ""), _bounty_id(bounty, title)),
        extraction_tier=1,
        extraction_confidence=0.9,
    )


@register("bounty_algora")
async def extract(inp: ExtractInput) -> ExtractOutput:
    try:
        data = json.loads(inp.content)
    except json.JSONDecodeError:
        return ExtractOutput(opps=[], tier_used=1, confidence=0.0)
    opps = [o for o in (_to_opportunity(b, inp) for b in _items_from_payload(data)) if o is not None]
    return ExtractOutput(opps=opps, tier_used=1, confidence=0.9 if opps else 0.0)

"""Phase 3.3 bounty lane — hermetic extractor contract tests.

No live HTTP. Each test feeds a canned JSON/HTML payload into the
extractor and asserts the produced Opportunity rows match the spec.

Covers the four extractors that ship with the bounty lane:

  * bounty_algora       — Algora tRPC envelope + legacy flat shape.
  * bounty_replit       — defunct upstream, code retained, HTML shape.
  * bounty_gitcoin      — defunct upstream, code retained, REST shape.
  * bounty_superteam    — alive, JSON array shape with token rewards.

Each test name follows the contract called out in the Phase 3.3 brief:

  - test_<platform>_extracts_basic_bounty  (smoke per platform)
  - test_amount_parsing_handles_eth_to_usd
  - test_stale_bounty_skipped
  - test_dedupe_by_apply_url
"""

from __future__ import annotations

import json

import pytest

from src.common.types import ApplyMethod, OppCategory, RemoteType
from src.extractors.base import ExtractInput
from src.extractors.tier1_selectors.bounty_algora import extract as algora_extract
from src.extractors.tier1_selectors.bounty_gitcoin import extract as gitcoin_extract
from src.extractors.tier1_selectors.bounty_replit import extract as replit_extract
from src.extractors.tier1_selectors.bounty_superteam import extract as superteam_extract


def _ei(payload: dict | list | str, *, slug: str, url: str) -> ExtractInput:
    if isinstance(payload, (dict, list)):
        content = json.dumps(payload)
        ct = "application/json"
    else:
        content = payload
        ct = "text/html"
    return ExtractInput(
        source_id=1,
        source_slug=slug,
        url=url,
        content=content,
        content_type=ct,
    )


def _trpc(items: list[dict]) -> dict:
    """Wrap a bounty list in the tRPC envelope Algora actually returns."""
    return {"result": {"data": {"json": {"items": items, "next_cursor": None}}}}


# ---------- Algora -----------------------------------------------------------


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_algora_extracts_basic_bounty():
    """Algora tRPC envelope — reward.amount is in cents → divide by 100."""
    payload = _trpc(
        [
            {
                "id": "jbTPoQNe8WiwNcRg",
                "status": "open",
                "kind": "dev",
                "org": {"handle": "twentyhq", "name": "Twenty"},
                "created_at": "2026-05-19T06:34:05.313324Z",
                "task": {
                    "title": "Implement IMAP fetcher",
                    "url": "https://github.com/twentyhq/twenty/pull/1",
                    "hash": "twenty#1",
                },
                "reward": {"currency": "USD", "amount": 250000},  # cents → $2,500
                "reward_formatted": "$2,500",
            }
        ]
    )
    out = await algora_extract(_ei(payload, slug="bounty_algora", url="https://console.algora.io/api/trpc/bounty.list"))
    assert len(out.opps) == 1
    o = out.opps[0]
    assert o.title == "Implement IMAP fetcher"
    assert o.company == "Twenty"
    assert o.comp_min == 2500.0  # cents → dollars
    assert o.comp_max == 2500.0
    assert o.comp_currency == "USD"
    assert o.category == OppCategory.FREELANCE
    assert o.apply_method == ApplyMethod.IN_PLATFORM
    assert o.canonical_url == "https://github.com/twentyhq/twenty/pull/1"
    assert o.remote_type == RemoteType.REMOTE


@pytest.mark.asyncio
async def test_algora_legacy_flat_shape_still_parses():
    """Backward-compat — if Algora republishes the documented flat feed it still works."""
    out = await algora_extract(
        _ei(
            {
                "bounties": [
                    {
                        "id": 42,
                        "title": "Fix race in async-queue",
                        "description": "Long description here.",
                        "amount": 250,
                        "currency": "USD",
                        "createdAt": "2026-05-15T10:00:00Z",
                        "org": {"handle": "myorg"},
                    }
                ]
            },
            slug="bounty_algora",
            url="https://console.algora.io/api/v1/bounties/feed.json",
        )
    )
    assert len(out.opps) == 1
    assert out.opps[0].title == "Fix race in async-queue"
    # Flat shape stores reward as a top-level `amount` in major units,
    # NOT cents — see _amount_usd branch.
    assert out.opps[0].comp_min == 250


@pytest.mark.asyncio
async def test_algora_skips_paid_status():
    """tRPC mixes open + paid — only open should surface."""
    out = await algora_extract(
        _ei(
            _trpc(
                [
                    {
                        "id": "paidA",
                        "status": "paid",
                        "org": {"handle": "x"},
                        "created_at": "2026-05-18T00:00:00Z",
                        "task": {"title": "Already paid", "url": "https://gh.com/x/1"},
                        "reward": {"currency": "USD", "amount": 50000},
                    },
                ]
            ),
            slug="bounty_algora",
            url="https://console.algora.io/api/trpc/bounty.list",
        )
    )
    assert out.opps == []


@pytest.mark.asyncio
async def test_algora_returns_empty_on_invalid_json():
    out = await algora_extract(
        _ei("not-json", slug="bounty_algora", url="https://x"),
    )
    assert out.opps == []
    assert out.confidence == 0.0


# ---------- Gitcoin (defunct but code retained) ------------------------------


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_gitcoin_extracts_basic_bounty():
    out = await gitcoin_extract(
        _ei(
            [
                {
                    "pk": 1234,
                    "title": "USDC integration bounty",
                    "value_in_token": 500,
                    "token_name": "USDC",
                    "web3_created": "2026-05-18T10:00:00Z",
                    "url": "https://gitcoin.co/issue/1234",
                    "bounty_owner_name": "exampleOrg",
                }
            ],
            slug="bounty_gitcoin",
            url="https://gitcoin.co/api/v1/bounty/?status=open",
        )
    )
    assert len(out.opps) == 1
    o = out.opps[0]
    assert o.title == "USDC integration bounty"
    assert o.comp_min == 500.0
    assert o.comp_currency == "USD"
    assert o.category == OppCategory.FREELANCE
    assert o.apply_method == ApplyMethod.IN_PLATFORM


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_amount_parsing_handles_eth_to_usd():
    """Crypto bounties: ETH → USD at the hardcoded $3000/ETH rate."""
    out = await gitcoin_extract(
        _ei(
            [
                {
                    "pk": 999,
                    "title": "ETH-paid integration",
                    "value_in_token": 0.5,
                    "token_name": "ETH",
                    "web3_created": "2026-05-18T10:00:00Z",
                    "url": "https://gitcoin.co/issue/eth-paid",
                    "bounty_owner_name": "ethorg",
                }
            ],
            slug="bounty_gitcoin",
            url="https://gitcoin.co/api/v1/bounty/?status=open",
        )
    )
    assert len(out.opps) == 1
    o = out.opps[0]
    # 0.5 ETH * $3000 = $1500.
    assert o.comp_min == 1500.0
    assert o.comp_currency == "USD"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_stale_bounty_skipped():
    """Bounties older than 14 days are dropped at the extractor."""
    out = await gitcoin_extract(
        _ei(
            [
                {
                    "pk": 1,
                    "title": "Stale bounty from 2024",
                    "value_in_token": 100,
                    "token_name": "USDC",
                    "web3_created": "2024-01-01T00:00:00Z",
                    "url": "https://gitcoin.co/issue/stale",
                }
            ],
            slug="bounty_gitcoin",
            url="https://gitcoin.co/api/v1/bounty/?status=open",
        )
    )
    assert out.opps == []


# ---------- Replit (defunct but code retained) -------------------------------


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_replit_extracts_basic_bounty():
    """The HTML extractor still parses a bounty card if Replit ever revives the product."""
    html = """<html><body>
      <article class="bounty-card">
        <h3>Build a Discord bot</h3>
        <a href="/bounties/abc123">View</a>
        <span class="bounty-cycles">5000 cycles</span>
      </article>
    </body></html>"""
    out = await replit_extract(
        _ei(html, slug="bounty_replit", url="https://replit.com/bounties"),
    )
    assert len(out.opps) == 1
    o = out.opps[0]
    assert o.title == "Build a Discord bot"
    # 5000 cycles * $0.01/cycle = $50.
    assert o.comp_min == 50.0
    assert "/bounties/abc123" in o.canonical_url
    assert o.remote_type == RemoteType.REMOTE


@pytest.mark.asyncio
async def test_replit_returns_empty_on_non_html():
    out = await replit_extract(
        _ei("just plain text", slug="bounty_replit", url="https://replit.com/bounties"),
    )
    assert out.opps == []
    assert out.confidence == 0.0


# ---------- Superteam Earn ---------------------------------------------------


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_superteam_extracts_basic_bounty():
    """JSON array shape — fixed compensation in a stablecoin."""
    future_deadline = "2026-12-01T00:00:00.000Z"
    out = await superteam_extract(
        _ei(
            [
                {
                    "id": "uuid-1",
                    "title": "Write Solana SDK docs",
                    "slug": "write-solana-sdk-docs",
                    "rewardAmount": 1500,
                    "token": "USDC",
                    "compensationType": "fixed",
                    "type": "bounty",
                    "deadline": future_deadline,
                    "status": "OPEN",
                    "sponsor": {"name": "Solana Foundation", "slug": "solana"},
                    "_count": {"Submission": 3, "Comments": 7},
                }
            ],
            slug="bounty_superteam",
            url="https://superteam.fun/api/listings",
        )
    )
    assert len(out.opps) == 1
    o = out.opps[0]
    assert o.title == "Write Solana SDK docs"
    assert o.company == "Solana Foundation"
    assert o.comp_min == 1500.0
    assert o.comp_max == 1500.0
    # Stablecoin → comp_currency stays as the token symbol so the
    # ranker can distinguish USDC vs USDT vs USDG. USD value is in
    # comp_min/comp_max.
    assert o.comp_currency == "USDC"
    assert o.category == OppCategory.FREELANCE
    assert o.apply_method == ApplyMethod.IN_PLATFORM
    assert o.canonical_url == "https://superteam.fun/listing/write-solana-sdk-docs"
    assert "3 submissions so far" in o.description


@pytest.mark.asyncio
async def test_superteam_converts_sol_to_usd():
    """Native SOL converts at the hardcoded $150/SOL rate."""
    out = await superteam_extract(
        _ei(
            [
                {
                    "id": "uuid-sol",
                    "title": "SOL-paid bounty",
                    "slug": "sol-bounty",
                    "rewardAmount": 10,
                    "token": "SOL",
                    "compensationType": "fixed",
                    "type": "bounty",
                    "deadline": "2026-12-01T00:00:00.000Z",
                    "status": "OPEN",
                    "sponsor": {"name": "DeFi DAO"},
                }
            ],
            slug="bounty_superteam",
            url="https://superteam.fun/api/listings",
        )
    )
    assert len(out.opps) == 1
    # 10 SOL * $150 = $1500.
    assert out.opps[0].comp_min == 1500.0
    assert out.opps[0].comp_currency == "SOL"


@pytest.mark.asyncio
async def test_superteam_handles_range_reward():
    """compensationType='range' → min/max from minRewardAsk/maxRewardAsk."""
    out = await superteam_extract(
        _ei(
            [
                {
                    "id": "uuid-range",
                    "title": "Open-ended project",
                    "slug": "open-project",
                    "minRewardAsk": 500,
                    "maxRewardAsk": 2000,
                    "token": "USDC",
                    "compensationType": "range",
                    "type": "project",
                    "deadline": "2026-12-01T00:00:00.000Z",
                    "status": "OPEN",
                    "sponsor": {"name": "Builder DAO"},
                }
            ],
            slug="bounty_superteam",
            url="https://superteam.fun/api/listings",
        )
    )
    assert len(out.opps) == 1
    assert out.opps[0].comp_min == 500.0
    assert out.opps[0].comp_max == 2000.0


@pytest.mark.asyncio
async def test_superteam_skips_non_open_status():
    out = await superteam_extract(
        _ei(
            [
                {
                    "id": "x",
                    "title": "Closed listing",
                    "status": "CLOSED",
                    "rewardAmount": 100,
                    "token": "USDC",
                    "deadline": "2026-12-01T00:00:00.000Z",
                    "sponsor": {"name": "X"},
                }
            ],
            slug="bounty_superteam",
            url="https://superteam.fun/api/listings",
        )
    )
    assert out.opps == []


@pytest.mark.asyncio
async def test_superteam_skips_stale_deadline():
    """Listing whose deadline is >14 days in the past is dropped."""
    out = await superteam_extract(
        _ei(
            [
                {
                    "id": "old",
                    "title": "Expired bounty",
                    "slug": "expired",
                    "rewardAmount": 100,
                    "token": "USDC",
                    "compensationType": "fixed",
                    "status": "OPEN",
                    "deadline": "2024-01-01T00:00:00.000Z",
                    "sponsor": {"name": "Old DAO"},
                }
            ],
            slug="bounty_superteam",
            url="https://superteam.fun/api/listings",
        )
    )
    assert out.opps == []


# ---------- Cross-platform contracts -----------------------------------------


@pytest.mark.asyncio
async def test_dedupe_by_apply_url():
    """Two extractions of the same payload yield the same canonical_url AND
    the same fingerprint_hash so the dedup layer in extractor_worker.py
    collapses them to a single opportunities row."""
    payload = _trpc(
        [
            {
                "id": "samebountyid",
                "status": "open",
                "org": {"handle": "sameorg", "name": "Same Org"},
                "created_at": "2026-05-18T10:00:00Z",
                "task": {
                    "title": "Dedupe me",
                    "url": "https://github.com/sameorg/repo/pull/9",
                    "hash": "repo#9",
                },
                "reward": {"currency": "USD", "amount": 50000},
            }
        ]
    )
    a = await algora_extract(_ei(payload, slug="bounty_algora", url="https://x"))
    b = await algora_extract(_ei(payload, slug="bounty_algora", url="https://x"))
    assert len(a.opps) == 1 and len(b.opps) == 1
    assert a.opps[0].canonical_url == b.opps[0].canonical_url
    assert a.opps[0].fingerprint_hash == b.opps[0].fingerprint_hash
    assert a.opps[0].apply_url == a.opps[0].canonical_url


@pytest.mark.asyncio
async def test_strategies_registered_after_freelance_import():
    """Importing the freelance subpackage must self-register all four bounty
    plugins with the central source registry so the scheduler can dispatch
    them."""
    import src.sources.freelance  # noqa: F401 — side-effect import
    from src.sources.registry import get as get_plugin

    for strategy in ("bounty_algora", "bounty_replit", "bounty_gitcoin", "bounty_superteam"):
        assert get_plugin(strategy) is not None, f"{strategy} not registered"

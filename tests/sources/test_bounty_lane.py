"""Phase 3.3 bounty lane — hermetic extractor contract tests.

No live HTTP. Each test feeds a canned JSON/HTML payload into the
extractor and asserts the produced Opportunity rows match the spec.
"""

from __future__ import annotations

import json

import pytest

from src.common.types import ApplyMethod, OppCategory, RemoteType
from src.extractors.base import ExtractInput
from src.extractors.tier1_selectors.bounty_algora import extract as algora_extract
from src.extractors.tier1_selectors.bounty_gitcoin import extract as gitcoin_extract
from src.extractors.tier1_selectors.bounty_replit import extract as replit_extract


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


# ---------- Algora -----------------------------------------------------------


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_algora_extracts_basic_bounty():
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
    o = out.opps[0]
    assert o.title == "Fix race in async-queue"
    assert o.company == "myorg"
    assert o.comp_min == 250
    assert o.comp_currency == "USD"
    assert o.category == OppCategory.FREELANCE
    assert o.apply_method == ApplyMethod.IN_PLATFORM
    assert "myorg/bounties/42" in o.canonical_url


@pytest.mark.asyncio
async def test_algora_skips_stale_bounty():
    out = await algora_extract(
        _ei(
            {
                "bounties": [
                    {
                        "id": 7,
                        "title": "Old bounty",
                        "amount": 100,
                        "createdAt": "2024-01-01T00:00:00Z",
                        "org": {"handle": "stale"},
                    }
                ]
            },
            slug="bounty_algora",
            url="https://console.algora.io/x",
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


# ---------- Gitcoin ----------------------------------------------------------


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_gitcoin_converts_eth_to_usd():
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
    # 0.5 ETH at the hardcoded $3000 rate = $1500.
    assert o.comp_min == 1500.0
    assert o.comp_currency == "USD"


@pytest.mark.asyncio
async def test_gitcoin_skips_stale():
    out = await gitcoin_extract(
        _ei(
            [
                {
                    "pk": 1,
                    "title": "Stale",
                    "value_in_token": 100,
                    "token_name": "USDC",
                    "web3_created": "2024-01-01T00:00:00Z",
                }
            ],
            slug="bounty_gitcoin",
            url="https://gitcoin.co",
        )
    )
    assert out.opps == []


@pytest.mark.asyncio
async def test_gitcoin_handles_usdc_pass_through():
    out = await gitcoin_extract(
        _ei(
            [
                {
                    "pk": 2,
                    "title": "USDC bounty",
                    "value_in_token": 300,
                    "token_name": "USDC",
                    "web3_created": "2026-05-18T10:00:00Z",
                }
            ],
            slug="bounty_gitcoin",
            url="https://gitcoin.co",
        )
    )
    assert len(out.opps) == 1
    assert out.opps[0].comp_min == 300.0


# ---------- Replit -----------------------------------------------------------


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_replit_parses_bounty_card_html():
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


@pytest.mark.asyncio
async def test_replit_handles_card_with_no_reward():
    html = """<html><body>
      <article class="bounty-card">
        <h3>Reward TBD</h3>
        <a href="/bounties/xyz">View</a>
      </article>
    </body></html>"""
    out = await replit_extract(
        _ei(html, slug="bounty_replit", url="https://replit.com/bounties"),
    )
    assert len(out.opps) == 1
    assert out.opps[0].comp_min is None


# ---------- cross-platform fingerprint stability -----------------------------


@pytest.mark.asyncio
async def test_algora_fingerprint_stable_across_runs():
    payload = {
        "bounties": [
            {
                "id": 42,
                "title": "Fix race",
                "amount": 250,
                "createdAt": "2026-05-15T10:00:00Z",
                "org": {"handle": "myorg"},
            }
        ]
    }
    a = await algora_extract(_ei(payload, slug="bounty_algora", url="https://x"))
    b = await algora_extract(_ei(payload, slug="bounty_algora", url="https://x"))
    assert a.opps[0].fingerprint_hash == b.opps[0].fingerprint_hash

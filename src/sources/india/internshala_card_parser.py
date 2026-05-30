"""Internshala listing card → Opportunity. Single source of truth.

Both the browser discovery worker (`internshala_discovery_worker`) and the
legacy tier-1 selector extractor (`extractors.tier1_selectors.internshala`)
funnel each `div.individual_internship` card through `parse_card` so card
parsing lives in exactly one place. Selector knowledge is supplied by the
caller (from `config/sources/internshala_selectors.yaml` in the worker, or
from `DEFAULT_CARD_SELECTORS` here when no YAML is on hand), never hardcoded
inside the parse logic.

Stipend strings are normalised by `src.common.stipend_parser.parse_stipend`,
the corpus-tested converter shared with the worker. This module populates the
Opportunity's *native* comp fields (the numbers as they appear on the card);
INR normalisation happens downstream in the ranker.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from selectolax.parser import HTMLParser

from src.common.internshala_posted_parser import parse_apply_by, parse_posted_relative
from src.common.stipend_parser import parse_stipend
from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType

_BASE_URL = "https://internshala.com"

# Real selector values lifted from the legacy tier-1 module. Callers without
# the YAML pass this dict straight through; the worker overrides it with the
# SIGHUP-reloadable `internshala_selectors.yaml` values. `parse_card` reads
# every selector from the passed dict and only falls back here per-missing-key.
DEFAULT_CARD_SELECTORS: dict[str, str] = {
    "card_title": ".heading_4_5.profile, .job-internship-name",
    "card_company": ".company_and_premium .company-name, p.company a",
    "card_location": ".locations span a, .location_link",
    "card_stipend": ".stipend, .stipend_container_table_cell",
    "card_apply_link": "a.view_detail_button",
    "card_apply_by": ".apply_by .item_body, .other_detail_item.apply_by .item_body",
    "card_posted_relative": ".posted-by, .status-success",
}


def _fp(*parts: str) -> str:
    """Stable fingerprint over a card's identifying fields. Mirrors the legacy
    `_fp` helper: sha1 of the lowercased parts joined by `|`."""
    return hashlib.sha1("|".join(p.lower() for p in parts).encode()).hexdigest()  # noqa: S324


def _sel(selectors: dict[str, str], key: str) -> str:
    """Selector lookup with per-key fallback to the module default."""
    return selectors.get(key) or DEFAULT_CARD_SELECTORS[key]


def _text(card: HTMLParser, selector: str) -> str | None:
    node = card.css_first(selector)
    return node.text(strip=True) if node else None


def parse_card(
    card_html: str,
    *,
    source_id: int,
    selectors: dict[str, str],
    now: datetime | None = None,
) -> Opportunity | None:
    """Parse one Internshala listing card into an Opportunity.

    `card_html` is the outerHTML of a single `div.individual_internship`
    element. `selectors` carries CSS selectors under the keys `card_title`,
    `card_company`, `card_location`, `card_stipend`, `card_apply_link`,
    `card_apply_by`, `card_posted_relative`; any missing key falls back to
    `DEFAULT_CARD_SELECTORS`.

    `now` (default `datetime.now(UTC)`) anchors the relative-date parsing so
    the caller can pass one clock shared with the validity gate. The card's
    "Apply By" deadline populates `expires_at` and the "Posted X ago" text
    populates `posted_at`; both stay None when the card omits or garbles them
    (fail-open — the gate keeps such cards).

    Returns None when the title is empty or the stipend is unparseable — the
    caller drops the card in both cases.
    """
    now = now or datetime.now(UTC)
    tree = HTMLParser(card_html)
    # The fixture / outerHTML wraps a single card; reach into it when present
    # so selectors that assume the card root resolve, but degrade to the whole
    # tree if the wrapper element is absent.
    card = tree.css_first("div.individual_internship") or tree.body or tree

    title = _text(card, _sel(selectors, "card_title"))
    if not title:
        return None

    stipend_text = _text(card, _sel(selectors, "card_stipend")) or ""
    parsed = parse_stipend(stipend_text)
    if parsed is None:
        return None

    company = _text(card, _sel(selectors, "card_company"))
    location = _text(card, _sel(selectors, "card_location"))

    # Internshala carries the canonical listing link on the card root's
    # `data-href` attribute (verified live 2026-05-30); the inner
    # `a.view_detail_button` the parser read before is ABSENT on the real
    # listing page, so reading it alone produced a bare base-URL link. Prefer
    # data-href, fall back to the configured anchor / title anchor / any <a>.
    href = (card.attributes.get("data-href") or "").strip()
    if not href:
        link_node = (
            card.css_first(_sel(selectors, "card_apply_link"))
            or card.css_first("a.job-title-href")
            or card.css_first("a")
        )
        href = (link_node.attributes.get("href", "") if link_node else "") or ""
    absolute = href if href.startswith("http") else f"{_BASE_URL}{href}"

    is_remote = "work from home" in (card.text() or "").lower()

    expires_at = parse_apply_by(_text(card, _sel(selectors, "card_apply_by")), now=now)
    posted_at = parse_posted_relative(_text(card, _sel(selectors, "card_posted_relative")), now=now)

    return Opportunity(
        source_id=source_id,
        canonical_url=absolute or _BASE_URL,
        title=title,
        company=company,
        description=stipend_text[:600] or None,
        comp_min=parsed.comp_min_native,
        comp_max=parsed.comp_max_native,
        comp_currency=parsed.native_currency,
        comp_period=parsed.native_period,
        location=location,
        remote_type=RemoteType.REMOTE if is_remote else RemoteType.ONSITE,
        category=OppCategory.INTERNSHIP,
        posted_at=posted_at,
        expires_at=expires_at,
        apply_url=absolute or _BASE_URL,
        apply_method=ApplyMethod.IN_PLATFORM,
        fingerprint_hash=_fp(company or "", title, location or ""),
        extraction_tier=1,
        extraction_confidence=0.78,
    )


__all__ = ["DEFAULT_CARD_SELECTORS", "parse_card"]

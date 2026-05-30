"""Internshala JOBS listing card → Opportunity (full-time roles).

Sibling of `internshala_card_parser` (internships). Kept as a separate module so
the internship parser — the single source of truth shared with the tier-1
extractor — stays untouched. The jobs parser differs in exactly three things:

  * emits ``category=OppCategory.FULLTIME`` (internships emit INTERNSHIP),
  * populates ``years_experience_min`` from the card's experience cell
    (`src.common.experience_parser`), which the jobs discovery worker filters on,
  * reads a jobs-shaped selector set (`JOBS_CARD_SELECTORS`, adds `card_experience`).

Everything else — salary via `parse_stipend`, deadline/posted via the shared
date parsers, the fingerprint, the URL absolutisation — is the same logic. The
three trivial helpers (`_text`/`_sel`/`_fp`) are copied rather than imported to
keep this module independent of the internship parser's internals.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from selectolax.parser import HTMLParser

from src.common.experience_parser import parse_experience_years_min
from src.common.internshala_posted_parser import parse_apply_by, parse_posted_relative
from src.common.stipend_parser import parse_stipend
from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType

_BASE_URL = "https://internshala.com"

# Jobs-DOM selectors. Recon'd from the live Internshala jobs listing
# (2026-05-31, anonymous fetch): each card is a `div.individual_internship`
# whose `detail-row-1` holds three icon-keyed cells —
#   i.ic-16-money     -> salary  (<span class="mobile"> carries the "/year" unit)
#   i.ic-16-briefcase -> experience ("No experience required" / "1 year(s)")
#   i.ic-16-home      -> location
# and whose listing URL lives in the root's `data-href` attribute (NOT an <a>).
# The legacy `.salary` / `.experience` / `a.view_detail_button` fallbacks are
# kept for the synthetic test fixtures + any DOM drift. Re-verify logged-in on
# the ThinkPad (apply-by / posted cells weren't visible in the anon view; those
# stay fail-open None until confirmed).
JOBS_CARD_SELECTORS: dict[str, str] = {
    "card_title": ".job-internship-name, .heading_4_5.profile, .profile",
    "card_company": ".company-name, .company_and_premium .company-name, p.company a",
    "card_location": ".locations span a, .location_link",
    "card_stipend": "i.ic-16-money ~ span.mobile, i.ic-16-money ~ span.desktop, .salary, .stipend",
    "card_experience": "i.ic-16-briefcase ~ span, .experience, .job-experience",
    "card_apply_link": "a.view_detail_button, a.job-title-href",
    "card_apply_by": ".apply_by .item_body, .other_detail_item.apply_by .item_body",
    "card_posted_relative": ".posted-by, .status-success",
}


def _fp(*parts: str) -> str:
    """Stable fingerprint over a card's identifying fields (sha1 of lowercased
    parts joined by `|`) — same scheme as the internship parser."""
    return hashlib.sha1("|".join(p.lower() for p in parts).encode()).hexdigest()  # noqa: S324


def _sel(selectors: dict[str, str], key: str) -> str:
    """Selector lookup with per-key fallback to the module default."""
    return selectors.get(key) or JOBS_CARD_SELECTORS[key]


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
    """Parse one Internshala jobs listing card into a full-time Opportunity.

    Returns None when the title is empty or the salary is unparseable (a job with
    no parseable salary can't clear the salary floor anyway). `now` anchors the
    relative-date parsing; `years_experience_min` stays None when the card omits
    or garbles the experience cell (the worker's gate fails open on None).
    """
    now = now or datetime.now(UTC)
    tree = HTMLParser(card_html)
    card = tree.css_first("div.individual_internship") or tree.body or tree

    title = _text(card, _sel(selectors, "card_title"))
    if not title:
        return None

    salary_text = _text(card, _sel(selectors, "card_stipend")) or ""
    parsed = parse_stipend(salary_text)
    if parsed is None:
        return None

    company = _text(card, _sel(selectors, "card_company"))
    location = _text(card, _sel(selectors, "card_location"))

    # Jobs put the listing URL in the card root's `data-href` attribute, not an
    # <a>. Prefer it; fall back to an anchor (legacy fixtures / DOM drift).
    href = card.attributes.get("data-href") or ""
    if not href:
        link_node = card.css_first(_sel(selectors, "card_apply_link")) or card.css_first("a")
        href = (link_node.attributes.get("href", "") if link_node else "") or ""
    absolute = href if href.startswith("http") else f"{_BASE_URL}{href}"

    is_remote = "work from home" in (card.text() or "").lower()

    expires_at = parse_apply_by(_text(card, _sel(selectors, "card_apply_by")), now=now)
    posted_at = parse_posted_relative(_text(card, _sel(selectors, "card_posted_relative")), now=now)
    years_experience_min = parse_experience_years_min(_text(card, _sel(selectors, "card_experience")))

    return Opportunity(
        source_id=source_id,
        canonical_url=absolute or _BASE_URL,
        title=title,
        company=company,
        description=salary_text[:600] or None,
        comp_min=parsed.comp_min_native,
        comp_max=parsed.comp_max_native,
        comp_currency=parsed.native_currency,
        comp_period=parsed.native_period,
        location=location,
        remote_type=RemoteType.REMOTE if is_remote else RemoteType.ONSITE,
        category=OppCategory.FULLTIME,
        posted_at=posted_at,
        expires_at=expires_at,
        years_experience_min=years_experience_min,
        apply_url=absolute or _BASE_URL,
        apply_method=ApplyMethod.IN_PLATFORM,
        fingerprint_hash=_fp(company or "", title, location or ""),
        extraction_tier=1,
        extraction_confidence=0.78,
    )


__all__ = ["JOBS_CARD_SELECTORS", "parse_card"]

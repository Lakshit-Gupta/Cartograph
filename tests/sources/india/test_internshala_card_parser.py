"""Tests for the shared Internshala card parser and the tier-1 backward-compat
refactor that now delegates to it.

The fixture-driven cases monkeypatch ``parse_stipend`` with a small deterministic
stub so the native-comp assertions stay exact and independent of the corpus-tested
parser built concurrently (Agent B). A separate test confirms the real
``src.common.stipend_parser.parse_stipend`` is the one actually wired in.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import src.sources.india.internshala_card_parser as card_parser
from src.common.stipend_parser import ParsedStipend
from src.common.types import ApplyMethod, OppCategory, RemoteType
from src.extractors.base import ExtractInput
from src.extractors.tier1_selectors.internshala import extract as tier1_extract
from src.sources.india.internshala_card_parser import DEFAULT_CARD_SELECTORS, parse_card

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "internshala"


def _load(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _stub_parsed(
    raw: str,
    *,
    comp_min: float,
    comp_max: float,
    currency: str = "INR",
    period: str = "month",
) -> ParsedStipend:
    return ParsedStipend(
        comp_min_inr_per_month=comp_min,
        comp_max_inr_per_month=comp_max,
        comp_min_native=comp_min,
        comp_max_native=comp_max,
        native_currency=currency,
        native_period=period,
        raw=raw,
    )


@pytest.fixture
def stub_stipend(monkeypatch: pytest.MonkeyPatch):
    """Deterministic stipend stub keyed on a handful of fixture strings.

    Returns native numbers verbatim so the card-parser assertions do not depend
    on currency/period INR math owned by another module. Unknown strings (the
    "Unpaid" edge) resolve to ``None`` to exercise the drop path.
    """

    table: dict[str, ParsedStipend] = {
        "₹30,000 /month": _stub_parsed("₹30,000 /month", comp_min=30000, comp_max=30000),
        "₹32,000 /month": _stub_parsed("₹32,000 /month", comp_min=32000, comp_max=32000),
        "₹33,000 /month": _stub_parsed("₹33,000 /month", comp_min=33000, comp_max=33000),
        "₹35,000 /month": _stub_parsed("₹35,000 /month", comp_min=35000, comp_max=35000),
        "₹38,000 /month": _stub_parsed("₹38,000 /month", comp_min=38000, comp_max=38000),
        "₹40,000 - 60,000 /month": _stub_parsed("₹40,000 - 60,000 /month", comp_min=40000, comp_max=60000),
        "₹30,000 - 45,000 /month": _stub_parsed("₹30,000 - 45,000 /month", comp_min=30000, comp_max=45000),
        "₹45,000 /month": _stub_parsed("₹45,000 /month", comp_min=45000, comp_max=45000),
        "₹50,000 /month": _stub_parsed("₹50,000 /month", comp_min=50000, comp_max=50000),
        "₹55,000 /month": _stub_parsed("₹55,000 /month", comp_min=55000, comp_max=55000),
        "₹40k /month": _stub_parsed("₹40k /month", comp_min=40000, comp_max=40000),
    }

    def _fake_parse(raw: str) -> ParsedStipend | None:
        return table.get(raw.strip())

    monkeypatch.setattr(card_parser, "parse_stipend", _fake_parse)
    return _fake_parse


# --------------------------------------------------------------------------- #
# Fixture-driven happy paths
# --------------------------------------------------------------------------- #


def test_remote_single_stipend(stub_stipend):
    opp = parse_card(_load("listing_card_01_remote_single.html"), source_id=1, selectors=DEFAULT_CARD_SELECTORS)
    assert opp is not None
    assert opp.title == "Backend Development"
    assert opp.company == "Acme Labs"
    assert opp.comp_min == 30000
    assert opp.comp_max == 30000
    assert opp.comp_currency == "INR"
    assert opp.comp_period == "month"
    assert opp.remote_type == RemoteType.REMOTE
    assert opp.category == OppCategory.INTERNSHIP
    assert opp.apply_method == ApplyMethod.IN_PLATFORM
    assert opp.canonical_url == "https://internshala.com/internship/detail/backend-development-acme-labs-1500001"
    assert opp.apply_url == opp.canonical_url
    assert opp.extraction_tier == 1
    assert opp.extraction_confidence == 0.78


def test_remote_range_stipend(stub_stipend):
    opp = parse_card(_load("listing_card_02_remote_range.html"), source_id=7, selectors=DEFAULT_CARD_SELECTORS)
    assert opp is not None
    assert opp.title == "Machine Learning"
    assert opp.company == "Nimbus AI"
    assert opp.comp_min == 40000
    assert opp.comp_max == 60000
    assert opp.remote_type == RemoteType.REMOTE
    assert opp.source_id == 7


def test_onsite_single_stipend(stub_stipend):
    opp = parse_card(_load("listing_card_03_onsite_single.html"), source_id=1, selectors=DEFAULT_CARD_SELECTORS)
    assert opp is not None
    assert opp.title == "Full Stack Development"
    assert opp.company == "Bharat Systems"
    assert opp.location == "Bangalore"
    assert opp.remote_type == RemoteType.ONSITE
    assert opp.comp_min == 35000


def test_onsite_range_stipend(stub_stipend):
    opp = parse_card(_load("listing_card_04_onsite_range.html"), source_id=1, selectors=DEFAULT_CARD_SELECTORS)
    assert opp is not None
    assert opp.title == "Data Science"
    assert opp.location == "Hyderabad"
    assert opp.remote_type == RemoteType.ONSITE
    assert opp.comp_min == 30000
    assert opp.comp_max == 45000


def test_apply_by_suffix_does_not_corrupt_fields(stub_stipend):
    opp = parse_card(_load("listing_card_05_apply_by_suffix.html"), source_id=1, selectors=DEFAULT_CARD_SELECTORS)
    assert opp is not None
    assert opp.title == "Python/Django Development"
    assert opp.company == "Tindrel Software"
    # "Apply By" date text lives outside the stipend node — comp stays clean.
    assert opp.comp_min == 32000
    assert opp.comp_period == "month"


def test_apply_by_populates_expires_at(stub_stipend):
    """The card's "Apply By 30 Jun' 26" is parsed into an inclusive end-of-day
    expires_at (the validity gate keys off this)."""
    opp = parse_card(
        _load("listing_card_05_apply_by_suffix.html"),
        source_id=1,
        selectors=DEFAULT_CARD_SELECTORS,
        now=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert opp is not None
    assert opp.expires_at == datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)


def test_missing_apply_by_leaves_dates_none(stub_stipend):
    """A card with no apply-by / posted node leaves both date fields None
    (fail-open: the gate keeps it)."""
    opp = parse_card(
        _load("listing_card_01_remote_single.html"),
        source_id=1,
        selectors=DEFAULT_CARD_SELECTORS,
        now=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert opp is not None
    assert opp.expires_at is None
    assert opp.posted_at is None


def test_past_deadline_card_still_parses(stub_stipend):
    """A past-deadline card parses successfully with a past expires_at — the
    parser never drops it; the validity gate does."""
    now = datetime(2026, 5, 31, tzinfo=UTC)
    opp = parse_card(
        _load("listing_card_13_past_deadline.html"),
        source_id=1,
        selectors=DEFAULT_CARD_SELECTORS,
        now=now,
    )
    assert opp is not None
    assert opp.expires_at is not None
    assert opp.expires_at < now


def test_posted_relative_populates_posted_at(stub_stipend):
    """A "Posted 4 days ago" node becomes an absolute posted_at."""
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)
    opp = parse_card(
        _load("listing_card_14_posted_relative.html"),
        source_id=1,
        selectors=DEFAULT_CARD_SELECTORS,
        now=now,
    )
    assert opp is not None
    assert opp.posted_at == datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)


def test_multi_skill_card(stub_stipend):
    opp = parse_card(_load("listing_card_06_multi_skill.html"), source_id=1, selectors=DEFAULT_CARD_SELECTORS)
    assert opp is not None
    assert opp.title == "Data Engineering"
    assert opp.company == "Pelagic Data"
    assert opp.comp_min == 50000
    assert opp.remote_type == RemoteType.REMOTE


def test_missing_company_yields_none_company(stub_stipend):
    opp = parse_card(_load("listing_card_07_missing_company.html"), source_id=1, selectors=DEFAULT_CARD_SELECTORS)
    assert opp is not None
    assert opp.title == "Artificial Intelligence (AI)"
    assert opp.company is None
    assert opp.comp_min == 38000
    # Fingerprint stays stable with an empty company part.
    assert len(opp.fingerprint_hash) == 40


def test_alternate_selector_shapes(stub_stipend):
    opp = parse_card(_load("listing_card_10_alt_selectors.html"), source_id=1, selectors=DEFAULT_CARD_SELECTORS)
    assert opp is not None
    assert opp.title == "DevOps"  # .job-internship-name branch
    assert opp.company == "Skylark Cloud"  # p.company a branch
    assert opp.comp_min == 45000  # .stipend_container_table_cell branch
    assert opp.remote_type == RemoteType.REMOTE


def test_absolute_href_passthrough(stub_stipend):
    opp = parse_card(_load("listing_card_11_absolute_href.html"), source_id=1, selectors=DEFAULT_CARD_SELECTORS)
    assert opp is not None
    # Already absolute — must not get double-prefixed.
    assert opp.canonical_url == "https://internshala.com/internship/detail/nlp-lexicon-1500011"


def test_k_suffix_onsite(stub_stipend):
    opp = parse_card(_load("listing_card_12_k_suffix_onsite.html"), source_id=1, selectors=DEFAULT_CARD_SELECTORS)
    assert opp is not None
    assert opp.title == "API Development"
    assert opp.location == "Pune"
    assert opp.remote_type == RemoteType.ONSITE
    assert opp.comp_min == 40000


# --------------------------------------------------------------------------- #
# None paths
# --------------------------------------------------------------------------- #


def test_missing_title_returns_none(stub_stipend):
    opp = parse_card(_load("listing_card_08_no_title.html"), source_id=1, selectors=DEFAULT_CARD_SELECTORS)
    assert opp is None


def test_unparseable_stipend_returns_none(stub_stipend):
    # "Unpaid" is absent from the stub table → parse_stipend returns None → drop.
    opp = parse_card(_load("listing_card_09_unpaid.html"), source_id=1, selectors=DEFAULT_CARD_SELECTORS)
    assert opp is None


def test_empty_card_returns_none(stub_stipend):
    opp = parse_card("<div class='individual_internship'></div>", source_id=1, selectors=DEFAULT_CARD_SELECTORS)
    assert opp is None


# --------------------------------------------------------------------------- #
# Selector-injection contract
# --------------------------------------------------------------------------- #


def test_selectors_read_from_passed_dict(stub_stipend):
    """A caller-supplied selector overrides the default; missing keys fall back."""
    html = _load("listing_card_01_remote_single.html")
    # Point card_title at a node that does not exist → title empty → None.
    bogus = dict(DEFAULT_CARD_SELECTORS)
    bogus["card_title"] = ".this_class_is_not_in_the_card"
    assert parse_card(html, source_id=1, selectors=bogus) is None


def test_missing_selector_key_falls_back_to_default(stub_stipend):
    """Passing a partial dict still works — absent keys use DEFAULT_CARD_SELECTORS."""
    html = _load("listing_card_01_remote_single.html")
    opp = parse_card(html, source_id=1, selectors={"card_title": ".heading_4_5.profile"})
    assert opp is not None
    assert opp.title == "Backend Development"
    assert opp.company == "Acme Labs"  # came from the fallback default


# --------------------------------------------------------------------------- #
# Real-parser integration (no stub) — confirms wiring to Agent B's module
# --------------------------------------------------------------------------- #


def test_real_parse_stipend_is_wired_in():
    """Without monkeypatching, the real corpus parser populates native comp."""
    opp = parse_card(_load("listing_card_01_remote_single.html"), source_id=1, selectors=DEFAULT_CARD_SELECTORS)
    assert opp is not None
    assert opp.comp_min == 30000
    assert opp.comp_currency == "INR"
    assert opp.comp_period == "month"
    # The real parser drops "Unpaid".
    assert parse_card(_load("listing_card_09_unpaid.html"), source_id=1, selectors=DEFAULT_CARD_SELECTORS) is None


# --------------------------------------------------------------------------- #
# Tier-1 backward compatibility
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_tier1_compat_full_listing(stub_stipend):
    """The refactored tier-1 extractor splits the page and delegates to
    parse_card, producing the same Opportunity set as a direct per-card parse."""
    page = _load("full_listing_page.html")
    out = await tier1_extract(
        ExtractInput(
            source_id=1,
            source_slug="india_internshala",
            url="https://internshala.com/internships/",
            content=page,
            content_type="text/html",
        )
    )
    assert out.tier_used == 1
    assert out.confidence == 0.78
    assert len(out.opps) == 3

    titles = [o.title for o in out.opps]
    assert titles == ["Backend Development", "Machine Learning", "Data Engineering"]

    by_title = {o.title: o for o in out.opps}
    assert by_title["Backend Development"].company == "Acme Labs"
    assert by_title["Backend Development"].comp_min == 30000
    assert by_title["Backend Development"].remote_type == RemoteType.REMOTE
    assert by_title["Backend Development"].canonical_url.endswith("backend-development-acme-labs-1600001")
    assert by_title["Machine Learning"].comp_min == 40000
    assert by_title["Machine Learning"].comp_max == 60000
    assert by_title["Machine Learning"].remote_type == RemoteType.ONSITE
    assert by_title["Machine Learning"].location == "Bangalore"

    for opp in out.opps:
        assert opp.category == OppCategory.INTERNSHIP
        assert opp.apply_method == ApplyMethod.IN_PLATFORM
        assert opp.extraction_tier == 1
        assert opp.comp_currency == "INR"


@pytest.mark.asyncio
async def test_tier1_empty_content_returns_no_opps():
    out = await tier1_extract(
        ExtractInput(
            source_id=1,
            source_slug="india_internshala",
            url="https://internshala.com/internships/",
            content="<html><body><p>no listings</p></body></html>",
            content_type="text/html",
        )
    )
    assert out.opps == []
    assert out.confidence == 0.0


@pytest.mark.asyncio
async def test_tier1_delegates_to_shared_parser(monkeypatch: pytest.MonkeyPatch):
    """Guard the single-source-of-truth invariant: tier-1 must call parse_card,
    not re-implement card parsing inline."""
    calls: list[int] = []
    real_parse = card_parser.parse_card

    def _spy(card_html: str, *, source_id: int, selectors: dict[str, str]):
        calls.append(source_id)
        return real_parse(card_html, source_id=source_id, selectors=selectors)

    # tier-1 imports parse_card by name into its own module namespace.
    import src.extractors.tier1_selectors.internshala as tier1_mod

    monkeypatch.setattr(tier1_mod, "parse_card", _spy)

    await tier1_extract(
        ExtractInput(
            source_id=99,
            source_slug="india_internshala",
            url="https://internshala.com/internships/",
            content=_load("full_listing_page.html"),
            content_type="text/html",
        )
    )
    assert calls == [99, 99, 99]

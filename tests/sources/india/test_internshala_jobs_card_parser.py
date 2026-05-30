"""Tests for the Internshala JOBS card parser.

Mirrors the internship card-parser test: a deterministic `parse_stipend` stub
keeps the comp assertions exact and independent of the corpus-tested salary
parser. The jobs parser differs from the internship one in three ways — it emits
`category=FULLTIME`, populates `years_experience_min` from the card's experience
cell, and reads a jobs-shaped selector set.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import src.sources.india.internshala_jobs_card_parser as jobs_parser
from src.common.stipend_parser import ParsedStipend
from src.common.types import ApplyMethod, OppCategory, RemoteType
from src.sources.india.internshala_jobs_card_parser import JOBS_CARD_SELECTORS, parse_card

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "internshala_jobs"


def _load(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _stub(raw: str, *, comp_min: float, comp_max: float, currency: str = "INR", period: str = "year") -> ParsedStipend:
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
    table: dict[str, ParsedStipend] = {
        "₹15,00,000 /year": _stub("₹15,00,000 /year", comp_min=1500000, comp_max=1500000),
        "₹12,00,000 - 18,00,000 /year": _stub("₹12,00,000 - 18,00,000 /year", comp_min=1200000, comp_max=1800000),
        "₹14,00,000 /year": _stub("₹14,00,000 /year", comp_min=1400000, comp_max=1400000),
        "₹13,00,000 /year": _stub("₹13,00,000 /year", comp_min=1300000, comp_max=1300000),
        "$90,000 /year": _stub("$90,000 /year", comp_min=90000, comp_max=90000, currency="USD"),
        "₹16,00,000 /year": _stub("₹16,00,000 /year", comp_min=1600000, comp_max=1600000),
    }

    def _fake_parse(raw: str) -> ParsedStipend | None:
        return table.get(raw.strip())

    monkeypatch.setattr(jobs_parser, "parse_stipend", _fake_parse)
    return _fake_parse


def test_fresher_card_is_fulltime_zero_experience(stub_stipend):
    opp = parse_card(_load("jobs_card_01_fresher.html"), source_id=5, selectors=JOBS_CARD_SELECTORS)
    assert opp is not None
    assert opp.title == "Backend Engineer"
    assert opp.company == "Nimbus Tech"
    assert opp.category == OppCategory.FULLTIME
    assert opp.years_experience_min == 0
    assert opp.comp_min == 1500000
    assert opp.comp_period == "year"
    assert opp.remote_type == RemoteType.REMOTE
    assert opp.apply_method == ApplyMethod.IN_PLATFORM
    assert opp.source_id == 5


def test_range_salary_and_experience(stub_stipend):
    opp = parse_card(_load("jobs_card_02_range_salary.html"), source_id=1, selectors=JOBS_CARD_SELECTORS)
    assert opp is not None
    assert opp.years_experience_min == 1
    assert opp.comp_min == 1200000
    assert opp.comp_max == 1800000


def test_experience_two_to_four_years(stub_stipend):
    opp = parse_card(_load("jobs_card_03_exp_2_4.html"), source_id=1, selectors=JOBS_CARD_SELECTORS)
    assert opp is not None
    assert opp.years_experience_min == 2


def test_missing_experience_is_none_fail_open(stub_stipend):
    opp = parse_card(_load("jobs_card_04_missing_experience.html"), source_id=1, selectors=JOBS_CARD_SELECTORS)
    assert opp is not None
    assert opp.years_experience_min is None


def test_usd_salary_currency_preserved(stub_stipend):
    opp = parse_card(_load("jobs_card_05_usd.html"), source_id=1, selectors=JOBS_CARD_SELECTORS)
    assert opp is not None
    assert opp.comp_currency == "USD"
    assert opp.years_experience_min == 1


def test_no_title_returns_none(stub_stipend):
    opp = parse_card(_load("jobs_card_06_no_title.html"), source_id=1, selectors=JOBS_CARD_SELECTORS)
    assert opp is None


def test_real_parse_stipend_is_wired_in():
    """Without the stub, the real corpus salary parser populates native comp."""
    opp = parse_card(_load("jobs_card_02_range_salary.html"), source_id=1, selectors=JOBS_CARD_SELECTORS)
    assert opp is not None
    assert opp.comp_min == 1200000
    assert opp.comp_max == 1800000
    assert opp.comp_currency == "INR"
    assert opp.comp_period == "year"
    assert opp.category == OppCategory.FULLTIME

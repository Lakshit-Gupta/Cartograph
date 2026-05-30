"""Regression corpus + edge-case unit tests for the stipend parser.

The corpus (`tests/fixtures/stipend_strings.json`, ≥100 entries) is the
contract: any change to `src.common.stipend_parser.parse_stipend` must keep
every entry green. Numeric entries assert the INR-per-month bounds, currency,
and period; garbage entries assert ``None``.

A small float tolerance is allowed on the INR amounts because period→month
conversion (year ÷ 12, etc.) introduces non-terminating decimals.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.common.stipend_parser import ParsedStipend, parse_stipend

# abs tolerance on INR amounts — year/12 etc. yield repeating decimals.
_INR_TOLERANCE = 1.0

_FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "stipend_strings.json"


def _load_corpus() -> list[dict[str, Any]]:
    with _FIXTURE_PATH.open(encoding="utf-8") as fh:
        data = json.load(fh)
    assert isinstance(data, list), "stipend corpus must be a JSON array"
    return data


_CORPUS: list[dict[str, Any]] = _load_corpus()


def _case_id(entry: dict[str, Any]) -> str:
    """Readable -k filter id from the raw string."""
    return repr(entry["raw"])


@pytest.mark.smoke
def test_corpus_is_large_enough() -> None:
    """The design pins ≥100 real strings — guard against accidental shrinkage."""
    assert len(_CORPUS) >= 100, f"stipend corpus has only {len(_CORPUS)} entries; design requires ≥100"


@pytest.mark.smoke
@pytest.mark.parametrize("entry", _CORPUS, ids=_case_id)
def test_corpus_entry(entry: dict[str, Any]) -> None:
    """Every corpus entry parses to its declared expectation."""
    result = parse_stipend(entry["raw"])

    if entry.get("expected", "MISSING") is None:
        # Garbage / non-numeric / zero-stipend → must be dropped.
        assert result is None, f"expected None for {entry['raw']!r}, got {result!r}"
        return

    assert result is not None, f"expected a ParsedStipend for {entry['raw']!r}, got None"
    assert isinstance(result, ParsedStipend)

    assert result.native_currency == entry["expected_currency"], (
        f"currency mismatch for {entry['raw']!r}: {result.native_currency!r} != {entry['expected_currency']!r}"
    )
    assert result.native_period == entry["expected_period"], (
        f"period mismatch for {entry['raw']!r}: {result.native_period!r} != {entry['expected_period']!r}"
    )

    assert result.comp_min_inr_per_month is not None
    assert result.comp_max_inr_per_month is not None
    assert abs(result.comp_min_inr_per_month - entry["expected_min_inr_per_month"]) < _INR_TOLERANCE, (
        f"min INR mismatch for {entry['raw']!r}: {result.comp_min_inr_per_month} != {entry['expected_min_inr_per_month']}"
    )
    assert abs(result.comp_max_inr_per_month - entry["expected_max_inr_per_month"]) < _INR_TOLERANCE, (
        f"max INR mismatch for {entry['raw']!r}: {result.comp_max_inr_per_month} != {entry['expected_max_inr_per_month']}"
    )

    # min must never exceed max.
    assert result.comp_min_inr_per_month <= result.comp_max_inr_per_month


# --- explicit edge-case unit tests (beyond the corpus) ----------------------


@pytest.mark.smoke
def test_range_ordering_lo_le_hi() -> None:
    """A range yields min ≤ max on both native and INR axes."""
    result = parse_stipend("₹10,000-15,000 /month")
    assert result is not None
    assert result.comp_min_native == 10000.0
    assert result.comp_max_native == 15000.0
    assert result.comp_min_inr_per_month == pytest.approx(10000.0)
    assert result.comp_max_inr_per_month == pytest.approx(15000.0)


@pytest.mark.smoke
def test_range_reversed_input_still_orders() -> None:
    """Even if the larger number is written first, min/max stay ordered."""
    result = parse_stipend("₹15,000 - 10,000 /month")
    assert result is not None
    assert result.comp_min_native == 10000.0
    assert result.comp_max_native == 15000.0


@pytest.mark.smoke
def test_en_dash_range() -> None:
    """An en-dash separator splits a range just like an ASCII hyphen."""
    result = parse_stipend("₹20,000 – 30,000 /month")
    assert result is not None
    assert result.comp_min_native == 20000.0
    assert result.comp_max_native == 30000.0


@pytest.mark.smoke
def test_lakh_forces_year_period() -> None:
    """LPA / lakh imply a yearly figure; INR/month = lakh/12."""
    result = parse_stipend("2.5 LPA")
    assert result is not None
    assert result.native_period == "year"
    assert result.comp_min_native == 250000.0
    # 250000 / 12 ≈ 20833.33
    assert result.comp_min_inr_per_month == pytest.approx(250000.0 / 12.0, abs=_INR_TOLERANCE)


@pytest.mark.smoke
def test_lakh_l_suffix_no_space() -> None:
    """₹2.5L parses identically to 2.5 lakh."""
    result = parse_stipend("₹2.5L")
    assert result is not None
    assert result.native_period == "year"
    assert result.comp_min_native == 250000.0


@pytest.mark.smoke
def test_crore_forces_year_and_scale() -> None:
    """1Cr = 1e7 INR/year."""
    result = parse_stipend("1Cr")
    assert result is not None
    assert result.native_period == "year"
    assert result.comp_min_native == 1e7
    assert result.comp_min_inr_per_month == pytest.approx(1e7 / 12.0, abs=_INR_TOLERANCE)


@pytest.mark.smoke
def test_crore_not_misread_as_lakh() -> None:
    """The 'Cr' token must scale by 1e7, never trip the single-letter L path."""
    result = parse_stipend("₹1.2Cr")
    assert result is not None
    assert result.comp_min_native == 12000000.0


@pytest.mark.smoke
def test_k_suffix_default_month() -> None:
    """A bare k-suffix value defaults to monthly INR."""
    result = parse_stipend("15k")
    assert result is not None
    assert result.native_period == "month"
    assert result.comp_min_native == 15000.0
    assert result.comp_min_inr_per_month == pytest.approx(15000.0)


@pytest.mark.smoke
def test_k_suffix_range() -> None:
    """k-suffix on both ends of a range."""
    result = parse_stipend("12k-18k /month")
    assert result is not None
    assert result.comp_min_native == 12000.0
    assert result.comp_max_native == 18000.0


@pytest.mark.smoke
def test_usd_converts_at_83() -> None:
    """USD monthly stipend converts at the currency.py snapshot rate (83x)."""
    result = parse_stipend("$2000 /month")
    assert result is not None
    assert result.native_currency == "USD"
    assert result.native_period == "month"
    assert result.comp_min_inr_per_month == pytest.approx(2000.0 * 83.0, abs=_INR_TOLERANCE)


@pytest.mark.smoke
def test_per_hour_scales_by_160() -> None:
    """Rs 500/hr maps to 500 * 160 = Rs 80,000/month."""
    result = parse_stipend("₹500 /hour")
    assert result is not None
    assert result.native_period == "hour"
    assert result.comp_min_inr_per_month == pytest.approx(80000.0, abs=_INR_TOLERANCE)


@pytest.mark.smoke
def test_default_period_is_month_for_bare_inr() -> None:
    """A bare INR number with no period defaults to monthly."""
    result = parse_stipend("20000")
    assert result is not None
    assert result.native_currency == "INR"
    assert result.native_period == "month"


@pytest.mark.smoke
@pytest.mark.parametrize(
    "raw",
    ["Unpaid", "unpaid", "Negotiable", "Performance based", "Competitive", "", "   ", "Stipend: 0", "₹0 /month"],
)
def test_non_numeric_and_zero_return_none(raw: str) -> None:
    """The garbage set + declared-zero stipends must all parse to None."""
    assert parse_stipend(raw) is None


@pytest.mark.smoke
def test_raw_is_preserved_verbatim() -> None:
    """The untouched input string is echoed back on the dataclass."""
    raw = "  ₹22,000 /MONTH  "
    result = parse_stipend(raw)
    assert result is not None
    assert result.raw == raw

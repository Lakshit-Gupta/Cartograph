"""Unit tests for the Internshala jobs-card experience parser.

`parse_experience_years_min` turns the required-experience text on a jobs card
("Fresher", "0-2 years", "1-3 years", …) into the MINIMUM years required, which
the jobs validity gate compares against the user's `max_experience_years` cap.
Returns ``None`` on anything it can't parse (the gate then fails open — keeps the
card).
"""

from __future__ import annotations

import pytest

from src.common.experience_parser import parse_experience_years_min


@pytest.mark.smoke
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Fresher", 0),
        ("fresher", 0),
        ("Fresher / No experience", 0),
        ("0 years", 0),
        ("0-2 years", 0),
        ("1 year", 1),
        ("1-3 years", 1),
        ("2 years", 2),
        ("2-4 years", 2),
        ("5+ years", 5),
        ("Experience: 3-5 yrs", 3),
        ("10 years", 10),
    ],
)
def test_experience_min_table(raw: str, expected: int) -> None:
    assert parse_experience_years_min(raw) == expected


@pytest.mark.smoke
@pytest.mark.parametrize("raw", ["", "   ", None, "Competitive", "negotiable", "as per role", "experienced"])
def test_experience_garbage_returns_none(raw: str | None) -> None:
    assert parse_experience_years_min(raw) is None


@pytest.mark.smoke
def test_range_takes_the_minimum_not_maximum() -> None:
    """A "2-4 years" requirement means MIN 2 — the gate must drop it at cap=1."""
    assert parse_experience_years_min("2-4 years") == 2


@pytest.mark.smoke
def test_fresher_beats_a_trailing_number() -> None:
    """A "Fresher (0-1 years)" string resolves to 0 via the fresher short-circuit."""
    assert parse_experience_years_min("Fresher (0-1 years)") == 0

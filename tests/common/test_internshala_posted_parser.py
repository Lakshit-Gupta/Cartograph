"""Unit tests for the Internshala listing-card freshness parsers.

Two pure functions convert the date-ish text Internshala renders on each
listing card into absolute, timezone-aware datetimes:

  - `parse_apply_by`        — "Apply By 30 Jun' 26" → the application deadline.
  - `parse_posted_relative` — "Posted 3 days ago"    → an absolute posted_at.

Both take an explicit `now` so the relative parser is deterministic, and both
return ``None`` on anything they cannot confidently parse (the caller fails
open — a missing/garbage date never drops an otherwise valid card).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.common.internshala_posted_parser import parse_apply_by, parse_posted_relative

_NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# parse_apply_by — explicit "Apply By DD Mon' YY" deadline.
# --------------------------------------------------------------------------- #
@pytest.mark.smoke
def test_apply_by_two_digit_year() -> None:
    """The fixture format `Apply By 30 Jun' 26` → end of 30 Jun 2026 (inclusive)."""
    got = parse_apply_by("Apply By 30 Jun' 26", now=_NOW)
    assert got == datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)


@pytest.mark.smoke
def test_apply_by_four_digit_year() -> None:
    got = parse_apply_by("Apply By 1 Jan' 2027", now=_NOW)
    assert got == datetime(2027, 1, 1, 23, 59, 59, tzinfo=UTC)


@pytest.mark.smoke
def test_apply_by_without_prefix() -> None:
    """The bare date (no 'Apply By' lead-in) still parses."""
    got = parse_apply_by("5 Aug' 25", now=_NOW)
    assert got == datetime(2025, 8, 5, 23, 59, 59, tzinfo=UTC)


@pytest.mark.smoke
def test_apply_by_full_month_name() -> None:
    got = parse_apply_by("Apply By 12 December' 26", now=_NOW)
    assert got == datetime(2026, 12, 12, 23, 59, 59, tzinfo=UTC)


@pytest.mark.smoke
def test_apply_by_is_inclusive_on_the_day() -> None:
    """A deadline of today must still be in the future at midday — end-of-day."""
    today = parse_apply_by("Apply By 31 May' 26", now=_NOW)
    assert today is not None
    assert today > _NOW


@pytest.mark.smoke
@pytest.mark.parametrize(
    "raw",
    ["", "   ", None, "Apply soon", "Posted 3 days ago", "Apply By 32 Jun' 26", "Apply By 5 Foo' 26"],
)
def test_apply_by_garbage_returns_none(raw: str | None) -> None:
    assert parse_apply_by(raw, now=_NOW) is None


# --------------------------------------------------------------------------- #
# parse_posted_relative — "Posted X ago" → absolute posted_at.
# --------------------------------------------------------------------------- #
@pytest.mark.smoke
def test_posted_days_ago() -> None:
    assert parse_posted_relative("Posted 3 days ago", now=_NOW) == _NOW - timedelta(days=3)


@pytest.mark.smoke
def test_posted_single_day_ago() -> None:
    assert parse_posted_relative("Posted 1 day ago", now=_NOW) == _NOW - timedelta(days=1)


@pytest.mark.smoke
def test_posted_weeks_ago() -> None:
    assert parse_posted_relative("Posted 2 weeks ago", now=_NOW) == _NOW - timedelta(days=14)


@pytest.mark.smoke
def test_posted_months_ago_uses_30_day_month() -> None:
    assert parse_posted_relative("Posted 2 months ago", now=_NOW) == _NOW - timedelta(days=60)


@pytest.mark.smoke
@pytest.mark.parametrize("raw", ["today", "Posted today", "just now", "few hours ago", "an hour ago", "Posted 5 hours ago"])
def test_posted_today_variants_return_now(raw: str) -> None:
    """Anything posted within the day collapses to `now` (age 0)."""
    assert parse_posted_relative(raw, now=_NOW) == _NOW


@pytest.mark.smoke
def test_posted_yesterday() -> None:
    assert parse_posted_relative("yesterday", now=_NOW) == _NOW - timedelta(days=1)


@pytest.mark.smoke
@pytest.mark.parametrize("raw", ["", "   ", None, "Work From Home", "₹32,000 /month"])
def test_posted_garbage_returns_none(raw: str | None) -> None:
    assert parse_posted_relative(raw, now=_NOW) is None

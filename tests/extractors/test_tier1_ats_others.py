"""Regression tests for the Lever, Ashby, Workable, and Contra tier-1 extractors.

These tests cover the defensive payload-shape guards added in commit 4cf5af1
(extractor coerce against null nested objects). The real ATS APIs occasionally
serve `compensation: null`, `categories: null`, `budget: null` for jobs that
were posted without the corresponding optional fields populated. Before the
guard, `.get(...).get(...)` chained on the None and raised `AttributeError`,
killing the entire batch.
"""

from __future__ import annotations

import json

import pytest

from src.common.types import ApplyMethod, OppCategory, RemoteType
from src.extractors.base import ExtractInput
from src.extractors.tier1_selectors.ashby import extract as ashby_extract
from src.extractors.tier1_selectors.contra import extract as contra_extract
from src.extractors.tier1_selectors.lever import extract as lever_extract
from src.extractors.tier1_selectors.workable import extract as workable_extract


def _ei(payload: dict | list, *, source_id: int = 1, slug: str = "x", url: str = "https://example.com") -> ExtractInput:
    return ExtractInput(
        source_id=source_id,
        source_slug=slug,
        url=url,
        content=json.dumps(payload),
        content_type="application/json",
    )


# ---------- Lever ------------------------------------------------------------


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_lever_basic_extraction():
    out = await lever_extract(
        _ei(
            [
                {
                    "text": "Senior Backend Engineer",
                    "categories": {"team": "Platform", "department": "Engineering", "location": "Remote - US", "commitment": "Full-time"},
                    "hostedUrl": "https://jobs.lever.co/palantir/abc",
                    "createdAt": 1730000000000,
                    "descriptionPlain": "Build distributed systems",
                }
            ]
        )
    )
    assert len(out.opps) == 1
    assert out.opps[0].title == "Senior Backend Engineer"
    assert out.opps[0].category == OppCategory.FULLTIME
    assert out.opps[0].apply_method == ApplyMethod.ATS_FORM


@pytest.mark.asyncio
async def test_lever_handles_null_categories():
    out = await lever_extract(
        _ei(
            [
                {
                    "text": "Engineer",
                    "categories": None,
                    "hostedUrl": "https://jobs.lever.co/x/y",
                }
            ]
        )
    )
    assert len(out.opps) == 1
    assert out.opps[0].title == "Engineer"


@pytest.mark.asyncio
async def test_lever_intern_classification():
    out = await lever_extract(
        _ei(
            [
                {
                    "text": "Software Engineering Intern",
                    "categories": {"commitment": "Intern"},
                    "hostedUrl": "https://example.com/intern",
                }
            ]
        )
    )
    assert out.opps[0].category == OppCategory.INTERNSHIP


# ---------- Ashby ------------------------------------------------------------


@pytest.mark.asyncio
async def test_ashby_basic_extraction():
    out = await ashby_extract(
        _ei(
            {
                "jobs": [
                    {
                        "title": "ML Engineer",
                        "teamName": "Research",
                        "locationName": "San Francisco, CA",
                        "employmentType": "FullTime",
                        "jobUrl": "https://jobs.ashbyhq.com/openai/role",
                        "compensation": {
                            "compensationTierSummary": {
                                "minValue": 200000,
                                "maxValue": 250000,
                                "currencyCode": "USD",
                            }
                        },
                    }
                ],
            }
        )
    )
    assert len(out.opps) == 1
    o = out.opps[0]
    assert o.title == "ML Engineer"
    assert o.comp_min == 200000
    assert o.comp_max == 250000
    assert o.comp_currency == "USD"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_ashby_handles_null_compensation():
    """Repro of the AttributeError crash that bit the live extractor."""
    out = await ashby_extract(
        _ei(
            {
                "jobs": [
                    {
                        "title": "Engineer",
                        "compensation": None,
                    }
                ],
            }
        )
    )
    assert len(out.opps) == 1
    assert out.opps[0].comp_min is None
    assert out.opps[0].comp_max is None


@pytest.mark.asyncio
async def test_ashby_handles_partial_compensation_summary():
    out = await ashby_extract(
        _ei(
            {
                "jobs": [
                    {
                        "title": "Engineer",
                        "compensation": {"compensationTierSummary": None},
                    }
                ],
            }
        )
    )
    assert len(out.opps) == 1


# ---------- Workable ---------------------------------------------------------


@pytest.mark.asyncio
async def test_workable_basic_extraction():
    out = await workable_extract(
        _ei(
            {
                "jobs": [
                    {
                        "title": "Site Reliability Engineer",
                        "department": "Infrastructure",
                        "location": {"city": "Remote"},
                        "remote": True,
                        "url": "https://apply.workable.com/x/j/abc",
                        "published": "2026-04-15T10:00:00Z",
                    }
                ],
            }
        )
    )
    assert len(out.opps) == 1
    assert out.opps[0].title == "Site Reliability Engineer"
    assert out.opps[0].remote_type == RemoteType.REMOTE
    assert out.opps[0].location == "Remote"


@pytest.mark.asyncio
async def test_workable_handles_no_jobs_key():
    out = await workable_extract(_ei({"name": "Bevy", "description": "..."}))
    assert out.opps == []
    assert out.confidence == 0.0


@pytest.mark.asyncio
async def test_workable_intern_classification():
    out = await workable_extract(
        _ei(
            {
                "jobs": [
                    {
                        "title": "Engineering Intern",
                        "location": {"city": "Athens"},
                    }
                ]
            }
        )
    )
    assert out.opps[0].category == OppCategory.INTERNSHIP


# ---------- Contra (freelance) ----------------------------------------------


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_contra_handles_null_budget():
    """Repro: contra serves budget=null for hourly-only listings."""
    out = await contra_extract(
        _ei(
            {
                "opportunities": [
                    {
                        "title": "React Native Build",
                        "budget": None,
                        "budgetMin": 50,
                        "budgetMax": 100,
                        "rateType": "hour",
                        "slug": "react-native-build",
                    }
                ]
            }
        )
    )
    assert len(out.opps) == 1
    o = out.opps[0]
    assert o.comp_min == 50
    assert o.comp_max == 100
    assert o.comp_period == "hour"
    assert o.category == OppCategory.FREELANCE
    assert o.apply_method == ApplyMethod.IN_PLATFORM


@pytest.mark.asyncio
async def test_contra_fixed_project_drops_comp_period():
    out = await contra_extract(
        _ei(
            {
                "opportunities": [
                    {
                        "title": "Landing page redesign",
                        "budgetMin": 1000,
                        "budgetMax": 1000,
                        "rateType": "fixed",
                        "slug": "landing-redesign",
                    }
                ]
            }
        )
    )
    assert out.opps[0].comp_period is None


@pytest.mark.asyncio
async def test_contra_skips_listings_without_title():
    out = await contra_extract(
        _ei(
            {
                "opportunities": [
                    {"title": "", "slug": "skip-me"},
                    {"slug": "no-title"},
                    {"title": "Real gig", "slug": "real"},
                ]
            }
        )
    )
    assert len(out.opps) == 1
    assert out.opps[0].title == "Real gig"

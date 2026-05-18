"""Unit tests for the Greenhouse JSON API extractor.

These tests pin the extractor's contract against a representative Greenhouse
`/v1/boards/<slug>/jobs?content=true` response. The Greenhouse format has
been stable since 2019 but the extractor must remain robust to nullable
nested objects (location, company, departments) — the real API serves all
three as null for some listings.
"""
from __future__ import annotations

import json

import pytest

from src.common.types import ApplyMethod, OppCategory, RemoteType
from src.extractors.base import ExtractInput
from src.extractors.tier1_selectors.greenhouse import extract


def _input(payload: dict | list, *, source_id: int = 1, slug: str = "ats_greenhouse",
           url: str = "https://boards-api.greenhouse.io/v1/boards/stripe/jobs") -> ExtractInput:
    return ExtractInput(
        source_id=source_id,
        source_slug=slug,
        url=url,
        content=json.dumps(payload),
        content_type="application/json",
    )


@pytest.mark.asyncio
async def test_returns_empty_on_invalid_json():
    out = await extract(ExtractInput(
        source_id=1, source_slug="ats_greenhouse",
        url="https://example.com", content="not json", content_type="application/json",
    ))
    assert out.opps == []
    assert out.confidence == 0.0


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_extracts_single_internship():
    out = await extract(_input({"jobs": [{
        "title": "Software Engineering Intern, Backend",
        "company": {"name": "Stripe"},
        "location": {"name": "San Francisco, CA"},
        "absolute_url": "https://stripe.com/jobs/listing/123",
        "updated_at": "2026-05-01T12:00:00Z",
        "content": "<p>Build payment infrastructure.</p>",
    }]}))
    assert len(out.opps) == 1
    o = out.opps[0]
    assert o.title == "Software Engineering Intern, Backend"
    assert o.company == "Stripe"
    assert o.category == OppCategory.INTERNSHIP
    assert o.location == "San Francisco, CA"
    assert o.remote_type == RemoteType.UNSPECIFIED
    assert o.apply_method == ApplyMethod.ATS_FORM
    assert o.canonical_url == "https://stripe.com/jobs/listing/123"
    assert o.extraction_tier == 1
    assert o.extraction_confidence == 0.92
    assert "<" not in o.description  # HTML stripped


@pytest.mark.asyncio
async def test_fellowship_classification():
    out = await extract(_input({"jobs": [{
        "title": "Anthropic Research Fellow 2026",
        "company": {"name": "Anthropic"},
        "location": {"name": "Remote"},
    }]}))
    assert out.opps[0].category == OppCategory.FELLOWSHIP
    assert out.opps[0].remote_type == RemoteType.REMOTE


@pytest.mark.asyncio
async def test_hybrid_location_classification():
    out = await extract(_input({"jobs": [{
        "title": "Backend Engineer",
        "company": {"name": "Acme"},
        "location": {"name": "Hybrid (NYC)"},
    }]}))
    assert out.opps[0].remote_type == RemoteType.HYBRID
    assert out.opps[0].category == OppCategory.FULLTIME


@pytest.mark.asyncio
async def test_residency_classification():
    out = await extract(_input({"jobs": [{
        "title": "AI Residency Program",
        "company": {"name": "Open Source Lab"},
    }]}))
    assert out.opps[0].category == OppCategory.FELLOWSHIP


@pytest.mark.asyncio
async def test_skips_jobs_with_no_title():
    out = await extract(_input({"jobs": [
        {"title": "", "company": {"name": "X"}},
        {"company": {"name": "Y"}},
        {"title": "Valid Role", "company": {"name": "Z"}},
    ]}))
    assert len(out.opps) == 1
    assert out.opps[0].title == "Valid Role"


@pytest.mark.asyncio
async def test_falls_back_to_departments_for_company_name():
    out = await extract(_input({"jobs": [{
        "title": "Engineer",
        "company": None,
        "departments": [{"name": "Engineering Department"}],
    }]}))
    assert out.opps[0].company == "Engineering Department"


@pytest.mark.asyncio
async def test_handles_null_location():
    out = await extract(_input({"jobs": [{
        "title": "Engineer",
        "company": {"name": "Acme"},
        "location": None,
    }]}))
    assert out.opps[0].location is None
    assert out.opps[0].remote_type == RemoteType.UNSPECIFIED


@pytest.mark.asyncio
async def test_url_falls_back_to_input_url_when_absolute_url_missing():
    inp_url = "https://boards-api.greenhouse.io/v1/boards/stripe/jobs"
    out = await extract(_input({"jobs": [{
        "title": "Engineer",
        "company": {"name": "Acme"},
    }]}, url=inp_url))
    assert out.opps[0].canonical_url == inp_url


@pytest.mark.asyncio
async def test_invalid_updated_at_does_not_raise():
    out = await extract(_input({"jobs": [{
        "title": "Engineer",
        "company": {"name": "Acme"},
        "updated_at": "not-a-real-date",
    }]}))
    assert out.opps[0].posted_at is None


@pytest.mark.asyncio
async def test_fingerprint_stable_across_runs():
    payload = {"jobs": [{
        "title": "Engineer",
        "company": {"name": "Acme"},
        "location": {"name": "NYC"},
        "updated_at": "2026-05-01T12:00:00Z",
    }]}
    out1 = await extract(_input(payload))
    out2 = await extract(_input(payload))
    assert out1.opps[0].fingerprint_hash == out2.opps[0].fingerprint_hash


@pytest.mark.asyncio
async def test_data_array_alias():
    out = await extract(_input({"data": [
        {"title": "Engineer", "company": {"name": "Acme"}},
    ]}))
    assert len(out.opps) == 1

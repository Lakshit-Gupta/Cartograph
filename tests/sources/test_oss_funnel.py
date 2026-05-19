"""Unit tests for Phase 3.4 — the OSS contribution funnel.

Mocks GitHub Search API via respx; never hits live network. Worker-
level DB writes are stubbed via monkeypatch so no live Postgres is
required.

Coverage:
  * `parse_issue_to_opportunity` shape (smoke).
  * Stale-issue filtering via `_is_stale`.
  * Per-company emit cap enforced inside `fetch_company_issues`.
  * Worker idles silently when no targets are configured.
  * 403 rate-limit triggers retry+backoff then surfaces as
    `rate_limited=True`.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType
from src.sources.oss_funnel import github_issues as ghi

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _fresh_iso(days_ago: int = 0) -> str:
    """ISO-8601 timestamp `days_ago` days before NOW (UTC)."""
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


def _sample_issue(
    *,
    number: int = 1,
    org: str = "vercel",
    repo: str = "next.js",
    title: str = "Add typings to hooks",
    body: str = "Looks like a good starter task — see CONTRIBUTING.md.",
    updated_days_ago: int = 1,
    assignees: list[dict] | None = None,
    pr: bool = False,
) -> dict[str, Any]:
    """Minimal stand-in for a GitHub Search API issue object."""
    out: dict[str, Any] = {
        "number": number,
        "html_url": f"https://github.com/{org}/{repo}/issues/{number}",
        "repository_url": f"https://api.github.com/repos/{org}/{repo}",
        "title": title,
        "body": body,
        "updated_at": _fresh_iso(updated_days_ago),
        "assignees": assignees or [],
    }
    if pr:
        out["pull_request"] = {"url": "x"}
    return out


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_fingerprint_is_deterministic_and_namespaced():
    a = ghi.fingerprint_for("vercel", "next.js", 42)
    b = ghi.fingerprint_for("vercel", "next.js", 42)
    assert a == b
    # `oss:` namespace prevents collision with future hash schemes.
    expected = hashlib.sha256(b"oss:vercel:next.js:42").hexdigest()
    assert a == expected
    assert ghi.fingerprint_for("vercel", "next.js", 43) != a
    assert ghi.fingerprint_for("anthropics", "next.js", 42) != a


def test_split_repo_url_handles_trailing_slash():
    assert ghi._split_repo_url("https://api.github.com/repos/vercel/next.js") == ("vercel", "next.js")
    assert ghi._split_repo_url("https://api.github.com/repos/vercel/next.js/") == ("vercel", "next.js")
    assert ghi._split_repo_url("") == ("", "")
    assert ghi._split_repo_url("https://example.com/x") == ("", "")


# ---------------------------------------------------------------------------
# parse_issue_to_opportunity
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_parses_github_issue_to_opportunity():
    issue = _sample_issue(number=101, org="vercel", repo="next.js", title="Improve docs")
    opp = ghi.parse_issue_to_opportunity(issue, source_id=30, company_name="Vercel")
    assert isinstance(opp, Opportunity)
    assert opp.source_id == 30
    assert opp.canonical_url == "https://github.com/vercel/next.js/issues/101"
    assert opp.title.startswith("[OSS]")
    assert "Improve docs" in opp.title
    assert opp.company == "Vercel"
    assert opp.category is OppCategory.FREELANCE
    assert opp.remote_type is RemoteType.REMOTE
    assert opp.apply_method is ApplyMethod.EXTERNAL
    assert opp.comp_min is None and opp.comp_max is None and opp.comp_currency is None
    assert opp.apply_url == opp.canonical_url
    assert opp.fingerprint_hash == ghi.fingerprint_for("vercel", "next.js", 101)
    assert opp.extraction_tier == 0
    assert 0.0 < opp.extraction_confidence <= 1.0


def test_parse_issue_handles_missing_fields():
    assert ghi.parse_issue_to_opportunity({}, source_id=1, company_name="X") is None
    # Number must be an int — GitHub Search will never send a string here,
    # but defensive parsers keep regressions out.
    assert (
        ghi.parse_issue_to_opportunity(
            {"number": "not-int", "html_url": "x", "repository_url": "https://api.github.com/repos/a/b"},
            source_id=1,
            company_name="X",
        )
        is None
    )


def test_parse_issue_clamps_description_to_1500_chars():
    long_body = "x" * 5000
    issue = _sample_issue(body=long_body)
    opp = ghi.parse_issue_to_opportunity(issue, source_id=1, company_name="X")
    assert opp is not None
    assert opp.description is not None
    assert len(opp.description) == 1500


# ---------------------------------------------------------------------------
# Stale-issue filter
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_stale_issue_skipped():
    """`_is_stale` returns True for issues older than threshold_days."""
    assert ghi._is_stale(_fresh_iso(days_ago=45)) is True
    assert ghi._is_stale(_fresh_iso(days_ago=1)) is False
    # Edge: just over the threshold.
    assert ghi._is_stale(_fresh_iso(days_ago=31)) is True
    # Empty / malformed strings are treated as fresh — never silently drop.
    assert ghi._is_stale("") is False
    assert ghi._is_stale("not-a-date") is False


# ---------------------------------------------------------------------------
# fetch_company_issues — respx-mocked
# ---------------------------------------------------------------------------


@pytest.mark.smoke
async def test_fetch_company_issues_filters_stale_and_assigned():
    """Stale, assigned, and PR-typed items are dropped client-side."""
    fresh = _sample_issue(number=1)
    stale = _sample_issue(number=2, updated_days_ago=45)
    assigned = _sample_issue(number=3, assignees=[{"login": "bob"}])
    pr_typed = _sample_issue(number=4, pr=True)
    payload = {"items": [stale, assigned, pr_typed, fresh]}

    async with respx.mock(assert_all_called=False) as r:
        r.get("https://api.github.com/search/issues").respond(json=payload)
        async with ghi.GitHubIssueScanner(token="") as scanner:
            result = await scanner.fetch_company_issues("vercel", limit=5)

    assert result.rate_limited is False
    assert result.error is None
    assert len(result.issues) == 1
    assert result.issues[0]["number"] == 1


@pytest.mark.smoke
async def test_per_company_daily_cap_enforced():
    """`limit=5` truncates a 20-issue payload to the first 5 fresh items."""
    items = [_sample_issue(number=i, updated_days_ago=1) for i in range(20)]
    async with respx.mock(assert_all_called=False) as r:
        r.get("https://api.github.com/search/issues").respond(json={"items": items})
        async with ghi.GitHubIssueScanner(token="") as scanner:
            result = await scanner.fetch_company_issues("vercel", limit=5)

    assert len(result.issues) == 5


async def test_fetch_empty_org_short_circuits():
    """Empty org never hits the network — defensive against bad config."""
    async with respx.mock(assert_all_called=False) as r:
        route = r.get("https://api.github.com/search/issues")
        async with ghi.GitHubIssueScanner(token="") as scanner:
            result = await scanner.fetch_company_issues("", limit=5)
    assert result.issues == []
    assert result.error == "empty_org"
    assert route.called is False


# ---------------------------------------------------------------------------
# Rate-limit handling
# ---------------------------------------------------------------------------


@pytest.mark.smoke
async def test_403_rate_limit_backs_off(monkeypatch):
    """A 403 with rate-limit headers triggers retry, then surfaces as
    `rate_limited=True` after the retry budget is exhausted.
    """
    # Make sleep instantaneous so the test doesn't actually wait 30s.
    sleeps: list[float] = []

    async def _fake_sleep(d: float) -> None:
        sleeps.append(d)

    monkeypatch.setattr("src.sources.oss_funnel.github_issues.asyncio.sleep", _fake_sleep)

    async with respx.mock(assert_all_called=False) as r:
        r.get("https://api.github.com/search/issues").respond(
            status_code=403,
            headers={"Retry-After": "1"},
            json={"message": "rate limited"},
        )
        async with ghi.GitHubIssueScanner(token="") as scanner:
            result = await scanner.fetch_company_issues("vercel", limit=5)

    assert result.rate_limited is True
    assert result.issues == []
    # We should have slept at least once (per retry).
    assert len(sleeps) >= 1
    # The floor enforced in _retry_after_seconds guarantees ≥ 30s.
    assert all(s >= ghi._RETRY_AFTER_FLOOR_S for s in sleeps)


async def test_500_status_returns_error_not_rate_limited(monkeypatch):
    """5xx is a transient backend error — should propagate as `error`,
    not `rate_limited`, so the caller can decide whether to retry the
    org tomorrow vs treat it as fatal."""
    async with respx.mock(assert_all_called=False) as r:
        r.get("https://api.github.com/search/issues").respond(status_code=500, text="boom")
        async with ghi.GitHubIssueScanner(token="") as scanner:
            result = await scanner.fetch_company_issues("vercel", limit=5)
    assert result.rate_limited is False
    assert result.error == "status:500"
    assert result.issues == []


async def test_http_error_recovers_gracefully(monkeypatch):
    """httpx-level transport errors return a typed FetchResult, never raise."""

    async def boom(*_a, **_kw):
        raise httpx.ConnectError("DNS go brr")

    async with ghi.GitHubIssueScanner(token="") as scanner:
        monkeypatch.setattr(scanner._client, "get", boom)
        result = await scanner.fetch_company_issues("vercel", limit=5)
    assert result.issues == []
    assert result.error is not None
    assert result.error.startswith("http:")


# ---------------------------------------------------------------------------
# Worker happy-path & feature-flag refusal
# ---------------------------------------------------------------------------


async def test_no_targets_configured_is_silent(monkeypatch):
    """Flag-on + zero target rows → empty summary, zero publishes."""
    from src.workers import oss_funnel as worker

    class _FakeSettings:
        mp_oss_funnel_enabled = True
        github_token = ""

    monkeypatch.setattr(worker, "get_settings", lambda: _FakeSettings())

    async def _load() -> list[dict]:
        return []

    async def _src_id() -> int:
        return 30

    monkeypatch.setattr(worker, "_load_active_targets", _load)
    monkeypatch.setattr(worker, "_resolve_source_id", _src_id)

    class _FakeQ:
        async def publish(self, *_a, **_kw):
            raise AssertionError("publish must NOT be called when no targets exist")

    summary = await worker.run_daily_scan(_FakeQ())
    assert summary.companies_scanned == 0
    assert summary.issues_emitted == 0


async def test_feature_flag_off_short_circuits(monkeypatch):
    """Flag-off → never touches DB or HTTP."""
    from src.workers import oss_funnel as worker

    class _FakeSettings:
        mp_oss_funnel_enabled = False
        github_token = ""

    monkeypatch.setattr(worker, "get_settings", lambda: _FakeSettings())

    async def _load() -> list[dict]:
        raise AssertionError("_load_active_targets must NOT run when flag is off")

    monkeypatch.setattr(worker, "_load_active_targets", _load)

    summary = await worker.run_daily_scan(q=None)  # q is unused when flag off
    assert summary.companies_scanned == 0
    assert summary.issues_emitted == 0

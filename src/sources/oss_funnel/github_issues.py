"""GitHub Search API client for the OSS contribution funnel.

The funnel hits `/search/issues` with a constant query:

    is:open is:issue label:"good first issue" org:<org>

then filters returned issues against:
  * `updated_at >= NOW() - _STALE_THRESHOLD_DAYS`
  * `assignees == []`
  * cap of `limit` issues per company (default 5)

Each survivor maps to an `Opportunity` via `parse_issue_to_opportunity`;
the caller pushes it through `extractors.persist.persist_and_publish`.

Rate-limit handling: authenticated requests get 30 req/min, unauth 10
req/min. On a rate-limit status (see `_RATE_LIMIT_HTTP_STATUSES`) we
wait >=`_RETRY_AFTER_FLOOR_S` and retry up to `_RATELIMIT_MAX_RETRIES`.

Ref: https://docs.github.com/en/rest/search/search#search-issues-and-pull-requests
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from src.common.logger import get_logger
from src.common.secrets import get_settings
from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType

_log = get_logger(__name__)


_GITHUB_SEARCH_URL = "https://api.github.com/search/issues"

# Tunables — module-scope so they aren't flagged as inline magic
# numbers and so retuning is a one-line edit.

# Per-request HTTP timeout in seconds. GitHub Search responses are
# typically <1s; we set a generous ceiling for tail-latency events.
_HTTP_TIMEOUT_S = 15.0

# Hard floor for retry sleep duration. Even when GitHub asks for less
# we honour the floor — under aggressive rate-limits the floor
# guarantees forward progress.
_RETRY_AFTER_FLOOR_S = 30.0

# Maximum retry attempts after a rate-limit response.
_RATELIMIT_MAX_RETRIES = 3

# GitHub Search returns at most this many items per page.
_GITHUB_PER_PAGE_CAP = 100

# Our default per_page — well below the cap; covers the default
# limit=5 with comfortable headroom for client-side filtering.
_DEFAULT_PER_PAGE = 30

# Issues with `updated_at` older than this many days are considered
# stale / abandoned and dropped client-side.
_STALE_THRESHOLD_DAYS = 30

# Opportunity body max chars — prevents pathologically long issue
# descriptions from blowing up the opportunities table.
_DESCRIPTION_MAX_CHARS = 1500

# Opportunity title max chars — matches the DB column width.
_TITLE_MAX_CHARS = 500

# Per-company daily emit cap. Prevents one big monorepo from drowning
# the digest. Persists across pages via the V016 fingerprint dedupe.
_DEFAULT_LIMIT = 5

# Rate-limit statuses GitHub uses. 403 is the primary rate-limit
# signal, 429 is the secondary rate-limit signal.
_PRIMARY_RATE_LIMIT_STATUS = 403
_SECONDARY_RATE_LIMIT_STATUS = 429
_RATE_LIMIT_HTTP_STATUSES = frozenset({_PRIMARY_RATE_LIMIT_STATUS, _SECONDARY_RATE_LIMIT_STATUS})

# Status floor for treating a response as a non-success bail.
_CLIENT_ERROR_FLOOR = 400

# Cap on how much of an error response body we include in logs.
_ERROR_BODY_LOG_CHARS = 200

# OppCategory has no OSS_CONTRIBUTION; the V001 CHECK constraint on
# opportunities.category is locked to {fulltime,internship,fellowship,
# freelance,contract,unknown}. FREELANCE is the closest semantic fit.
_OSS_OPP_CATEGORY = OppCategory.FREELANCE
# Structured API output → high confidence baseline for the ranker.
_OSS_EXTRACTION_CONFIDENCE = 0.9


def fingerprint_for(org: str, repo: str, issue_number: int) -> str:
    """Deterministic dedup key, `oss:` namespaced."""
    return hashlib.sha256(f"oss:{org}:{repo}:{issue_number}".encode()).hexdigest()


def _parse_iso_timestamp(raw: str) -> datetime | None:
    """Parse a GitHub `updated_at` stamp, returning None on failure.

    Normalises trailing `Z` to `+00:00` for `fromisoformat`
    compatibility on older Python and attaches UTC if naive.
    """
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _is_stale(updated_at_iso: str, *, threshold_days: int = _STALE_THRESHOLD_DAYS) -> bool:
    """True when `updated_at` is older than `threshold_days`. Malformed
    / empty stamps are treated as fresh (False)."""
    dt = _parse_iso_timestamp(updated_at_iso)
    if dt is None:
        return False
    return dt < datetime.now(UTC) - timedelta(days=threshold_days)


def _split_repo_url(repo_url: str) -> tuple[str, str]:
    """`...github.com/repos/vercel/next.js` -> `('vercel', 'next.js')`.

    Returns `('', '')` for any non-github URL or malformed string.
    """
    if not repo_url or "github.com/repos/" not in repo_url:
        return "", ""
    after = repo_url.split("github.com/repos/", 1)[1].rstrip("/")
    parts = [p for p in after.split("/") if p]
    if len(parts) < 2:
        return "", ""
    return parts[0], parts[1]


def _extract_issue_identity(
    issue: dict[str, Any],
) -> tuple[int, str, str, str] | None:
    """`(issue_number, html_url, org, repo)` or None for malformed payloads."""
    issue_number = issue.get("number")
    html_url = issue.get("html_url") or ""
    repo_url = issue.get("repository_url") or ""
    if not isinstance(issue_number, int) or not html_url or not repo_url:
        return None
    org, repo = _split_repo_url(repo_url)
    if not org or not repo:
        return None
    return issue_number, html_url, org, repo


def _build_opp_title(repo: str, issue_number: int, raw_title: str) -> str:
    """`[OSS] <repo>: <title>`, clipped to `_TITLE_MAX_CHARS`."""
    title = (raw_title or "").strip() or f"{repo}#{issue_number}"
    return f"[OSS] {repo}: {title}"[:_TITLE_MAX_CHARS]


def _build_opp_description(raw_body: str) -> str | None:
    """Trim body to `_DESCRIPTION_MAX_CHARS`; None when empty."""
    body = (raw_body or "").strip()[:_DESCRIPTION_MAX_CHARS]
    return body or None


def _opportunity_from_parts(
    *,
    source_id: int,
    company_name: str,
    issue_number: int,
    html_url: str,
    org: str,
    repo: str,
    title: str,
    description: str | None,
) -> Opportunity:
    """Build the canonical Opportunity row. All comp fields stay None
    (OSS is unpaid); apply_method is EXTERNAL (GitHub web flow)."""
    return Opportunity(
        source_id=source_id,
        canonical_url=html_url,
        title=title,
        company=company_name,
        description=description,
        comp_min=None,
        comp_max=None,
        comp_currency=None,
        comp_period=None,
        location=None,
        remote_type=RemoteType.REMOTE,
        category=_OSS_OPP_CATEGORY,
        posted_at=None,
        apply_url=html_url,
        apply_method=ApplyMethod.EXTERNAL,
        fingerprint_hash=fingerprint_for(org, repo, issue_number),
        extraction_tier=0,
        extraction_confidence=_OSS_EXTRACTION_CONFIDENCE,
    )


def parse_issue_to_opportunity(
    issue: dict[str, Any],
    *,
    source_id: int,
    company_name: str,
) -> Opportunity | None:
    """Map a GitHub Search API issue to a canonical Opportunity.

    Returns None for malformed payloads (missing url/number, non-github
    `repository_url`). The caller handles upstream filtering (stale,
    assigned, per-company cap).
    """
    identity = _extract_issue_identity(issue)
    if identity is None:
        return None
    issue_number, html_url, org, repo = identity
    return _opportunity_from_parts(
        source_id=source_id,
        company_name=company_name,
        issue_number=issue_number,
        html_url=html_url,
        org=org,
        repo=repo,
        title=_build_opp_title(repo, issue_number, issue.get("title") or ""),
        description=_build_opp_description(issue.get("body") or ""),
    )


@dataclass(frozen=True, slots=True)
class FetchResult:
    """One org fetch. `rate_limited=True` = retries exhausted on a
    rate-limit; caller should soft-skip, NOT treat as empty result."""

    issues: list[dict[str, Any]]
    rate_limited: bool = False
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _FilterCtx:
    """Per-call filter parameters. Bundled to keep helper signatures
    tight (the previous flat-args version pushed `_try_one_attempt`
    past the 5-param threshold)."""

    org: str
    limit: int
    stale_threshold_days: int


def _should_keep_issue(item: dict[str, Any], *, stale_threshold_days: int) -> bool:
    """Drop PRs (Search returns them as 'issues' with a `pull_request`
    sub-object), already-assigned items, and stale items."""
    if item.get("pull_request"):
        return False
    if item.get("assignees"):
        return False
    return not _is_stale(item.get("updated_at") or "", threshold_days=stale_threshold_days)


def _filter_issues(
    items: list[dict[str, Any]],
    *,
    limit: int,
    stale_threshold_days: int,
) -> list[dict[str, Any]]:
    """Apply PR / assignee / stale filters and truncate to `limit`."""
    kept: list[dict[str, Any]] = []
    for it in items:
        if not _should_keep_issue(it, stale_threshold_days=stale_threshold_days):
            continue
        kept.append(it)
        if len(kept) >= limit:
            break
    return kept


class GitHubIssueScanner:
    """Async client for `/search/issues` with client-side filtering.

    Concurrency: one shared `httpx.AsyncClient` per scanner, opened
    lazily. The daily cron creates ONE scanner and scans every active
    target_company SEQUENTIALLY — the per-minute search rate-limit
    would torch a parallel batch.
    """

    def __init__(self, *, token: str | None = None, per_page: int = _DEFAULT_PER_PAGE) -> None:
        self._token = (token or "").strip() or None
        self._per_page = max(1, min(_GITHUB_PER_PAGE_CAP, per_page))
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> GitHubIssueScanner:
        await self._ensure_client()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "cartograph-oss-funnel/0.1",
            }
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            self._client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S, headers=headers)
        return self._client

    def _build_search_params(self, org: str) -> dict[str, Any]:
        """Query string is BYTE-IDENTICAL to the pre-refactor build."""
        return {
            "q": f'is:open is:issue label:"good first issue" org:{org}',
            "sort": "created",
            "order": "desc",
            "per_page": self._per_page,
        }

    async def _request_once(self, params: dict[str, Any]) -> httpx.Response | str:
        """One GET. Response on success, exception class name on transport failure."""
        client = await self._ensure_client()
        try:
            return await client.get(_GITHUB_SEARCH_URL, params=params)
        except httpx.HTTPError as e:
            return e.__class__.__name__

    async def _wait_for_rate_limit(self, resp: httpx.Response, *, org: str, attempt: int) -> None:
        """Log + sleep `_retry_after_seconds(resp)` on a rate-limit response."""
        reset = self._retry_after_seconds(resp)
        _log.warning(
            "oss_funnel_rate_limited",
            org=org,
            status=resp.status_code,
            retry_after_s=reset,
            attempt=attempt + 1,
        )
        await asyncio.sleep(reset)

    @staticmethod
    def _parse_json_payload(resp: httpx.Response, *, org: str) -> list[dict[str, Any]] | str:
        """Decode items array; returns 'bad_json' string on decode failure."""
        try:
            payload = resp.json()
        except ValueError as e:
            _log.warning("oss_funnel_bad_json", org=org, err=str(e))
            return "bad_json"
        return payload.get("items") or []

    def _classify_response(self, resp: httpx.Response, ctx: _FilterCtx) -> FetchResult:
        """Map a non-rate-limit response to a terminal `FetchResult`."""
        if resp.status_code >= _CLIENT_ERROR_FLOOR:
            _log.warning(
                "oss_funnel_bad_status",
                org=ctx.org,
                status=resp.status_code,
                body=resp.text[:_ERROR_BODY_LOG_CHARS],
            )
            return FetchResult(issues=[], error=f"status:{resp.status_code}")
        items = self._parse_json_payload(resp, org=ctx.org)
        if isinstance(items, str):
            return FetchResult(issues=[], error=items)
        kept = _filter_issues(
            items,
            limit=ctx.limit,
            stale_threshold_days=ctx.stale_threshold_days,
        )
        return FetchResult(issues=kept)

    async def _try_one_attempt(
        self,
        params: dict[str, Any],
        ctx: _FilterCtx,
        attempt: int,
    ) -> FetchResult | None:
        """One GET + classify. Returns terminal `FetchResult` or None
        to signal 'rate-limited, please retry'. Mirrors pre-refactor
        flow: on the FINAL attempt we surface `rate_limited=True`
        immediately without sleeping."""
        outcome = await self._request_once(params)
        if isinstance(outcome, str):
            _log.warning("oss_funnel_http_error", org=ctx.org, err=outcome)
            return FetchResult(issues=[], error=f"http:{outcome}")
        resp = outcome
        if resp.status_code in _RATE_LIMIT_HTTP_STATUSES:
            if attempt >= _RATELIMIT_MAX_RETRIES:
                return FetchResult(issues=[], rate_limited=True)
            await self._wait_for_rate_limit(resp, org=ctx.org, attempt=attempt)
            return None
        return self._classify_response(resp, ctx)

    async def fetch_company_issues(
        self,
        org: str,
        *,
        limit: int = _DEFAULT_LIMIT,
        stale_threshold_days: int = _STALE_THRESHOLD_DAYS,
    ) -> FetchResult:
        """Up to `limit` non-stale, unassigned issues for `org`.

        Server-side filters: `is:open is:issue label:"good first issue"`.
        Client-side filters: PR/assignee/stale (in `_filter_issues`).
        """
        if not org or not org.strip():
            return FetchResult(issues=[], error="empty_org")
        params = self._build_search_params(org)
        ctx = _FilterCtx(org=org, limit=limit, stale_threshold_days=stale_threshold_days)
        for attempt in range(_RATELIMIT_MAX_RETRIES + 1):
            result = await self._try_one_attempt(params, ctx, attempt)
            if result is not None:
                return result
        # Retry budget exhausted on rate-limit retries.
        return FetchResult(issues=[], rate_limited=True)

    @staticmethod
    def _retry_after_from_header(value: str | None) -> float | None:
        """Parse `Retry-After`. None if absent/unparseable."""
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    @staticmethod
    def _retry_after_from_reset(value: str | None) -> float | None:
        """Parse `X-RateLimit-Reset` (UTC epoch second) → delta from now.
        None if absent, unparseable, or in the past."""
        if not value:
            return None
        try:
            wait = float(value) - datetime.now(UTC).timestamp()
        except ValueError:
            return None
        return wait if wait > 0 else None

    @classmethod
    def _retry_after_seconds(cls, resp: httpx.Response) -> float:
        """Wait interval: `Retry-After` > `X-RateLimit-Reset` > floor.
        Never returns 0 — the floor guarantees forward progress."""
        explicit = cls._retry_after_from_header(resp.headers.get("Retry-After"))
        if explicit is not None:
            return max(_RETRY_AFTER_FLOOR_S, explicit)
        reset = cls._retry_after_from_reset(resp.headers.get("X-RateLimit-Reset"))
        if reset is not None:
            return max(_RETRY_AFTER_FLOOR_S, reset)
        return _RETRY_AFTER_FLOOR_S


async def open_scanner() -> GitHubIssueScanner:
    """Wire up token from settings; tests pass `token=""` directly."""
    settings = get_settings()
    return GitHubIssueScanner(token=settings.github_token)

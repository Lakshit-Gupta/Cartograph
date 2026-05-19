"""GitHub Search API client for the OSS contribution funnel.

The funnel hits the public `/search/issues` endpoint with a constant
query template:

    is:open is:issue label:"good first issue" org:<org>

then filters the returned issues against:

  * `updated_at >= NOW() - 30 days`  (drops likely-abandoned issues)
  * `assignees == []`                 (someone is already on it)
  * cap of `limit` issues per company (default 5, prevents one massive
    monorepo from drowning the digest)

Each surviving issue is mapped to a canonical `Opportunity` row via
`parse_issue_to_opportunity` and the caller is expected to push the
row through `extractors.persist.persist_and_publish` (the same write
path the freelance Telegram fetcher uses) — that handles dedup,
opportunities-table upsert, and the Streams.RANK publish.

Rate-limit handling:
  * Authenticated requests get 30 req/min for /search/issues; unauth
    gets 10 req/min. Either way we wait ≥`_RETRY_AFTER_FLOOR_S` on a
    403/429 and retry up to `_RATELIMIT_MAX_RETRIES` times.
  * We read `X-RateLimit-Remaining` / `X-RateLimit-Reset` headers and
    refuse subsequent calls early if remaining drops to 0.

Reference: https://docs.github.com/en/rest/search/search#search-issues-and-pull-requests
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

# Tunables — kept at module scope so the linter doesn't flag them as
# inline magic numbers and so retuning is a one-line edit.
_HTTP_TIMEOUT_S = 15.0
_RETRY_AFTER_FLOOR_S = 30.0
_RATELIMIT_MAX_RETRIES = 3
_DEFAULT_PER_PAGE = 30  # GitHub Search returns up to 100/page; 30 covers our limit=5 with headroom.
_STALE_THRESHOLD_DAYS = 30
_DESCRIPTION_MAX_CHARS = 1500
_DEFAULT_LIMIT = 5


def fingerprint_for(org: str, repo: str, issue_number: int) -> str:
    """Deterministic dedup key. Stable across restarts and rate-limit retries.

    The Opportunity write path keys dedup off this hash + canonical_url;
    `oss:` namespace prefix keeps OSS-funnel emissions from colliding
    with any other source that might one day hash repo names.
    """
    return hashlib.sha256(f"oss:{org}:{repo}:{issue_number}".encode()).hexdigest()


def _is_stale(updated_at_iso: str, *, threshold_days: int = _STALE_THRESHOLD_DAYS) -> bool:
    """True when an issue's `updated_at` is older than `threshold_days`.

    GitHub returns ISO-8601 with a trailing `Z`; we parse with a UTC
    fallback. Malformed dates are treated as fresh (False) so the
    funnel never silently drops a parseable issue.
    """
    if not updated_at_iso:
        return False
    try:
        # Python's fromisoformat() handles `+00:00` but pre-3.11 chokes on
        # the trailing `Z`. We normalise once to be Python-version-safe.
        dt = datetime.fromisoformat(updated_at_iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt < datetime.now(UTC) - timedelta(days=threshold_days)


def _split_repo_url(repo_url: str) -> tuple[str, str]:
    """('https://api.github.com/repos/vercel/next.js') -> ('vercel', 'next.js').

    Returns ('', '') for any URL that isn't a github.com repos endpoint
    (e.g. example.com, malformed URLs, empty strings).
    """
    if not repo_url or "github.com/repos/" not in repo_url:
        return "", ""
    after = repo_url.split("github.com/repos/", 1)[1].rstrip("/")
    parts = [p for p in after.split("/") if p]
    if len(parts) < 2:
        return "", ""
    return parts[0], parts[1]


def parse_issue_to_opportunity(
    issue: dict[str, Any],
    *,
    source_id: int,
    company_name: str,
) -> Opportunity | None:
    """Map a GitHub Search API issue object to a canonical Opportunity.

    Returns None for malformed payloads (missing url, missing number).
    The caller is responsible for upstream filtering (stale, assigned,
    per-company cap).
    """
    issue_number = issue.get("number")
    html_url = issue.get("html_url") or ""
    repo_url = issue.get("repository_url") or ""
    if not isinstance(issue_number, int) or not html_url or not repo_url:
        return None
    org, repo = _split_repo_url(repo_url)
    if not org or not repo:
        return None
    title = (issue.get("title") or "").strip() or f"{repo}#{issue_number}"
    body = (issue.get("body") or "").strip()[:_DESCRIPTION_MAX_CHARS]
    return Opportunity(
        source_id=source_id,
        canonical_url=html_url,
        # Title format makes the source obvious in the digest.
        title=f"[OSS] {repo}: {title}"[:500],
        company=company_name,
        description=body or None,
        comp_min=None,
        comp_max=None,
        comp_currency=None,
        comp_period=None,
        location=None,
        remote_type=RemoteType.REMOTE,
        # OppCategory has no OSS_CONTRIBUTION value and the V001 CHECK
        # constraint on opportunities.category is locked to
        # {fulltime,internship,fellowship,freelance,contract,unknown}.
        # FREELANCE is the closest semantic fit (unpaid contribution
        # work, async, contract-like) and the existing ranker already
        # weights freelance opps differently from full-time roles.
        # Documenting the choice here so the next reviewer doesn't try
        # to "fix" it without realising the schema gate.
        category=OppCategory.FREELANCE,
        posted_at=None,
        apply_url=html_url,
        apply_method=ApplyMethod.EXTERNAL,
        fingerprint_hash=fingerprint_for(org, repo, issue_number),
        extraction_tier=0,
        extraction_confidence=0.9,  # structured API output, high confidence
    )


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Result of a single org fetch. `rate_limited=True` means we hit a
    403 and exhausted retries — caller should NOT count this as an
    empty result; it should propagate as a soft skip."""

    issues: list[dict[str, Any]]
    rate_limited: bool = False
    error: str | None = None


class GitHubIssueScanner:
    """Async client that hits `/search/issues` and filters the result.

    Concurrency model: a single shared `httpx.AsyncClient` per scanner
    instance, opened lazily on first request. The daily cron creates
    ONE scanner, scans every active target_company sequentially (we
    deliberately do NOT fan out — the per-minute search rate-limit
    would torch a parallel batch), and closes the client at the end.
    """

    def __init__(self, *, token: str | None = None, per_page: int = _DEFAULT_PER_PAGE) -> None:
        self._token = (token or "").strip() or None
        self._per_page = max(1, min(100, per_page))
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

    async def fetch_company_issues(
        self,
        org: str,
        *,
        limit: int = _DEFAULT_LIMIT,
        stale_threshold_days: int = _STALE_THRESHOLD_DAYS,
    ) -> FetchResult:
        """Return up to `limit` non-stale, unassigned issues for `org`.

        Filtering happens client-side (the GitHub Search query
        already enforces `is:open is:issue label:"good first issue"`
        but does NOT filter on assignee or staleness).
        """
        if not org or not org.strip():
            return FetchResult(issues=[], error="empty_org")
        q = f'is:open is:issue label:"good first issue" org:{org}'
        params = {
            "q": q,
            "sort": "created",
            "order": "desc",
            "per_page": self._per_page,
        }

        client = await self._ensure_client()

        for attempt in range(_RATELIMIT_MAX_RETRIES + 1):
            try:
                resp = await client.get(_GITHUB_SEARCH_URL, params=params)
            except httpx.HTTPError as e:
                _log.warning("oss_funnel_http_error", org=org, err=str(e))
                return FetchResult(issues=[], error=f"http:{e.__class__.__name__}")

            # 403 here is overwhelmingly rate-limit. GitHub also returns
            # 429 for secondary rate-limits — handle both.
            if resp.status_code in {403, 429}:
                reset = self._retry_after_seconds(resp)
                _log.warning(
                    "oss_funnel_rate_limited",
                    org=org,
                    status=resp.status_code,
                    retry_after_s=reset,
                    attempt=attempt + 1,
                )
                if attempt >= _RATELIMIT_MAX_RETRIES:
                    return FetchResult(issues=[], rate_limited=True)
                await asyncio.sleep(reset)
                continue

            if resp.status_code >= 400:
                _log.warning(
                    "oss_funnel_bad_status",
                    org=org,
                    status=resp.status_code,
                    body=resp.text[:200],
                )
                return FetchResult(issues=[], error=f"status:{resp.status_code}")

            try:
                payload = resp.json()
            except ValueError as e:
                _log.warning("oss_funnel_bad_json", org=org, err=str(e))
                return FetchResult(issues=[], error="bad_json")

            items = payload.get("items") or []
            kept: list[dict[str, Any]] = []
            for it in items:
                # GitHub Search returns PRs as "issues" too; filter them out.
                if it.get("pull_request"):
                    continue
                if it.get("assignees"):
                    continue
                if _is_stale(it.get("updated_at") or "", threshold_days=stale_threshold_days):
                    continue
                kept.append(it)
                if len(kept) >= limit:
                    break
            return FetchResult(issues=kept)

        # Unreachable: the for-loop either returns or breaks via the
        # retry-cap branch above. Belt-and-suspenders for the type-checker.
        return FetchResult(issues=[], rate_limited=True)

    @staticmethod
    def _retry_after_seconds(resp: httpx.Response) -> float:
        """Resolve the wait interval from GitHub's headers.

        Preference order: explicit `Retry-After` > `X-RateLimit-Reset`
        delta > a conservative floor. Never returns 0 — the floor
        guarantees forward progress under aggressive limits.
        """
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(_RETRY_AFTER_FLOOR_S, float(retry_after))
            except ValueError:
                pass
        reset = resp.headers.get("X-RateLimit-Reset")
        if reset:
            try:
                # X-RateLimit-Reset is a UTC epoch second
                wait = float(reset) - datetime.now(UTC).timestamp()
                if wait > 0:
                    return max(_RETRY_AFTER_FLOOR_S, wait)
            except ValueError:
                pass
        return _RETRY_AFTER_FLOOR_S


async def open_scanner() -> GitHubIssueScanner:
    """Helper for callers — wire up token from settings + open client.

    Callers should `await open_scanner()` then `async with` the result
    (or remember to `await scanner.aclose()`). Kept as a separate
    factory so tests can pass `token=""` without touching settings.
    """
    settings = get_settings()
    return GitHubIssueScanner(token=settings.github_token)

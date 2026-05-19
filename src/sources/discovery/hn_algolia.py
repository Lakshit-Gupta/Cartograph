"""HN Algolia search for aggregator mentions.

Queries the public Algolia search API for HN stories + comments mentioning
job boards / hiring lists in the last 90 days. The Algolia endpoint is
unauthenticated + CF-free + rate-limit-lenient.

Strategy: a small set of canned queries; for each result, extract URLs from
the story_url + comment_text fields. The classifier filters out off-topic hits.
"""

from __future__ import annotations

import re
import time
from urllib.parse import quote

import httpx

from src.common.logger import get_logger
from src.sources.discovery.base import CandidateSource

_log = get_logger(__name__)

_ALGOLIA_URL = "https://hn.algolia.com/api/v1/search"
_QUERIES = [
    '"job board"',
    '"hiring"',
    '"internship"',
    '"jobs list"',
    '"freelance"',
]
_DAYS_BACK = 90
_HITS_PER_PAGE = 30  # keep small — each hit ~= one candidate, classifier cap is 50/day

_URL_RE = re.compile(r"https?://[^\s<>\"'\)]+")
_NOISE_HOSTS = {
    "news.ycombinator.com",
    "ycombinator.com",
    "github.com",
}


def _host(url: str) -> str:
    try:
        return url.split("://", 1)[1].split("/", 1)[0].lower()
    except (IndexError, ValueError):
        return ""


def _hit_to_candidates(hit: dict, query: str) -> list[CandidateSource]:
    """One Algolia hit can yield 0..N candidates (story URL + URLs in comment)."""
    out: list[CandidateSource] = []
    seen: set[str] = set()

    story_url = hit.get("url") or ""
    title = hit.get("title") or hit.get("story_title") or ""
    # comment_text may be HTML — strip tags coarsely.
    body = hit.get("comment_text") or hit.get("story_text") or ""
    body_clean = re.sub(r"<[^>]+>", " ", body)

    # First: the story URL itself if present.
    if story_url and _host(story_url) not in _NOISE_HOSTS:
        seen.add(story_url)
        out.append(
            CandidateSource(
                url=story_url,
                title=title[:200],
                snippet=body_clean[:500],
                discovered_via="hn_algolia_search",
                raw_payload={"query": query, "objectID": hit.get("objectID")},
            )
        )

    # Then: URLs inside the comment / story body.
    for match in _URL_RE.finditer(body_clean):
        url = match.group(0).rstrip(".,;:!?\"'")
        if url in seen or _host(url) in _NOISE_HOSTS:
            continue
        seen.add(url)
        # Local snippet around the URL.
        start = max(0, match.start() - 60)
        end = min(len(body_clean), match.end() + 60)
        out.append(
            CandidateSource(
                url=url,
                title=title[:200],
                snippet=body_clean[start:end].strip()[:500],
                discovered_via="hn_algolia_search",
                raw_payload={"query": query, "objectID": hit.get("objectID")},
            )
        )
    return out


class HNAlgoliaStrategy:
    name = "hn_algolia_search"

    async def run(self, http_client: httpx.AsyncClient) -> list[CandidateSource]:
        # Algolia accepts a unix timestamp filter: numericFilters=created_at_i>N
        since = int(time.time()) - _DAYS_BACK * 86400
        out: list[CandidateSource] = []
        for q in _QUERIES:
            url = f"{_ALGOLIA_URL}?query={quote(q)}&tags=(story,comment)&hitsPerPage={_HITS_PER_PAGE}&numericFilters=created_at_i%3E{since}"
            try:
                resp = await http_client.get(url, timeout=15.0)
                resp.raise_for_status()
                data = resp.json()
            except (httpx.HTTPError, httpx.TimeoutException, ValueError) as e:
                _log.warning("hn_algolia_fetch_failed", q=q, err=str(e))
                continue
            for hit in data.get("hits", []):
                out.extend(_hit_to_candidates(hit, query=q))
            # polite gap between queries
            _log.info("hn_algolia_query_done", q=q, total_so_far=len(out))
        return out

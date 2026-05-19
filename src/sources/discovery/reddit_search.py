"""Reddit search for 'where to find jobs / lists' threads.

Uses the unauthenticated `/r/<sub>/search.json` endpoint with restrict_sr=on.
Each matching post yields URLs from selftext + url fields.

Why unauthenticated: discovery is read-only + low volume; the existing
reddit OAuth flow in `src/sources/reddit_auth.py` is reserved for higher-
volume hot-feed crawling (r/forhire/new) where rate limits matter. One-off
search calls fit comfortably under the per-IP 60-req-per-10min anonymous
quota.
"""

from __future__ import annotations

import re
from urllib.parse import quote

import httpx

from src.common.logger import get_logger
from src.sources.discovery.base import CandidateSource

_log = get_logger(__name__)

_SUBS = ["cscareerquestions", "internships", "forhire", "remotejs"]
_QUERIES = [
    "where to find",
    "list of",
    "job boards",
    "best sites for",
]
_LIMIT = 10  # per (sub, query) — keeps total well under classifier cap

_URL_RE = re.compile(r"https?://[^\s<>\"'\)]+")
_USER_AGENT = "cartograph-discovery/0.1 (+https://github.com/cartograph)"
_NOISE_HOSTS = {
    "reddit.com",
    "www.reddit.com",
    "old.reddit.com",
    "redd.it",
    "imgur.com",
    "i.redd.it",
    "v.redd.it",
}


def _host(url: str) -> str:
    try:
        return url.split("://", 1)[1].split("/", 1)[0].lower()
    except (IndexError, ValueError):
        return ""


def _post_to_candidates(child: dict, sub: str, query: str) -> list[CandidateSource]:
    out: list[CandidateSource] = []
    seen: set[str] = set()
    post = child.get("data", {}) if isinstance(child, dict) else {}
    title = post.get("title", "") or ""
    selftext = post.get("selftext", "") or ""
    permalink = post.get("permalink", "") or ""
    body = f"{title}\n{selftext}"

    # Pull every embedded URL.
    for match in _URL_RE.finditer(body):
        url = match.group(0).rstrip(".,;:!?\"')")
        if url in seen or _host(url) in _NOISE_HOSTS:
            continue
        seen.add(url)
        start = max(0, match.start() - 60)
        end = min(len(body), match.end() + 60)
        out.append(
            CandidateSource(
                url=url,
                title=title[:200],
                snippet=body[start:end].replace("\n", " ").strip()[:500],
                discovered_via="reddit_search",
                raw_payload={"sub": sub, "query": query, "permalink": permalink},
            )
        )
    return out


class RedditSearchStrategy:
    name = "reddit_search"

    async def run(self, http_client: httpx.AsyncClient) -> list[CandidateSource]:
        out: list[CandidateSource] = []
        headers = {"User-Agent": _USER_AGENT}
        for sub in _SUBS:
            for q in _QUERIES:
                url = f"https://www.reddit.com/r/{sub}/search.json?q={quote(q)}&restrict_sr=on&sort=relevance&limit={_LIMIT}"
                try:
                    resp = await http_client.get(url, headers=headers, timeout=15.0)
                    if resp.status_code == 429:
                        _log.warning("reddit_search_rate_limited", sub=sub, q=q)
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                except (httpx.HTTPError, httpx.TimeoutException, ValueError) as e:
                    _log.warning("reddit_search_failed", sub=sub, q=q, err=str(e))
                    continue
                children = data.get("data", {}).get("children", [])
                for child in children:
                    out.extend(_post_to_candidates(child, sub=sub, query=q))
            _log.info("reddit_search_sub_done", sub=sub, total_so_far=len(out))
        return out

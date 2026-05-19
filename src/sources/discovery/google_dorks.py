"""Google dork strategy — uses DuckDuckGo HTML (no API key required).

We deliberately avoid the actual Google search HTML page (heavy CF/captcha
protection, ToS issues). DuckDuckGo's HTML endpoint accepts the same dork
syntax (`inurl:`, `intitle:`, `site:`) and is permissive for low-volume
scraping. We rate-limit one query per 3s to stay polite.

Expected yield is LOWER than the other 3 strategies because:
  - DDG re-ranks aggressively (less long-tail than Google)
  - many dorks return curated awesome-list repos (already covered by
    github_awesome strategy → dedupe drops them)

We keep it as the 4th strategy specifically for finding niche sites
GitHub awesome-lists and HN don't surface — e.g. region-specific job
boards, fellowship pages on .edu domains.
"""

from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlparse

import httpx

from src.common.logger import get_logger
from src.sources.discovery.base import CandidateSource

_log = get_logger(__name__)

_DDG_URL = "https://html.duckduckgo.com/html/"
_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# Hand-tuned dorks. Hardcoded — Phase 3.3 lifts these into config/dorks.yaml.
_DORKS = [
    "inurl:careers site:github.io",
    'intitle:"job board" india internship',
    'inurl:fellowship "applications open"',
    'intitle:"hiring" site:notion.site',
    'inurl:opportunities "remote" "full-time"',
    'intitle:"jobs" site:airtable.com',
]

# DDG HTML result rows look like: <a class="result__url" href="//duckduckgo.com/l/?uddg=ENCODED_URL...
# The /l/ wrapper hides outbound URLs; we extract the `uddg=` param.
_DDG_LINK_RE = re.compile(
    r'<a[^>]+class="result__url"[^>]+href="(?P<href>[^"]+)"',
    re.IGNORECASE,
)
_DDG_TITLE_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]*>(?P<text>[^<]+)</a>',
    re.IGNORECASE,
)
_DDG_SNIPPET_RE = re.compile(
    r'<a[^>]+class="result__snippet"[^>]*>(?P<text>.+?)</a>',
    re.IGNORECASE | re.DOTALL,
)

_NOISE_HOSTS = {
    "duckduckgo.com",
    "google.com",
    "bing.com",
    "youtube.com",
    "linkedin.com",
}


def _decode_ddg_href(href: str) -> str:
    """DDG wraps outbound links as `//duckduckgo.com/l/?uddg=<urlenc>`. Decode it."""
    if "uddg=" not in href:
        return href
    try:
        # href is `//duckduckgo.com/l/?uddg=...&rut=...`
        query = href.split("?", 1)[1]
        for piece in query.split("&"):
            if piece.startswith("uddg="):
                return unquote(piece[5:])
    except (IndexError, ValueError):
        return href
    return href


def _host(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except (ValueError, TypeError):
        return ""


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def _parse_ddg_html(html: str, query: str) -> list[CandidateSource]:
    out: list[CandidateSource] = []
    seen: set[str] = set()
    hrefs = [m.group("href") for m in _DDG_LINK_RE.finditer(html)]
    titles = [_strip_tags(m.group("text")) for m in _DDG_TITLE_RE.finditer(html)]
    snippets = [_strip_tags(m.group("text")) for m in _DDG_SNIPPET_RE.finditer(html)]
    for i, href in enumerate(hrefs):
        url = _decode_ddg_href(href)
        if not url.startswith("http"):
            continue
        if url in seen:
            continue
        host = _host(url).lower()
        if host in _NOISE_HOSTS:
            continue
        seen.add(url)
        title = titles[i] if i < len(titles) else ""
        snippet = snippets[i] if i < len(snippets) else ""
        out.append(
            CandidateSource(
                url=url,
                title=title[:200],
                snippet=snippet[:500],
                discovered_via="google_dorks",
                raw_payload={"dork": query},
            )
        )
    return out


class GoogleDorksStrategy:
    name = "google_dorks"

    async def run(self, http_client: httpx.AsyncClient) -> list[CandidateSource]:
        out: list[CandidateSource] = []
        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        }
        for dork in _DORKS:
            url = f"{_DDG_URL}?q={quote(dork)}"
            try:
                resp = await http_client.get(url, headers=headers, timeout=20.0, follow_redirects=True)
                resp.raise_for_status()
            except (httpx.HTTPError, httpx.TimeoutException) as e:
                _log.warning("google_dork_fetch_failed", dork=dork, err=str(e))
                continue
            results = _parse_ddg_html(resp.text, query=dork)
            out.extend(results)
            _log.info("google_dork_done", dork=dork, n=len(results), total=len(out))
        return out

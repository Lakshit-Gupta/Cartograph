"""GitHub `awesome-*` README mining.

Pulls a hand-picked set of public awesome-list READMEs in raw markdown form,
regex-extracts every (URL, anchor text) pair, and returns them as candidates.

These READMEs are the most reliable yield in the discovery pipeline because:
  - they're curated by humans (low false-positive rate)
  - raw.githubusercontent.com is unauthenticated + CF-free
  - one README contains 50-500 outbound URLs

Design note: we deliberately do NOT parse the markdown — regex on the raw
text is faster and the format inside an awesome-list is unstable. We let
the LLM classifier filter noise downstream.
"""

from __future__ import annotations

import re

import httpx

from src.common.logger import get_logger
from src.sources.discovery.base import CandidateSource

_log = get_logger(__name__)

# Curated seed list. Adding new READMEs is one-line config — but Phase 3.2
# keeps it small + reliable. Phase 3.3 can pull from a YAML config.
_AWESOME_READMES = [
    ("https://raw.githubusercontent.com/tramcar/awesome-job-boards/master/README.md", "awesome-job-boards"),
    ("https://raw.githubusercontent.com/bmuschko/awesome-remote-jobs/master/README.md", "awesome-remote-jobs"),
    ("https://raw.githubusercontent.com/lukasz-madon/awesome-remote-job/master/README.md", "awesome-remote-job"),
]

# Markdown link form: [text](url) — captures both groups. We also accept bare URLs.
_MD_LINK_RE = re.compile(r"\[([^\]\n]{1,200})\]\((https?://[^\s)]+)\)")
_BARE_URL_RE = re.compile(r"(?<![\(\[\"'])(https?://[^\s)\]\"'<>]+)")

# Drop URLs that point right back at GitHub repos or assets — we already crawl
# those via the github_markdown sources lane, no need to re-discover.
_NOISE_HOSTS = {
    "github.com",
    "raw.githubusercontent.com",
    "user-images.githubusercontent.com",
    "img.shields.io",
    "shields.io",
    "badge.fury.io",
    "travis-ci.org",
    "codecov.io",
}


def _host(url: str) -> str:
    """Strip scheme + path; lowercase host."""
    try:
        host = url.split("://", 1)[1].split("/", 1)[0]
        return host.lower()
    except (IndexError, ValueError):
        return ""


def _extract_candidates(text: str, source_readme: str) -> list[CandidateSource]:
    """Pull every plausible aggregator URL out of one README's raw markdown."""
    seen: set[str] = set()
    out: list[CandidateSource] = []
    for match in _MD_LINK_RE.finditer(text):
        anchor, url = match.group(1).strip(), match.group(2).strip().rstrip(".,;")
        if url in seen or _host(url) in _NOISE_HOSTS:
            continue
        seen.add(url)
        # Snippet = ±60 chars context around the link in the README, gives the
        # classifier something domain-y to chew on beyond just the URL.
        start = max(0, match.start() - 60)
        end = min(len(text), match.end() + 60)
        snippet = text[start:end].replace("\n", " ").strip()
        out.append(
            CandidateSource(
                url=url,
                title=anchor[:200],
                snippet=snippet[:500],
                discovered_via="github_awesome_lists",
                raw_payload={"source_readme": source_readme},
            )
        )
    # Pick up bare URLs too (often inside table cells without markdown link syntax)
    for match in _BARE_URL_RE.finditer(text):
        url = match.group(1).strip().rstrip(".,;")
        if url in seen or _host(url) in _NOISE_HOSTS:
            continue
        seen.add(url)
        start = max(0, match.start() - 60)
        end = min(len(text), match.end() + 60)
        snippet = text[start:end].replace("\n", " ").strip()
        out.append(
            CandidateSource(
                url=url,
                title="",
                snippet=snippet[:500],
                discovered_via="github_awesome_lists",
                raw_payload={"source_readme": source_readme},
            )
        )
    return out


class GitHubAwesomeStrategy:
    name = "github_awesome_lists"

    async def run(self, http_client: httpx.AsyncClient) -> list[CandidateSource]:
        out: list[CandidateSource] = []
        for url, label in _AWESOME_READMES:
            try:
                resp = await http_client.get(url, timeout=20.0, follow_redirects=True)
                resp.raise_for_status()
            except (httpx.HTTPError, httpx.TimeoutException) as e:
                _log.warning("github_awesome_fetch_failed", url=url, err=str(e))
                continue
            out.extend(_extract_candidates(resp.text, label))
            _log.info("github_awesome_parsed", readme=label, n_candidates=len(out))
        return out

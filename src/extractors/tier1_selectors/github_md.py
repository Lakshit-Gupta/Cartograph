"""GitHub markdown awesome-list extractor (SimplifyJobs / Ouckah / PittCSC etc.).

Strategy: ``github_md`` — pulls opportunity rows out of the markdown table
format these lists all use:

    | Company | Role | Location | Application/Link | Date Posted |
    | --- | --- | --- | --- | --- |
    | Stripe | Backend Engineer | Remote (US) | [Apply](https://...) | May 12 |

stdlib only — no markdown parser dependency.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta

from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType
from src.extractors.base import ExtractInput, ExtractOutput
from src.extractors.tier1_selectors import register


def _fp(*parts: str) -> str:
    return hashlib.sha1("|".join(p.lower() for p in parts).encode()).hexdigest()  # noqa: S324


# Strip markdown link/image syntax and bold/italic markers.
_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_BOLD_RE = re.compile(r"\*{1,3}|_{1,3}")
_HTML_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r"https?://[^\s)>\]]+")
_REL_DAYS_RE = re.compile(r"(\d+)\s*d", re.IGNORECASE)
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _strip_md(cell: str) -> str:
    """Strip images, links (keep visible text), bold/italic markers, raw HTML."""
    s = _IMG_RE.sub("", cell)
    s = _LINK_RE.sub(lambda m: m.group(1), s)
    s = _BOLD_RE.sub("", s)
    s = _HTML_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _extract_url(cell: str) -> str | None:
    # Prefer the URL inside [text](url) syntax; fall back to bare http URL.
    m = _LINK_RE.search(cell)
    if m:
        return m.group(2).strip()
    m2 = _URL_RE.search(cell)
    return m2.group(0).strip() if m2 else None


def _is_separator_row(cells: list[str]) -> bool:
    return all(re.fullmatch(r":?-{2,}:?", c.strip()) for c in cells if c.strip())


def _parse_date(text: str) -> datetime | None:
    text = text.strip()
    if not text:
        return None
    # "0d" / "3d" relative — days ago. Anchor to today UTC midnight.
    rel = _REL_DAYS_RE.fullmatch(text)
    if rel:
        days = int(rel.group(1))
        now = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return now - timedelta(days=days)
    # "May 12" or "12 May" — current year, UTC midnight.
    tokens = re.split(r"[\s,]+", text)
    month: int | None = None
    day: int | None = None
    for tok in tokens:
        key = tok.lower().strip(".")
        if key in _MONTHS:
            month = _MONTHS[key]
        elif tok.isdigit() and 1 <= int(tok) <= 31:
            day = int(tok)
    if month and day:
        year = datetime.now(UTC).year
        try:
            return datetime(year, month, day, tzinfo=UTC)
        except ValueError:
            return None
    return None


def _split_row(line: str) -> list[str]:
    # Drop the leading/trailing pipes then split. Don't use " | " — many lists
    # write cells without padding (e.g. "|Stripe|Backend|Remote|").
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [c.strip() for c in stripped.split("|")]


_ATS_DOMAINS = ("greenhouse", "lever", "ashby", "workable")


@register("github_md")
async def extract(inp: ExtractInput) -> ExtractOutput:
    opps: list[Opportunity] = []
    # State machine: NONE → HEADER_SEEN → IN_BODY. Any non-pipe line resets to NONE.
    state = "NONE"
    for raw in (inp.content or "").splitlines():
        line = raw.rstrip()
        if not line.startswith("|"):
            state = "NONE"
            continue
        cells = _split_row(line)
        if len(cells) < 4:
            continue
        if _is_separator_row(cells):
            state = "IN_BODY" if state == "HEADER_SEEN" else "NONE"
            continue
        if state == "NONE":
            # First pipe row after a gap — treat as header, await separator.
            state = "HEADER_SEEN"
            continue
        if state != "IN_BODY":
            continue

        company_raw, title_raw, location_raw, link_cell = cells[0], cells[1], cells[2], cells[3]
        date_raw = cells[4] if len(cells) > 4 else ""

        company = _strip_md(company_raw)
        title = _strip_md(title_raw)
        location = _strip_md(location_raw)
        apply_url = _extract_url(link_cell)
        if not (company and title and apply_url):
            continue

        loc_lower = location.lower()
        remote = RemoteType.REMOTE if "remote" in loc_lower else RemoteType.HYBRID if "hybrid" in loc_lower else RemoteType.ONSITE
        category = OppCategory.INTERNSHIP if "intern" in title.lower() else OppCategory.FULLTIME
        url_lower = apply_url.lower()
        apply_method = ApplyMethod.ATS_FORM if any(d in url_lower for d in _ATS_DOMAINS) else ApplyMethod.EXTERNAL
        posted = _parse_date(date_raw)

        opps.append(
            Opportunity(
                source_id=inp.source_id,
                canonical_url=apply_url,
                title=title,
                company=company,
                description=None,
                location=location or None,
                remote_type=remote,
                category=category,
                posted_at=posted,
                apply_url=apply_url,
                apply_method=apply_method,
                fingerprint_hash=_fp(company, title, location, str(posted)[:10] if posted else ""),
                extraction_tier=1,
                extraction_confidence=0.78,
            )
        )
    return ExtractOutput(opps=opps, tier_used=1, confidence=0.78 if opps else 0.0)

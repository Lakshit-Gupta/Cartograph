"""Upwork digest email → list[Opportunity].

Parses the HTML body of Upwork's "Saved search" / "Job feed" digest emails.
Each job card in those emails wraps the title in an <a href="https://www.upwork.com/jobs/..."> link,
followed by a snippet with budget, posted time, country, and category.

Resulting Opportunity rows get published directly to Streams.RANK so they skip
the fetch + extract tiers entirely (the email is already structured data).
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from email.message import Message
from typing import Any

from src.common.logger import get_logger
from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType

_log = get_logger(__name__)

_UPWORK_SOURCE_SLUG = "fl_upwork_email"
_JOB_URL_RX = re.compile(
    r"https?://(?:www\.)?upwork\.com/jobs/[A-Za-z0-9_~\-]+",
    re.IGNORECASE,
)
_BUDGET_FIXED_RX = re.compile(r"\$([\d,]+(?:\.\d+)?)\s*(?:fixed|fixed[- ]price)?", re.IGNORECASE)
_BUDGET_RANGE_RX = re.compile(r"\$([\d,]+(?:\.\d+)?)\s*[-–to]+\s*\$?([\d,]+(?:\.\d+)?)", re.IGNORECASE)
_HOURLY_RX = re.compile(r"\$([\d,]+(?:\.\d+)?)\s*(?:-\s*\$?([\d,]+(?:\.\d+)?))?\s*/\s*hr", re.IGNORECASE)
_POSTED_RX = re.compile(
    r"posted\s+(\d+)\s+(minute|hour|day|week)s?\s+ago",
    re.IGNORECASE,
)


def _fp(*parts: str) -> str:
    return hashlib.sha1("|".join(p.lower() for p in parts).encode()).hexdigest()  # noqa: S324


def _decode_payload(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, AttributeError):
        return payload.decode("utf-8", errors="replace")


def _extract_html(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return _decode_payload(part)
        return ""
    if (msg.get_content_type() or "").lower() == "text/html":
        return _decode_payload(msg)
    return ""


def _canonicalise(url: str) -> str:
    # Drop trailing slashes and tracking params.
    url = url.split("?", 1)[0].split("#", 1)[0]
    return url.rstrip("/")


def _parse_posted_at(text: str) -> datetime | None:
    m = _POSTED_RX.search(text)
    if not m:
        return None
    qty = int(m.group(1))
    unit = m.group(2).lower()
    now = datetime.now(UTC)
    delta = {
        "minute": timedelta(minutes=qty),
        "hour": timedelta(hours=qty),
        "day": timedelta(days=qty),
        "week": timedelta(weeks=qty),
    }.get(unit)
    return now - delta if delta else None


def _parse_budget(text: str) -> dict[str, Any]:
    """Returns dict with comp_min, comp_max, comp_currency, comp_period."""
    hourly = _HOURLY_RX.search(text)
    if hourly:
        lo = float(hourly.group(1).replace(",", ""))
        hi = float(hourly.group(2).replace(",", "")) if hourly.group(2) else lo
        return {"comp_min": lo, "comp_max": hi, "comp_currency": "USD", "comp_period": "hour"}
    rng = _BUDGET_RANGE_RX.search(text)
    if rng:
        lo = float(rng.group(1).replace(",", ""))
        hi = float(rng.group(2).replace(",", ""))
        return {"comp_min": lo, "comp_max": hi, "comp_currency": "USD", "comp_period": None}
    fixed = _BUDGET_FIXED_RX.search(text)
    if fixed:
        v = float(fixed.group(1).replace(",", ""))
        return {"comp_min": v, "comp_max": v, "comp_currency": "USD", "comp_period": None}
    return {"comp_min": None, "comp_max": None, "comp_currency": None, "comp_period": None}


def _node_text(node: Any) -> str:
    try:
        return (node.text(separator=" ") or "").strip()
    except Exception:
        return ""


def parse_upwork_digest(msg: Message, *, source_id: int | None = None) -> list[Opportunity]:
    """Return Opportunity rows extracted from one Upwork digest email.

    `source_id` defaults to None — the caller (worker entrypoint) is expected
    to resolve the id from the `fl_upwork_email` slug once at startup and
    pass it in. If None, returns [] (we won't fabricate FKs).
    """
    if source_id is None:
        _log.warning("upwork_parser_no_source_id")
        return []

    html = _extract_html(msg)
    if not html:
        _log.info("upwork_parser_no_html")
        return []

    try:
        from selectolax.parser import HTMLParser  # lazy

        tree = HTMLParser(html)
    except Exception as e:
        _log.warning("upwork_parser_html_failed", err=str(e))
        return []

    seen_urls: set[str] = set()
    opps: list[Opportunity] = []

    # Each job card has an <a href="https://www.upwork.com/jobs/..."> with the title.
    # The surrounding block (parent table row / div) holds budget + posted time text.
    for link in tree.css('a[href*="upwork.com/jobs/"]'):
        try:
            href = (link.attributes.get("href") or "").strip()
            if not href or not _JOB_URL_RX.search(href):
                continue
            url = _canonicalise(href)
            if url in seen_urls:
                continue
            title = _node_text(link)
            if not title or len(title) < 4:
                continue
            seen_urls.add(url)

            # Climb to a useful surrounding block for budget/posted text.
            container = link.parent
            for _ in range(4):
                if container is None or container.parent is None:
                    break
                container = container.parent
            surrounding = _node_text(container) if container is not None else ""

            budget = _parse_budget(surrounding)
            posted = _parse_posted_at(surrounding)

            opps.append(
                Opportunity(
                    source_id=source_id,
                    canonical_url=url,
                    title=title[:200],
                    company=None,  # not exposed in digest
                    description=surrounding[:1200] or None,
                    comp_min=budget["comp_min"],
                    comp_max=budget["comp_max"],
                    comp_currency=budget["comp_currency"],
                    comp_period=budget["comp_period"],
                    location=None,
                    remote_type=RemoteType.REMOTE,
                    category=OppCategory.FREELANCE,
                    posted_at=posted,
                    apply_url=url,
                    apply_method=ApplyMethod.IN_PLATFORM,
                    fingerprint_hash=_fp(
                        "upwork",
                        title[:80],
                        str(budget["comp_min"] or ""),
                        str(posted)[:10] if posted else "",
                    ),
                    extraction_tier=0,
                    extraction_confidence=0.85,
                )
            )
        except Exception as e:
            _log.warning("upwork_parser_card_failed", err=str(e))
            continue

    if not opps:
        _log.info("upwork_parser_empty", subject=(msg.get("Subject") or "")[:120])
    return opps

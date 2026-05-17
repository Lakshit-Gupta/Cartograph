"""Internshala HTML scraping selectors."""
from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

from selectolax.parser import HTMLParser

from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType
from src.extractors.base import ExtractInput, ExtractOutput
from src.extractors.tier1_selectors import register


def _fp(*parts: str) -> str:
    return hashlib.sha1("|".join(p.lower() for p in parts).encode()).hexdigest()  # noqa: S324


_STIPEND_RE = re.compile(r"(\d[\d,]+)\s*(?:-\s*(\d[\d,]+))?\s*/\s*(month|year|hour)", re.IGNORECASE)


@register("india_internshala")
async def extract(inp: ExtractInput) -> ExtractOutput:
    tree = HTMLParser(inp.content)
    opps: list[Opportunity] = []
    cards = tree.css("div.individual_internship") or tree.css("div.container-fluid.individual_internship")
    for card in cards:
        title_node = card.css_first(".heading_4_5.profile, .job-internship-name") or card.css_first("a.view_detail_button")
        title = (title_node.text(strip=True) if title_node else "")
        if not title:
            continue
        company_node = card.css_first(".company_and_premium .company-name, p.company a")
        company = company_node.text(strip=True) if company_node else None
        loc_node = card.css_first(".locations span a, .location_link")
        location = loc_node.text(strip=True) if loc_node else None
        stipend_node = card.css_first(".stipend, .stipend_container_table_cell")
        stipend_text = stipend_node.text(strip=True) if stipend_node else ""
        comp_min = comp_max = None
        comp_period = None
        m = _STIPEND_RE.search(stipend_text)
        if m:
            try:
                comp_min = float(m.group(1).replace(",", ""))
                if m.group(2):
                    comp_max = float(m.group(2).replace(",", ""))
                comp_period = m.group(3).lower()
            except ValueError:
                pass
        link_node = card.css_first("a.view_detail_button") or card.css_first("a")
        href = link_node.attributes.get("href", "") if link_node else ""
        absolute = href if href.startswith("http") else f"https://internshala.com{href}"
        is_remote = "work from home" in (card.text() or "").lower()

        opps.append(Opportunity(
            source_id=inp.source_id,
            canonical_url=absolute or inp.url,
            title=title,
            company=company,
            description=stipend_text[:600],
            location=location,
            remote_type=RemoteType.REMOTE if is_remote else RemoteType.ONSITE,
            category=OppCategory.INTERNSHIP,
            posted_at=datetime.now(UTC),
            apply_url=absolute,
            apply_method=ApplyMethod.IN_PLATFORM,
            comp_min=comp_min,
            comp_max=comp_max,
            comp_currency="INR",
            comp_period=comp_period,
            fingerprint_hash=_fp(company or "", title, location or "", ""),
            extraction_tier=1,
            extraction_confidence=0.78,
        ))
    return ExtractOutput(opps=opps, tier_used=1, confidence=0.78 if opps else 0.0)

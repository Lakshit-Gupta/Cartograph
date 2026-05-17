"""Tier-0 regex extractor.

Picks up embedded form posts, mailto: apply targets, ATS-embedded iframes —
anything where a single page contains exactly one opportunity with strong markers.
"""
from __future__ import annotations

import hashlib
import re

from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType
from src.extractors.base import ExtractInput, Extractor, ExtractOutput

_TITLE_RE = re.compile(r"<title>(.+?)</title>", re.IGNORECASE | re.DOTALL)
_MAILTO_RE = re.compile(r'mailto:([\w.+-]+@[\w.-]+\.[a-z]{2,})', re.IGNORECASE)
_ATS_IFRAME_RE = re.compile(
    r'<iframe[^>]+src=["\'](https?://(?:boards|jobs|apply)\.[^"\']+)["\']',
    re.IGNORECASE,
)
_COMP_RE = re.compile(
    r"(?P<cur>\$|₹|€|£|INR|USD|EUR|GBP)\s*"
    r"(?P<lo>\d{1,3}(?:[, ]\d{3})*|\d+)\s*"
    r"(?:[-–to]\s*(?P<hi>\d{1,3}(?:[, ]\d{3})*|\d+))?\s*"
    r"(?:/\s*(?P<per>hour|hr|month|mo|year|yr|annum))?",
    re.IGNORECASE,
)
_CATEGORY_HINTS = {
    OppCategory.INTERNSHIP: ("internship", "intern"),
    OppCategory.FELLOWSHIP: ("fellowship", "fellow", "residency", "scholar"),
    OppCategory.FREELANCE: ("freelance", "contract", "1099"),
    OppCategory.FULLTIME: ("full-time", "fulltime", "permanent", "senior", "engineer"),
}
_REMOTE_HINTS = {
    RemoteType.REMOTE: ("remote", "anywhere", "work from home", "wfh"),
    RemoteType.HYBRID: ("hybrid",),
    RemoteType.ONSITE: ("onsite", "in office", "in-person"),
}


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s)


def fingerprint(company: str | None, title: str, location: str | None, posted_at: str | None) -> str:
    bucket = (posted_at or "")[:10]
    raw = f"{(company or '').lower()}|{title.lower()}|{(location or '').lower()}|{bucket}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()  # noqa: S324


class Tier0Regex(Extractor):
    tier = 0

    async def extract(self, inp: ExtractInput) -> ExtractOutput:
        body = inp.content or ""
        title_m = _TITLE_RE.search(body)
        title = (title_m.group(1).strip() if title_m else "").splitlines()[0][:200]
        if not title:
            return ExtractOutput(opps=[], tier_used=self.tier, confidence=0.0)

        plain = _strip_html(body).lower()

        # Category
        category = OppCategory.UNKNOWN
        for cat, hints in _CATEGORY_HINTS.items():
            if any(h in plain for h in hints):
                category = cat
                break

        # Remote
        remote = RemoteType.UNSPECIFIED
        for rt, hints in _REMOTE_HINTS.items():
            if any(h in plain for h in hints):
                remote = rt
                break

        # Apply target
        apply_url: str | None = None
        apply_method: ApplyMethod | None = None
        mailto_m = _MAILTO_RE.search(body)
        if mailto_m:
            apply_url = f"mailto:{mailto_m.group(1)}"
            apply_method = ApplyMethod.EMAIL
        else:
            iframe_m = _ATS_IFRAME_RE.search(body)
            if iframe_m:
                apply_url = iframe_m.group(1)
                apply_method = ApplyMethod.EMBEDDED_FORM
            elif "<form" in body.lower():
                apply_method = ApplyMethod.EMBEDDED_FORM

        # Comp
        comp_min: float | None = None
        comp_max: float | None = None
        comp_currency: str | None = None
        comp_period: str | None = None
        comp_m = _COMP_RE.search(body)
        if comp_m:
            try:
                comp_min = float(comp_m.group("lo").replace(",", "").replace(" ", ""))
                if comp_m.group("hi"):
                    comp_max = float(comp_m.group("hi").replace(",", "").replace(" ", ""))
                cur = comp_m.group("cur").upper()
                comp_currency = {"$": "USD", "₹": "INR", "€": "EUR", "£": "GBP"}.get(cur, cur)
                per = (comp_m.group("per") or "").lower()
                comp_period = (
                    "hour" if per in ("hour", "hr") else
                    "month" if per in ("month", "mo") else
                    "year" if per in ("year", "yr", "annum") else None
                )
            except (ValueError, AttributeError):
                pass

        company = None
        company_m = re.search(r"company[^a-z]{0,4}([A-Z][\w &.,'-]{1,80})", body)
        if company_m:
            company = company_m.group(1).strip()

        opp = Opportunity(
            source_id=inp.source_id,
            canonical_url=inp.url,
            title=title,
            company=company,
            description=_strip_html(body)[:1200],
            remote_type=remote,
            category=category,
            apply_url=apply_url or inp.url,
            apply_method=apply_method,
            comp_min=comp_min,
            comp_max=comp_max,
            comp_currency=comp_currency,
            comp_period=comp_period,
            fingerprint_hash=fingerprint(company, title, None, None),
            extraction_tier=self.tier,
            extraction_confidence=0.55 if apply_method else 0.35,
        )
        return ExtractOutput(opps=[opp], tier_used=self.tier, confidence=opp.extraction_confidence)

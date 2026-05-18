"""Tier-2 LLM extractor — last resort, sandboxed.

- No tool use.
- Untrusted page content fenced inside <CONTENT> tags; if the page itself
  contained nested HTML that looks like an instruction, it is double-fenced
  inside <IGNORE> by the caller (in extractors/base for raw input).
- JSON-schema validated output.
- Cost-gated via src.common.llm.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from src.common.llm import chat_json, fence_untrusted, load_prompt
from src.common.logger import get_logger
from src.common.metrics import extract_tier_distribution
from src.common.types import ApplyMethod, OppCategory, Opportunity, RemoteType
from src.extractors.base import ExtractInput, ExtractOutput

_log = get_logger(__name__)


def _fp(*parts: str) -> str:
    return hashlib.sha1("|".join(p.lower() for p in parts).encode()).hexdigest()  # noqa: S324


def _coerce_iso(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class Tier2LLM:
    tier = 2

    def __init__(self) -> None:
        self._prompt = load_prompt("tier2_extractor.txt")

    async def extract(self, inp: ExtractInput) -> ExtractOutput:
        # Truncate to keep prompt small & cheap
        content_snip = (inp.content or "")[:12_000]
        messages = [
            {"role": "system", "content": self._prompt},
            {
                "role": "user",
                "content": (
                    f"<URL>{inp.url}</URL>\n<SOURCE>{inp.source_slug}</SOURCE>\n<CONTENT>\n{fence_untrusted(content_snip)}\n</CONTENT>\n"
                ),
            },
        ]
        try:
            obj = await chat_json(
                messages=messages,
                kind="llm_extract",
                temperature=0.0,
                max_tokens=900,
            )
        except Exception as e:
            _log.warning("tier2_llm_failed", url=inp.url, err=str(e))
            return ExtractOutput(opps=[], tier_used=self.tier, confidence=0.0)

        title = (obj.get("title") or "").strip()
        if not title:
            return ExtractOutput(opps=[], tier_used=self.tier, confidence=0.0)

        try:
            opp = Opportunity(
                source_id=inp.source_id,
                canonical_url=inp.url,
                title=title[:200],
                company=obj.get("company"),
                description=(obj.get("description") or "")[:1200],
                comp_min=obj.get("comp_min"),
                comp_max=obj.get("comp_max"),
                comp_currency=obj.get("comp_currency"),
                comp_period=obj.get("comp_period"),
                location=obj.get("location"),
                remote_type=RemoteType(obj.get("remote_type", "unspecified")),
                category=OppCategory(obj.get("category", "unknown")),
                posted_at=_coerce_iso(obj.get("posted_at")),
                expires_at=_coerce_iso(obj.get("expires_at")),
                apply_url=obj.get("apply_url") or inp.url,
                apply_method=ApplyMethod(obj["apply_method"]) if obj.get("apply_method") else None,
                fingerprint_hash=_fp(obj.get("company") or "", title, obj.get("location") or "", ""),
                extraction_tier=self.tier,
                extraction_confidence=float(obj.get("confidence", 0.7)),
            )
        except Exception as e:
            _log.warning("tier2_validation_failed", err=str(e))
            return ExtractOutput(opps=[], tier_used=self.tier, confidence=0.0)

        extract_tier_distribution.labels(source=inp.source_slug, tier=str(self.tier)).inc()
        return ExtractOutput(opps=[opp], tier_used=self.tier, confidence=opp.extraction_confidence)

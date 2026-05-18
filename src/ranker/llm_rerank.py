"""LLM re-rank — top-K candidates through OpenRouter for final ordering."""

from __future__ import annotations

import json
from typing import Any

from src.common.llm import chat_json, fence_untrusted, load_prompt
from src.common.logger import get_logger
from src.common.types import Opportunity

_log = get_logger(__name__)


async def rerank(
    *,
    profile_summary: str,
    candidates: list[tuple[Opportunity, float]],
    top_k: int = 30,
) -> list[tuple[Opportunity, float, str]]:
    """Returns [(opp, final_score, reason)] sorted best-first."""
    if not candidates:
        return []
    head = candidates[:top_k]
    tail = candidates[top_k:]

    prompt = load_prompt("re_ranker.txt")
    items: list[dict[str, Any]] = []
    for opp, base in head:
        items.append(
            {
                "id": str(opp.id) if opp.id else opp.canonical_url,
                "title": opp.title,
                "company": opp.company,
                "description": fence_untrusted((opp.description or "")[:400]),
                "remote_type": opp.remote_type.value,
                "location": opp.location,
                "comp_min": opp.comp_min,
                "comp_max": opp.comp_max,
                "comp_currency": opp.comp_currency,
                "category": opp.category.value,
                "source_quality": 1.0,
                "base_score": float(base),
            }
        )

    msgs = [
        {"role": "system", "content": prompt.format(profile_summary=profile_summary)},
        {"role": "user", "content": f"<CANDIDATES>\n{json.dumps(items)}\n</CANDIDATES>"},
    ]
    try:
        # V4 Flash + xhigh = max reasoning. Ignored by non-reasoning models.
        resp = await chat_json(
            messages=msgs,
            kind="llm_rerank",
            temperature=0.0,
            max_tokens=2000,
            reasoning_effort="xhigh",
        )
    except Exception as e:
        _log.warning("rerank_failed", err=str(e))
        # Fall back to base order
        return [(opp, base, "rerank skipped") for opp, base in candidates]

    if not isinstance(resp, list):
        return [(opp, base, "rerank malformed") for opp, base in candidates]

    by_id: dict[str, tuple[Opportunity, float]] = {(str(opp.id) if opp.id else opp.canonical_url): (opp, b) for opp, b in head}
    out: list[tuple[Opportunity, float, str]] = []
    seen: set[str] = set()
    for item in resp:
        oid = str(item.get("id") or "")
        if oid not in by_id or oid in seen:
            continue
        seen.add(oid)
        opp, _base = by_id[oid]
        final = float(item.get("final_score", 0.0))
        reason = str(item.get("reason") or "")[:180]
        out.append((opp, final, reason))

    # Append anyone the LLM ignored at original base score
    for oid, (opp, b) in by_id.items():
        if oid not in seen:
            out.append((opp, b, "not selected by reranker"))

    # Append tail untouched
    out.extend((opp, b, "below rerank threshold") for opp, b in tail)
    return out

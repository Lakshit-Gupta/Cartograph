"""Block selector — rank parsed Blocks against an opportunity.

Keyword-vote scoring: each block earns one point per opp keyword that
appears in the block's title + bullets (lower-cased). Optional
``variant_keywords`` (e.g. the resume_variants[*].lean_keywords list)
augment the keyword set.

This is intentionally cheap — it runs before the LLM tailor step which
operates only on the top-3 blocks. Anything more sophisticated belongs in
the ranker subsystem, not here.
"""
from __future__ import annotations

import re
from typing import Any

from src.application.resume_latex.parser.blocks import Block

_TOKEN = re.compile(r"[a-z][a-z0-9_+#.\-]{2,}")


def _extract_keywords(text: str) -> list[str]:
    if not text:
        return []
    return _TOKEN.findall(text.lower())


def _opp_field(opp: Any, name: str) -> Any:
    if isinstance(opp, dict):
        return opp.get(name)
    return getattr(opp, name, None)


def rank(
    blocks: list[Block],
    opp: Any,
    variant_keywords: list[str] | None = None,
) -> list[Block]:
    """Return ``blocks`` sorted by keyword-vote, highest first.

    Args:
        blocks: list from ``parser.blocks.parse(...).blocks``.
        opp: Opportunity model OR a dict with at least ``title`` and
            ``description`` keys.
        variant_keywords: optional list of additional keywords (lower-cased)
            to weigh in — e.g. the user's resume variant lean_keywords.

    Stable order is preserved for blocks that tie on score (Python sort is
    stable). Blocks with no signal at all keep their parse order.
    """
    title = str(_opp_field(opp, "title") or "")
    description = str(_opp_field(opp, "description") or "")
    keywords = set(_extract_keywords(title) + _extract_keywords(description))
    if variant_keywords:
        keywords |= {k.lower() for k in variant_keywords if k}

    if not keywords:
        return list(blocks)

    def score(b: Block) -> int:
        text = " ".join([b.title, *b.bullets]).lower()
        return sum(1 for k in keywords if k in text)

    return sorted(blocks, key=score, reverse=True)

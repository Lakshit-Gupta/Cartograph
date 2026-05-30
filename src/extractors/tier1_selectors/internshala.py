"""Internshala HTML scraping selectors.

Thin tier-1 adapter over the shared card parser. The per-card title / company /
stipend / link logic — and the stipend regex it used to carry inline — now live
in `src.sources.india.internshala_card_parser`, the single source of truth that
the browser-discovery worker also consumes. This module's only remaining job is
to split a full listing page into individual `div.individual_internship` cards
and hand each to `parse_card`. The `@register("india_internshala")` decorator
and the `ExtractInput` / `ExtractOutput` contract are unchanged.
"""

from __future__ import annotations

from selectolax.parser import HTMLParser

from src.common.types import Opportunity
from src.extractors.base import ExtractInput, ExtractOutput
from src.extractors.tier1_selectors import register
from src.sources.india.internshala_card_parser import DEFAULT_CARD_SELECTORS, parse_card

_CONFIDENCE = 0.78


@register("india_internshala")
async def extract(inp: ExtractInput) -> ExtractOutput:
    tree = HTMLParser(inp.content)
    cards = tree.css("div.individual_internship") or tree.css("div.container-fluid.individual_internship")
    opps: list[Opportunity] = []
    for card in cards:
        opp = parse_card(card.html or "", source_id=inp.source_id, selectors=DEFAULT_CARD_SELECTORS)
        if opp is not None:
            opps.append(opp)
    return ExtractOutput(opps=opps, tier_used=1, confidence=_CONFIDENCE if opps else 0.0)

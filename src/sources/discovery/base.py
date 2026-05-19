"""DiscoveryStrategy Protocol + CandidateSource value type.

Each strategy is a small async object that takes a shared httpx client and
returns a list of CandidateSource. The pipeline (pipeline.py) is responsible
for dedupe, LLM classification, and promotion — strategies stay dumb.

Why httpx (not curl_cffi): the discovery worker hits public APIs (HN Algolia,
Reddit JSON, raw GitHub README) that don't gate on TLS fingerprints. Keeping
httpx avoids pulling the curl_cffi browser fingerprint stack into this lane.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


@dataclass(slots=True)
class CandidateSource:
    """A single URL surfaced by a discovery strategy.

    The classifier later annotates this with `classifier_confidence` /
    `classifier_category` / `classifier_rationale`. Until then those stay None.

    Fields chosen so a CandidateSource can be inserted as-is into
    `candidate_sources` (the migration column list matches 1:1 except for the
    DB-generated id + status).
    """

    url: str
    title: str
    snippet: str
    discovered_via: str
    raw_payload: dict[str, Any] = field(default_factory=dict)
    # Classifier output — populated by classifier.classify() before insert.
    classifier_confidence: float | None = None
    classifier_category: str | None = None
    classifier_rationale: str | None = None


class DiscoveryStrategy(Protocol):
    """Each strategy file under `src/sources/discovery/` defines one of these.

    Implementations MUST be safe to call repeatedly — the pipeline dedupes on
    URL afterwards but a strategy returning duplicates wastes LLM budget.
    """

    name: str

    async def run(self, http_client: httpx.AsyncClient) -> list[CandidateSource]: ...

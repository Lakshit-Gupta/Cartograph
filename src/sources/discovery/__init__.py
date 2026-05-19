"""Phase 3.2 — Dark-source discovery subpackage.

Weekly cron-driven worker that mines 4 strategies for candidate aggregator URLs,
classifies them via LLM, and either auto-promotes (confidence > 0.85) into the
`sources` table or parks them in `candidate_sources` for /review.

Public surface:
    - DiscoveryStrategy Protocol  (base.py)
    - CandidateSource dataclass    (base.py)
    - run_discovery_pipeline       (pipeline.py)

Strategy modules are eagerly importable but lazy-instantiated by the worker.
"""

from src.sources.discovery.base import CandidateSource, DiscoveryStrategy

__all__ = ["CandidateSource", "DiscoveryStrategy"]

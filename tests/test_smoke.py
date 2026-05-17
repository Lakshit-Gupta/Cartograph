"""Smoke tests — verify pure-Python modules import without infra."""
from __future__ import annotations


def test_imports_common_types() -> None:
    from src.common import types
    assert types.OppState.NEW.value == "new"


def test_imports_extractor_dedup() -> None:
    from src.extractors.dedup import canonicalize_url, fp_components

    url = "https://example.com/jobs/42?utm_source=x&id=ok"
    canon = canonicalize_url(url)
    assert "utm_source" not in canon
    assert "id=ok" in canon
    fp1 = fp_components(company="X", title="Eng", location="Remote", posted_iso="2025-05-14", lane="fulltime")
    fp2 = fp_components(company="X", title="Eng", location="Remote", posted_iso="2025-05-14", lane="fulltime")
    assert fp1 == fp2
    fp3 = fp_components(company="Y", title="Eng", location="Remote", posted_iso="2025-05-14", lane="fulltime")
    assert fp1 != fp3


def test_imports_ranker_formula() -> None:
    from src.common.types import OppCategory, Opportunity, RemoteType
    from src.ranker.formula import RankerWeights, score

    opp = Opportunity(
        source_id=1,
        canonical_url="https://x",
        title="Senior Python Engineer",
        company="Acme",
        description="Build distributed systems in Python and Postgres.",
        comp_min=120000, comp_max=150000, comp_currency="USD", comp_period="year",
        remote_type=RemoteType.REMOTE,
        category=OppCategory.FULLTIME,
        fingerprint_hash="abc",
    )
    out = score(
        opp,
        profile_keywords={"python", "postgres", "docker"},
        embedding_sim=0.7,
        source_quality=1.0,
        response_rate=0.05,
        comp_floors={"fulltime": 70000},
        weights=RankerWeights(),
    )
    assert 0.0 <= out.score <= 1.0
    assert "embedding_sim" in out.components

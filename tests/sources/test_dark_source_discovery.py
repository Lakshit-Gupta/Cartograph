"""Tests for Phase 3.2 — dark-source discovery pipeline.

Coverage:
  - classifier output → high-confidence rows auto-promote (smoke)
  - classifier output → mid-confidence rows write candidate_sources rows
  - classifier output → low-confidence rows are dropped
  - dedupe against existing sources / candidate_sources skips classification
  - LLM daily cap is enforced — overflow candidates are skipped, not classified

All DB calls are stubbed via monkeypatch — no live Postgres. The LLM is
mocked by patching `chat_json` to return canned JSON. HTTP fetches are
mocked via respx (used inside the strategy unit tests).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from src.sources.discovery import classifier, pipeline, promoter
from src.sources.discovery.base import CandidateSource

# ---------------------------------------------------------------------------
# In-memory fake DB. The pipeline + promoter touch a small set of SQL queries;
# we stub each one. Anything not stubbed raises NotImplementedError so a test
# that accidentally tries to hit live PG fails loudly.
# ---------------------------------------------------------------------------


class FakeDB:
    def __init__(self) -> None:
        self.sources: list[dict] = []  # rows in sources
        self.candidates: list[dict] = []  # rows in candidate_sources
        self.provenance: list[dict] = []  # rows in source_provenance
        self.strategy_rows: list[dict] = []
        self._next_source_id = 100
        self._next_candidate_id = 1000

    def install(self, monkeypatch) -> None:
        """Monkeypatch every db.* function the pipeline touches."""
        from src.common import db as db_module
        from src.sources.discovery import pipeline as pipeline_module
        from src.sources.discovery import promoter as promoter_module

        async def fake_fetch_one(query, *args):
            return await self._fetch_one(query, *args)

        async def fake_fetch_all(query, *args):
            return await self._fetch_all(query, *args)

        async def fake_execute(query, *args):
            return await self._execute(query, *args)

        # Patch on all the modules that pulled `from src.common import db`.
        for mod in (db_module, pipeline_module, promoter_module):
            monkeypatch.setattr(mod.db if hasattr(mod, "db") else mod, "fetch_one", fake_fetch_one, raising=False)
            monkeypatch.setattr(mod.db if hasattr(mod, "db") else mod, "fetch_all", fake_fetch_all, raising=False)
            monkeypatch.setattr(mod.db if hasattr(mod, "db") else mod, "execute", fake_execute, raising=False)

        # The promoter uses db.acquire().transaction() for the auto-promote
        # multi-statement insert. Provide a fake context manager that hands
        # the test a connection-like object with fetchrow/execute methods.
        import contextlib

        @contextlib.asynccontextmanager
        async def fake_acquire():
            yield _FakeConn(self)

        monkeypatch.setattr(db_module, "acquire", fake_acquire)
        monkeypatch.setattr(promoter_module.db, "acquire", fake_acquire, raising=False)

    async def _fetch_one(self, query: str, *args):
        q = " ".join(query.split())
        if "SELECT 1 FROM sources WHERE base_url = $1" in q:
            url = args[0]
            for s in self.sources:
                if s.get("base_url") == url:
                    return {"?column?": 1}
            for c in self.candidates:
                if c.get("url") == url:
                    return {"?column?": 1}
            return None
        raise NotImplementedError(f"fetch_one unhandled: {q[:100]}")

    async def _fetch_all(self, query: str, *args):
        q = " ".join(query.split())
        if "FROM discovery_strategies WHERE active IS TRUE" in q:
            if not self.strategy_rows:
                # All four active by default.
                return [
                    {"name": "github_awesome_lists"},
                    {"name": "hn_algolia_search"},
                    {"name": "reddit_search"},
                    {"name": "google_dorks"},
                ]
            return self.strategy_rows
        if "FROM candidate_sources" in q and "status = 'pending'" in q:
            pending = [c for c in self.candidates if c["status"] == "pending"]
            return pending
        raise NotImplementedError(f"fetch_all unhandled: {q[:100]}")

    async def _execute(self, query: str, *args):
        q = " ".join(query.split())
        if "INSERT INTO candidate_sources" in q and "'pending'" in q:
            url = args[0]
            for c in self.candidates:
                if c["url"] == url:
                    return "INSERT 0 0"
            self.candidates.append(
                {
                    "id": self._next_id_candidate(),
                    "url": url,
                    "title": args[1],
                    "snippet": args[2],
                    "discovered_via": args[3],
                    "classifier_confidence": args[4],
                    "classifier_category": args[5],
                    "classifier_rationale": args[6],
                    "status": "pending",
                }
            )
            return "INSERT 0 1"
        if "UPDATE discovery_strategies" in q:
            return "UPDATE 1"
        raise NotImplementedError(f"execute unhandled: {q[:100]}")

    def _next_id_source(self) -> int:
        self._next_source_id += 1
        return self._next_source_id

    def _next_id_candidate(self) -> int:
        self._next_candidate_id += 1
        return self._next_candidate_id


class _FakeConn:
    """A connection-shaped object exposed by the FakeDB's `acquire()`."""

    def __init__(self, store: FakeDB) -> None:
        self._store = store

    def transaction(self):
        return _FakeTxn()

    async def fetchrow(self, query: str, *args):
        q = " ".join(query.split())
        if "INSERT INTO sources" in q and "RETURNING id" in q:
            slug = args[0]
            for s in self._store.sources:
                if s["slug"] == slug:
                    return None  # ON CONFLICT DO NOTHING
            new_id = self._store._next_id_source()
            self._store.sources.append(
                {
                    "id": new_id,
                    "slug": slug,
                    "name": args[1],
                    "category": args[2],
                    "base_url": args[3],
                }
            )
            return {"id": new_id}
        if "INSERT INTO candidate_sources" in q and "'auto_promoted'" in q and "RETURNING id" in q:
            url = args[0]
            for c in self._store.candidates:
                if c["url"] == url:
                    c["status"] = "auto_promoted"
                    return {"id": c["id"]}
            cid = self._store._next_id_candidate()
            self._store.candidates.append(
                {
                    "id": cid,
                    "url": url,
                    "title": args[1],
                    "snippet": args[2],
                    "discovered_via": args[3],
                    "classifier_confidence": args[4],
                    "classifier_category": args[5],
                    "classifier_rationale": args[6],
                    "status": "auto_promoted",
                    "promoted_source_id": args[7],
                }
            )
            return {"id": cid}
        raise NotImplementedError(f"fetchrow unhandled: {q[:100]}")

    async def execute(self, query: str, *args):
        q = " ".join(query.split())
        if "INSERT INTO source_provenance" in q:
            self._store.provenance.append(
                {
                    "source_id": args[0],
                    "candidate_source_id": args[1],
                    "discovered_via": args[2],
                }
            )
            return "INSERT 0 1"
        raise NotImplementedError(f"conn.execute unhandled: {q[:100]}")


class _FakeTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


# ---------------------------------------------------------------------------
# Helper to mock the LLM classifier.
# ---------------------------------------------------------------------------


def _mock_classifier(
    monkeypatch, *, mapping: dict[str, dict[str, Any]] | None = None, default: dict[str, Any] | None = None
) -> dict[str, int]:
    """Patch classifier.classify so it returns canned output per URL.

    Returns a dict {"calls": int} the caller can read.
    """
    counter = {"calls": 0}

    async def fake_classify(candidate: CandidateSource) -> dict[str, Any] | None:
        counter["calls"] += 1
        if mapping and candidate.url in mapping:
            return mapping[candidate.url]
        return default

    monkeypatch.setattr(classifier, "classify", fake_classify)
    return counter


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_classifier_high_confidence_auto_promotes(monkeypatch):
    """Confidence > 0.85 → row lands in sources via auto_promote path."""
    fdb = FakeDB()
    fdb.install(monkeypatch)
    _mock_classifier(
        monkeypatch,
        default={
            "is_aggregator": True,
            "category": "freelance",
            "confidence": 0.95,
            "rationale": "Clearly a freelance board",
        },
    )

    candidates = [
        CandidateSource(url="https://example.com/jobs", title="Jobs", snippet="A jobs board", discovered_via="github_awesome_lists"),
    ]
    # Run classify+promote step manually (don't fetch real HTTP).
    result = await classifier.classify(candidates[0])
    assert result is not None
    classifier.apply_to_candidate(candidates[0], result)
    stats = await promoter.promote_candidates(candidates)

    assert stats.auto_promoted == 1
    assert stats.pending == 0
    assert stats.discarded == 0
    assert len(fdb.sources) == 1
    assert fdb.sources[0]["base_url"] == "https://example.com/jobs"
    assert fdb.sources[0]["category"] == "freelance"
    assert len(fdb.provenance) == 1
    assert fdb.provenance[0]["source_id"] == fdb.sources[0]["id"]


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_classifier_mid_confidence_writes_candidate_row(monkeypatch):
    """Confidence 0.5 .. 0.85 → row in candidate_sources, status='pending'."""
    fdb = FakeDB()
    fdb.install(monkeypatch)
    _mock_classifier(
        monkeypatch,
        default={
            "is_aggregator": True,
            "category": "fellowship",
            "confidence": 0.7,
            "rationale": "Looks fellow-ish",
        },
    )

    c = CandidateSource(url="https://maybe.example.com/list", title="Fellowships", snippet="Curated", discovered_via="reddit_search")
    result = await classifier.classify(c)
    assert result is not None
    classifier.apply_to_candidate(c, result)
    stats = await promoter.promote_candidates([c])

    assert stats.auto_promoted == 0
    assert stats.pending == 1
    assert stats.discarded == 0
    assert len(fdb.sources) == 0
    assert len(fdb.candidates) == 1
    assert fdb.candidates[0]["status"] == "pending"
    assert fdb.candidates[0]["classifier_confidence"] == pytest.approx(0.7)


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_classifier_low_confidence_drops(monkeypatch):
    """Confidence < 0.5 → dropped, no DB write at all."""
    fdb = FakeDB()
    fdb.install(monkeypatch)
    _mock_classifier(
        monkeypatch,
        default={
            "is_aggregator": False,
            "category": "other",
            "confidence": 0.2,
            "rationale": "Not a board",
        },
    )

    c = CandidateSource(url="https://blog.example.com/post", title="A blog", snippet="someone's post", discovered_via="hn_algolia_search")
    result = await classifier.classify(c)
    assert result is not None
    classifier.apply_to_candidate(c, result)
    stats = await promoter.promote_candidates([c])

    assert stats.discarded == 1
    assert stats.auto_promoted == 0
    assert stats.pending == 0
    assert len(fdb.sources) == 0
    assert len(fdb.candidates) == 0


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_dedupe_against_existing_sources(monkeypatch):
    """A URL already in `sources.base_url` is skipped before LLM is invoked."""
    fdb = FakeDB()
    fdb.sources.append(
        {
            "id": 1,
            "slug": "preexisting",
            "name": "Preexisting source",
            "category": "ats",
            "base_url": "https://known.example.com",
        }
    )
    fdb.install(monkeypatch)
    counter = _mock_classifier(
        monkeypatch,
        default={
            "is_aggregator": True,
            "category": "ats",
            "confidence": 0.99,
            "rationale": "should-not-be-called",
        },
    )

    # Build a strategy that yields the dup URL + one new URL.
    class _StubStrategy:
        name = "github_awesome_lists"

        async def run(self, client):
            return [
                CandidateSource(url="https://known.example.com", title="dup", snippet="", discovered_via="github_awesome_lists"),
                CandidateSource(url="https://new.example.com", title="new", snippet="", discovered_via="github_awesome_lists"),
            ]

    monkeypatch.setattr(pipeline, "_all_strategies", lambda: [_StubStrategy()])

    async with httpx.AsyncClient() as client:
        stats = await pipeline.run_discovery_pipeline(http_client=client, llm_cap=10)

    # One classifier call (only the new URL); the dup gets pre-filtered.
    assert counter["calls"] == 1
    assert stats.total_llm_calls == 1
    # The new URL auto-promoted (confidence=0.99).
    assert stats.total_auto_promoted == 1
    # First strategy stats should show one duplicate.
    s_stats = stats.strategy_stats[0]
    assert s_stats.duplicates >= 1


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_daily_llm_cap_enforced(monkeypatch):
    """The pipeline-level llm_cap stops classification after N calls."""
    fdb = FakeDB()
    fdb.install(monkeypatch)
    counter = _mock_classifier(
        monkeypatch,
        default={
            "is_aggregator": True,
            "category": "other",
            "confidence": 0.6,
            "rationale": "ok",
        },
    )

    class _StubStrategy:
        name = "hn_algolia_search"

        async def run(self, client):
            # 5 candidates, but cap will be 2 → only 2 should be classified.
            return [
                CandidateSource(url=f"https://example.com/n{i}", title=f"n{i}", snippet="", discovered_via="hn_algolia_search")
                for i in range(5)
            ]

    monkeypatch.setattr(pipeline, "_all_strategies", lambda: [_StubStrategy()])

    async with httpx.AsyncClient() as client:
        stats = await pipeline.run_discovery_pipeline(http_client=client, llm_cap=2)

    assert counter["calls"] == 2
    assert stats.total_llm_calls == 2
    # The remaining 3 are reported as skipped_for_cap, not classified.
    s_stats = stats.strategy_stats[0]
    assert s_stats.skipped_for_cap == 3
    assert s_stats.classified == 2
    # Two classified, mid-confidence → both pending.
    assert stats.total_pending == 2

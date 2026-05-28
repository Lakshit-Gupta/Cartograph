"""Consumes Streams.RANK → scores opps → writes opportunity_scores → transitions to 'ranked'."""

from __future__ import annotations

import asyncio
import json
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.common.db import acquire, close_pool, current_tenant, init_pool
from src.common.logger import configure_logging, get_logger
from src.common.metrics import score_latency_seconds
from src.common.queue import Groups, RedisQ, Streams
from src.common.secrets import get_settings
from src.common.types import Opportunity
from src.ranker.embeddings import cosine, embed_one
from src.ranker.feedback import source_response_rates
from src.ranker.formula import load_weights_async, score

configure_logging("ranker")
_log = get_logger(__name__)


# --- Tunables ---------------------------------------------------------------
# Description prefix length fed into the lazy opp-embedding computation.
# Mirrors the cap used by the extractor when it normally seeds the embedding,
# so cold-path re-computes stay consistent with the steady-state shape.
_OPP_EMBED_DESCRIPTION_CHARS = 400
# Cap on profile-summary skill keywords — keeps the embedding input bounded
# even when skills.yaml grows. 80 words easily fits MiniLM's context window.
_PROFILE_SKILL_WORDS_LIMIT = 80
# How often the cached `source_response_rates()` snapshot is refreshed inside
# the consume loop. 30 min is the floor that keeps the ranker hot without
# pounding Postgres.
_RESPONSE_RATE_REFRESH_SECONDS = 1800
# Freelance opps with a final score at or above this threshold get an
# additional priority_push onto stream:notify (see end of `_score_one`).
_PRIORITY_PUSH_SCORE_THRESHOLD = 0.75
# Source quality multiplier — kept as a constant so future per-source
# overrides have a single point to plumb through.
_DEFAULT_SOURCE_QUALITY = 1.0


_profile_cache: dict | None = None


@dataclass(frozen=True, slots=True)
class _ScoreContext:
    """Static-per-tick context for `_score_one`.

    Bundled so the scoring callsite stays under the 5-param cap. The
    response-rate snapshot is mutated by the refresh task; we pass the dict
    by reference and reads pick up the latest map.
    """

    profile_emb: list[float]
    raw: dict
    resp_rates: dict[int, float]


async def _ensure_profile_embedding() -> tuple[list[float], dict]:
    """Load profile YAMLs, embed headline+skills, upsert into profiles table."""
    global _profile_cache
    if _profile_cache:
        return _profile_cache["embedding"], _profile_cache["raw"]

    settings = get_settings()
    root = Path(settings.config_root) / "profile"
    resume = json.loads((root / "resume.json").read_text())
    skills_doc = yaml.safe_load((root / "skills.yaml").read_text()) or {}
    prefs = yaml.safe_load((root / "prefs.yaml").read_text()) or {}
    comp_floors = yaml.safe_load((root / "comp_floors.yaml").read_text()) or {}

    skill_words: list[str] = []
    for group in skills_doc.values():
        if isinstance(group, dict):
            skill_words.extend(group.keys())
    headline = (resume.get("basics") or {}).get("summary", "")
    summary_text = f"{headline} | skills: {', '.join(skill_words[:_PROFILE_SKILL_WORDS_LIMIT])}"
    emb = await embed_one(summary_text)

    raw = {
        "resume": resume,
        "skills": skills_doc,
        "prefs": prefs,
        "comp_floors": comp_floors,
        "keywords": set(s.lower() for s in skill_words),
    }
    _profile_cache = {"embedding": emb, "raw": raw}

    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO profiles(user_id, embedding, headline, skills, raw_resume, raw_skills_yaml, raw_prefs_yaml, updated_at)
            VALUES ($7, $1::vector, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                embedding = EXCLUDED.embedding,
                headline = EXCLUDED.headline,
                skills = EXCLUDED.skills,
                raw_resume = EXCLUDED.raw_resume,
                raw_skills_yaml = EXCLUDED.raw_skills_yaml,
                raw_prefs_yaml = EXCLUDED.raw_prefs_yaml,
                updated_at = NOW()
            """,
            emb,
            headline,
            skill_words,
            json.dumps(resume),
            json.dumps(skills_doc),
            json.dumps(prefs),
            current_tenant(),
        )
    return emb, raw


def _comp_floor(prefs_comp: dict, cat: str) -> float:
    table = (prefs_comp.get("floors") or {}).get(cat) or {}
    # Prefer USD if present, else INR, else 0
    return float(
        table.get("usd_per_year")
        or table.get("usd_per_month")
        or table.get("usd_per_hour")
        or table.get("inr_per_year")
        or table.get("inr_per_month")
        or table.get("inr_per_hour")
        or 0
    )


def _floors_from_prefs(comp_floors_raw: dict) -> dict[str, float]:
    """Project the comp_floors YAML onto the `category -> floor` map that
    `formula.score` consumes. Contract-frozen key set (used by the formula
    side as well — every supported `OppCategory` value must appear)."""
    return {
        "internship": _comp_floor(comp_floors_raw, "internship"),
        "fulltime": _comp_floor(comp_floors_raw, "fulltime"),
        "freelance": _comp_floor(comp_floors_raw, "freelance"),
        "fellowship": _comp_floor(comp_floors_raw, "fellowship"),
        # `contract` shares the freelance floor — kept as an explicit alias so
        # the scorer never falls through to 0.0 silently when sources tag
        # opps as `contract`.
        "contract": _comp_floor(comp_floors_raw, "freelance"),
        "unknown": 0.0,
    }


def _extract_opportunity_record(rec: Any) -> Opportunity:
    """Project an asyncpg row from `opportunities` onto the `Opportunity`
    dataclass that `formula.score` expects. `canonical_url` is unused by the
    scorer, so we pass an empty placeholder to avoid an extra SELECT column.
    """
    return Opportunity(
        source_id=int(rec["source_id"]),
        canonical_url="",  # unused for scoring
        title=rec["title"],
        company=rec["company"],
        description=rec["description"],
        location=rec["location"],
        remote_type=rec["remote_type"],
        category=rec["category"],
        comp_min=rec["comp_min"],
        comp_max=rec["comp_max"],
        comp_currency=rec["comp_currency"],
        comp_period=rec["comp_period"],
        posted_at=rec["posted_at"],
        fingerprint_hash=rec["fingerprint_hash"] or "",
        extraction_tier=int(rec["extraction_tier"] or 0),
        extraction_confidence=float(rec["extraction_confidence"] or 0),
    )


async def _ensure_embedding(rec: Any, opportunity_id: str) -> list[float]:
    """Return the opp's embedding vector, computing+persisting on cold path.

    Cold path: the extractor failed to seed `embedding` (or this row predates
    the embedding column being populated). We compute on the spot, write it
    back so the next score is hot, and return the freshly-computed vector.

    Hot path: `rec['embedding']` is already a pgvector — coerce to list.
    """
    if rec["embedding"] is None:
        text = f"{rec['title']} | {(rec['company'] or '')} | {(rec['description'] or '')[:_OPP_EMBED_DESCRIPTION_CHARS]}"
        opp_emb = await embed_one(text)
        async with acquire() as conn:
            await conn.execute(
                "UPDATE opportunities SET embedding = $1::vector WHERE id = $2",
                opp_emb,
                opportunity_id,
            )
        return opp_emb
    return list(rec["embedding"])


async def _persist_score(opportunity_id: str, out: Any) -> None:
    """UPSERT the (user, opp) row in `opportunity_scores`.

    SQL kept verbatim — Phase 4.2 cutover relies on the
    `VALUES ($4, $1, $2, $3::jsonb, 'v1')` parameter shape (user_id last).
    """
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO opportunity_scores(user_id, opportunity_id, score, score_components, ranker_version)
            VALUES ($4, $1, $2, $3::jsonb, 'v1')
            ON CONFLICT (user_id, opportunity_id) DO UPDATE
              SET score = EXCLUDED.score,
                  score_components = EXCLUDED.score_components,
                  scored_at = NOW()
            """,
            opportunity_id,
            out.score,
            json.dumps(out.components),
            current_tenant(),
        )


async def _transition_to_ranked(opportunity_id: str) -> None:
    """Move an opp out of `new`/`queued` into `ranked` after scoring.

    Predicate kept identical to the pre-refactor version — Phase 1 state
    machine treats any other source state (e.g. `applied`, `archived`) as a
    no-op.
    """
    async with acquire() as conn:
        await conn.execute(
            "UPDATE opportunities SET state = 'ranked' WHERE id = $1 AND state IN ('new','queued')",
            opportunity_id,
        )


async def _persist_comp_min_inr(opportunity_id: str, comp_min: float | None, comp_currency: str | None) -> None:
    """Populate `opportunities.comp_min_inr` (V023) with the INR-normalized
    comp value for the auto-apply filter to read without per-row Python.

    Best-effort — failures here are logged but never break ranking; the
    auto-apply filter treats NULL comp_min_inr as "no comp signal" (passes).
    """
    from src.common.currency import to_inr

    inr = to_inr(comp_min, comp_currency)
    if inr is None:
        return
    try:
        async with acquire() as conn:
            await conn.execute(
                "UPDATE opportunities SET comp_min_inr = $2 WHERE id = $1",
                opportunity_id,
                float(inr),
            )
    except Exception as e:
        _log.warning("ranker_comp_min_inr_persist_failed", err=str(e), opp_id=opportunity_id)


async def _maybe_priority_push(q: RedisQ, opportunity_id: str, category: Any, final_score: float) -> None:
    """Freelance + high score → publish a priority_push to stream:notify.

    Threshold lives in `_PRIORITY_PUSH_SCORE_THRESHOLD` so the digest
    notifier and this worker can be retuned from one place.
    """
    if category == "freelance" and final_score >= _PRIORITY_PUSH_SCORE_THRESHOLD:
        await q.publish(
            Streams.NOTIFY,
            {
                "kind": "priority_push",
                "user_id": 1,
                "opportunity_id": opportunity_id,
            },
        )


async def _score_one(q: RedisQ, opportunity_id: str, ctx: _ScoreContext) -> None:
    t0 = time.perf_counter()
    async with acquire() as conn:
        rec = await conn.fetchrow(
            """
            SELECT id, source_id, title, company, description, location, remote_type, category,
                   comp_min, comp_max, comp_currency, comp_period, posted_at, embedding, fingerprint_hash,
                   apply_url, apply_method, extraction_tier, extraction_confidence
            FROM opportunities WHERE id = $1
            """,
            opportunity_id,
        )
    if rec is None:
        return

    opp_emb = await _ensure_embedding(rec, opportunity_id)
    emb_sim = cosine(ctx.profile_emb, opp_emb)
    opp = _extract_opportunity_record(rec)

    weights = await load_weights_async()
    floors = _floors_from_prefs(ctx.raw["comp_floors"])
    out = score(
        opp,
        profile_keywords=set(ctx.raw["keywords"]),
        embedding_sim=emb_sim,
        source_quality=_DEFAULT_SOURCE_QUALITY,
        response_rate=float(ctx.resp_rates.get(int(rec["source_id"]), 0.0)),
        comp_floors=floors,
        weights=weights,
    )

    await _persist_score(opportunity_id, out)
    await _persist_comp_min_inr(opportunity_id, rec["comp_min"], rec["comp_currency"])
    await _transition_to_ranked(opportunity_id)

    score_latency_seconds.observe(time.perf_counter() - t0)

    await _maybe_priority_push(q, opportunity_id, rec["category"], out.score)


async def main() -> None:
    await init_pool()
    q = await RedisQ.connect()
    profile_emb, raw = await _ensure_profile_embedding()

    # Refresh response rates every 30 min
    resp_rates: dict[int, float] = await source_response_rates()
    ctx = _ScoreContext(profile_emb=profile_emb, raw=raw, resp_rates=resp_rates)

    async def refresh_rates() -> None:
        nonlocal ctx
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=_RESPONSE_RATE_REFRESH_SECONDS)
            except TimeoutError:
                new_rates = await source_response_rates()
                ctx = _ScoreContext(profile_emb=ctx.profile_emb, raw=ctx.raw, resp_rates=new_rates)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    _refresh_task = asyncio.create_task(refresh_rates(), name="refresh_response_rates")
    _log.info("ranker_started")

    async for msg in q.consume(Streams.RANK, Groups.RANKERS):
        if stop.is_set():
            break
        try:
            opp_id = msg.fields.get("opportunity_id")
            if not opp_id:
                # Contract violation: every Streams.RANK payload MUST carry an
                # opportunity_id. Producers that have inline opps should call
                # src.extractors.persist.persist_and_publish() first.
                await q.dlq(
                    Streams.RANK,
                    msg.msg_id,
                    msg.fields,
                    "contract_violation_missing_opportunity_id",
                )
                _log.error("rank_contract_violation", payload=msg.fields)
            else:
                await _score_one(q, opp_id, ctx)
        except Exception as e:
            _log.exception("ranker_failed", err=str(e))
            await q.dlq(Streams.RANK, msg.msg_id, msg.fields, str(e))
        await q.ack(Streams.RANK, Groups.RANKERS, msg.msg_id)

    _refresh_task.cancel()
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

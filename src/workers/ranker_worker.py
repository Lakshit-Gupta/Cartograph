"""Consumes Streams.RANK → scores opps → writes opportunity_scores → transitions to 'ranked'."""
from __future__ import annotations

import asyncio
import json
import signal
import time
from pathlib import Path

import yaml

from src.common.db import acquire, close_pool, init_pool
from src.common.logger import configure_logging, get_logger
from src.common.metrics import score_latency_seconds
from src.common.queue import Groups, RedisQ, Streams
from src.common.secrets import get_settings
from src.common.types import Opportunity
from src.ranker.embeddings import cosine, embed_one
from src.ranker.feedback import source_response_rates
from src.ranker.formula import load_weights, score

configure_logging("ranker")
_log = get_logger(__name__)


_profile_cache: dict | None = None


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
    summary_text = f"{headline} | skills: {', '.join(skill_words[:80])}"
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
            VALUES (1, $1::vector, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                embedding = EXCLUDED.embedding,
                headline = EXCLUDED.headline,
                skills = EXCLUDED.skills,
                raw_resume = EXCLUDED.raw_resume,
                raw_skills_yaml = EXCLUDED.raw_skills_yaml,
                raw_prefs_yaml = EXCLUDED.raw_prefs_yaml,
                updated_at = NOW()
            """,
            emb, headline, skill_words, json.dumps(resume), json.dumps(skills_doc), json.dumps(prefs),
        )
    return emb, raw


def _comp_floor(prefs_comp: dict, cat: str) -> float:
    table = (prefs_comp.get("floors") or {}).get(cat) or {}
    # Prefer USD if present, else INR, else 0
    return float(table.get("usd_per_year") or table.get("usd_per_month") or table.get("usd_per_hour") or
                 table.get("inr_per_year") or table.get("inr_per_month") or table.get("inr_per_hour") or 0)


async def _score_one(q: RedisQ, opportunity_id: str, profile_emb: list[float], raw: dict, resp_rates: dict[int, float]) -> None:
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

    # Ensure embedding exists; compute if missing
    if rec["embedding"] is None:
        text = f"{rec['title']} | {(rec['company'] or '')} | {(rec['description'] or '')[:400]}"
        opp_emb = await embed_one(text)
        async with acquire() as conn:
            await conn.execute(
                "UPDATE opportunities SET embedding = $1::vector WHERE id = $2",
                opp_emb, opportunity_id,
            )
    else:
        opp_emb = list(rec["embedding"])

    emb_sim = cosine(profile_emb, opp_emb)

    opp = Opportunity(
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

    weights = load_weights()
    floors_table = raw["comp_floors"]
    floors = {
        "internship": _comp_floor(floors_table, "internship"),
        "fulltime":   _comp_floor(floors_table, "fulltime"),
        "freelance":  _comp_floor(floors_table, "freelance"),
        "fellowship": _comp_floor(floors_table, "fellowship"),
        "contract":   _comp_floor(floors_table, "freelance"),
        "unknown":    0.0,
    }
    out = score(
        opp,
        profile_keywords=set(raw["keywords"]),
        embedding_sim=emb_sim,
        source_quality=1.0,
        response_rate=float(resp_rates.get(int(rec["source_id"]), 0.0)),
        comp_floors=floors,
        weights=weights,
    )

    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO opportunity_scores(user_id, opportunity_id, score, score_components, ranker_version)
            VALUES (1, $1, $2, $3::jsonb, 'v1')
            ON CONFLICT (user_id, opportunity_id) DO UPDATE
              SET score = EXCLUDED.score,
                  score_components = EXCLUDED.score_components,
                  scored_at = NOW()
            """,
            opportunity_id, out.score, json.dumps(out.components),
        )
        await conn.execute(
            "UPDATE opportunities SET state = 'ranked' WHERE id = $1 AND state IN ('new','queued')",
            opportunity_id,
        )

    score_latency_seconds.observe(time.perf_counter() - t0)

    # Priority push if freelance + high score
    if rec["category"] == "freelance" and out.score >= 0.75:
        await q.publish(Streams.NOTIFY, {
            "kind": "priority_push", "user_id": 1, "opportunity_id": opportunity_id,
        })


async def main() -> None:
    await init_pool()
    q = await RedisQ.connect()
    profile_emb, raw = await _ensure_profile_embedding()

    # Refresh response rates every 30 min
    resp_rates: dict[int, float] = await source_response_rates()

    async def refresh_rates() -> None:
        nonlocal resp_rates
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=1800)
            except TimeoutError:
                resp_rates = await source_response_rates()

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
                    Streams.RANK, msg.msg_id, msg.fields,
                    "contract_violation_missing_opportunity_id",
                )
                _log.error("rank_contract_violation", payload=msg.fields)
            else:
                await _score_one(q, opp_id, profile_emb, raw, resp_rates)
        except Exception as e:
            _log.exception("ranker_failed", err=str(e))
            await q.dlq(Streams.RANK, msg.msg_id, msg.fields, str(e))
        await q.ack(Streams.RANK, Groups.RANKERS, msg.msg_id)

    _refresh_task.cancel()
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

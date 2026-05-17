"""Resume bullet tailoring + variant picker.

Loads one of `config/profile/resume_variants/{backend,fullstack,ml}.json`,
picks the 3 most relevant experience/project bullets, and asks the LLM-writer
to rewrite them so that opp-specific tech is surfaced. The original
profile.json is treated as authoritative — the LLM may emphasize but never
invent.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from src.common.llm import chat_json, fence_untrusted
from src.common.logger import get_logger
from src.common.secrets import get_settings
from src.common.types import Opportunity

_log = get_logger(__name__)

_VARIANT_LABELS = ("backend", "fullstack", "ml")
_DEFAULT_VARIANT = "backend"

# Tokens used by pick_variant to vote
_BACKEND_BIAS = {
    "backend", "infra", "platform", "devops", "sre", "reliability",
    "api", "database", "postgres", "redis", "queue", "kafka", "systems",
    "distributed", "microservice", "docker", "kubernetes", "k8s", "linux",
}
_FULLSTACK_BIAS = {
    "fullstack", "full-stack", "full stack", "frontend", "front-end",
    "react", "next.js", "nextjs", "typescript", "ui", "ux", "tailwind",
    "node", "express", "product engineer", "web", "saas",
}
_ML_BIAS = {
    "ml", "ai", "llm", "rag", "embedding", "embeddings", "vector",
    "agent", "agentic", "generative", "openai", "anthropic", "transformer",
    "nlp", "computer vision", "deep learning", "pytorch", "tensorflow",
    "data scientist", "research engineer",
}


def _variants_dir() -> Path:
    return Path(get_settings().config_root) / "profile" / "resume_variants"


def _profile_dir() -> Path:
    return Path(get_settings().config_root) / "profile"


def _load_variant(label: str) -> dict[str, Any]:
    label = label if label in _VARIANT_LABELS else _DEFAULT_VARIANT
    path = _variants_dir() / f"{label}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _load_skills() -> dict[str, Any]:
    path = _profile_dir() / "skills.yaml"
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        _log.warning("skills_yaml_missing", path=str(path))
        return {}


def _collect_candidate_bullets(profile_dict: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk profile experience + projects and yield candidate bullets w/ keywords."""
    out: list[dict[str, Any]] = []
    for exp in profile_dict.get("experience", []) or []:
        kws = [k.lower() for k in (exp.get("keywords") or [])]
        for bullet in exp.get("bullets", []) or []:
            if not bullet or bullet.startswith("Quantified achievement"):
                continue
            out.append({
                "source": "experience",
                "context": f"{exp.get('title','')} @ {exp.get('company','')}",
                "bullet": bullet,
                "keywords": kws,
            })
    for proj in profile_dict.get("projects", []) or []:
        kws = [k.lower() for k in (proj.get("keywords") or [])]
        summary = proj.get("summary") or ""
        if summary:
            out.append({
                "source": "project",
                "context": proj.get("name", ""),
                "bullet": summary,
                "keywords": kws,
            })
    return out


def _opp_tokens(opp: Opportunity | dict[str, Any]) -> set[str]:
    if isinstance(opp, dict):
        text = " ".join(filter(None, [
            opp.get("title"), opp.get("description"), opp.get("company"),
        ]))
    else:
        text = " ".join(filter(None, [opp.title, opp.description, opp.company]))
    tokens = re.findall(r"[a-zA-Z][a-zA-Z+#\.\-]{1,}", text.lower())
    return set(tokens)


def pick_variant(opp: Opportunity | dict[str, Any]) -> str:
    """Pick the best-fit resume variant by keyword vote against lean_keywords + bias."""
    tokens = _opp_tokens(opp)
    if not tokens:
        return _DEFAULT_VARIANT

    scores: dict[str, int] = {}
    for label in _VARIANT_LABELS:
        try:
            variant = _load_variant(label)
        except FileNotFoundError:
            scores[label] = 0
            continue
        lean = {k.lower() for k in variant.get("lean_keywords", [])}
        scores[label] = sum(1 for t in tokens if t in lean)

    # Add coarse-bias votes (catches synonyms not in lean_keywords)
    for bias_set, label in (
        (_BACKEND_BIAS, "backend"),
        (_FULLSTACK_BIAS, "fullstack"),
        (_ML_BIAS, "ml"),
    ):
        scores[label] = scores.get(label, 0) + sum(1 for t in tokens if t in bias_set)

    best = max(scores.items(), key=lambda kv: kv[1])
    if best[1] == 0:
        return _DEFAULT_VARIANT
    return best[0]


def _is_profile_template_placeholder(profile_dict: dict[str, Any]) -> bool:
    edu = (profile_dict.get("education") or [{}])[0]
    return edu.get("school") == "REPLACE" or edu.get("degree") == "REPLACE"


def _rank_candidates(
    candidates: list[dict[str, Any]],
    opp_tokens: set[str],
    variant_kws: set[str],
) -> list[dict[str, Any]]:
    """Sort candidates by overlap with (opp tokens) + (variant lean keywords)."""
    def score(c: dict[str, Any]) -> int:
        kw_hits = sum(1 for k in c["keywords"] if k in opp_tokens)
        text_hits = sum(1 for t in opp_tokens if t in c["bullet"].lower())
        lean_hits = sum(1 for k in c["keywords"] if k in variant_kws)
        return kw_hits * 3 + text_hits + lean_hits
    return sorted(candidates, key=score, reverse=True)


async def tailor_bullets(
    profile_dict: dict[str, Any],
    opp: Opportunity | dict[str, Any],
    variant_label: str,
) -> list[str]:
    """Return up to 5 LLM-rewritten bullets, max 3 sent for rewriting."""
    if _is_profile_template_placeholder(profile_dict):
        _log.warning("profile_is_template_placeholder")

    variant = _load_variant(variant_label)
    skills = _load_skills()
    variant_kws = {k.lower() for k in variant.get("lean_keywords", [])}

    candidates = _collect_candidate_bullets(profile_dict)
    if not candidates:
        _log.warning("no_candidate_bullets", variant=variant_label)
        return []

    top_three = _rank_candidates(candidates, _opp_tokens(opp), variant_kws)[:3]

    opp_summary = {
        "title": getattr(opp, "title", None) or (opp.get("title") if isinstance(opp, dict) else ""),
        "company": getattr(opp, "company", None) or (opp.get("company") if isinstance(opp, dict) else ""),
        "description": (getattr(opp, "description", None) or
                        (opp.get("description") if isinstance(opp, dict) else "") or "")[:1500],
    }

    system = (
        "You rewrite resume bullets to surface opp-relevant tech without inventing facts. "
        "Each bullet remains a one-line, action-led achievement. Return JSON only."
    )
    user = (
        "Rewrite the candidate bullets to emphasize tech mentioned in <OPP>. "
        "Preserve all numbers, employers, project names. Never invent new claims. "
        "Output JSON: {\"bullets\": [\"...\", \"...\", \"...\"]} with at most 5 entries.\n\n"
        f"<VARIANT>{variant.get('label')} — {variant.get('headline','')}</VARIANT>\n"
        f"<LEAN_KEYWORDS>{', '.join(sorted(variant_kws))}</LEAN_KEYWORDS>\n"
        f"<SKILL_HINTS>{json.dumps({k: list(v.keys()) for k, v in skills.items() if isinstance(v, dict)})[:1200]}</SKILL_HINTS>\n"
        f"<OPP>{fence_untrusted(json.dumps(opp_summary))}</OPP>\n"
        f"<CANDIDATES>{json.dumps(top_three)}</CANDIDATES>\n"
    )

    try:
        data = await chat_json(
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            kind="llm_writer",
            model=get_settings().openrouter_model_writer,
            max_tokens=900,
            temperature=0.2,
        )
    except Exception as e:
        _log.warning("tailor_bullets_llm_failed", err=str(e))
        # Graceful fallback — return the originals
        return [c["bullet"] for c in top_three][:5]

    bullets = data.get("bullets") if isinstance(data, dict) else None
    if not isinstance(bullets, list):
        _log.warning("tailor_bullets_bad_shape", got=type(data).__name__)
        return [c["bullet"] for c in top_three][:5]

    cleaned = [str(b).strip() for b in bullets if str(b).strip()]
    return cleaned[:5]

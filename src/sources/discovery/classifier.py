"""LLM-based aggregator classifier for discovery candidates.

Wraps `src.common.llm.chat_json` with a strict response schema. Each call
costs ~$0.001 with Gemini Flash; the pipeline enforces a 50-call/day cap.

The prompt lives in `config/prompts/source_classifier.txt`; we substitute
4 placeholders ({url}, {title}, {snippet}, {discovered_via}) and wrap the
substituted block in `<IGNORE>...</IGNORE>` sentinels so the model treats
candidate-controlled text as data per the CLAUDE.md "LLM sandboxing" rule.
"""

from __future__ import annotations

from typing import Any

from src.common.llm import (
    LLMEmptyResponse,
    LLMInvalidJSON,
    LLMSafetyBlock,
    chat_json,
    fence_untrusted,
    load_prompt,
)
from src.common.logger import get_logger
from src.common.secrets import get_settings
from src.sources.discovery.base import CandidateSource

_log = get_logger(__name__)

_VALID_CATEGORIES = {
    "ats",
    "rss",
    "github_md",
    "hn",
    "reddit",
    "fellowship",
    "india",
    "freelance",
    "other",
}


def _build_messages(candidate: CandidateSource) -> list[dict[str, str]]:
    """Render the prompt template with the candidate's data fenced."""
    template = load_prompt("source_classifier.txt")
    # Build the substitution dict — fence each user-controlled field so the
    # model can't be hijacked by a malicious snippet containing prompt-injection
    # text. The {{ }} -> { } unescape in the template lets us still emit
    # literal JSON braces.
    safe = {
        "url": candidate.url,
        "title": fence_untrusted(candidate.title or "<none>"),
        "snippet": fence_untrusted(candidate.snippet or "<none>"),
        "discovered_via": candidate.discovered_via,
    }
    body = template.format(**safe)
    return [
        {"role": "system", "content": "You are a strict JSON classifier. Return only the JSON object."},
        {"role": "user", "content": body},
    ]


def _coerce_result(data: dict[str, Any]) -> dict[str, Any] | None:
    """Validate + coerce the model's JSON. Returns None on schema violation."""
    if not isinstance(data, dict):
        return None
    is_agg = data.get("is_aggregator")
    cat = data.get("category")
    conf = data.get("confidence")
    rat = data.get("rationale", "")
    if not isinstance(is_agg, bool):
        return None
    if cat not in _VALID_CATEGORIES:
        return None
    try:
        conf_f = float(conf)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= conf_f <= 1.0):
        # Clamp instead of reject — models sometimes return 1.2 etc.
        conf_f = max(0.0, min(1.0, conf_f))
    return {
        "is_aggregator": is_agg,
        "category": cat,
        "confidence": conf_f,
        "rationale": str(rat)[:500],
    }


async def classify(candidate: CandidateSource) -> dict[str, Any] | None:
    """Run the classifier for one candidate.

    Returns the parsed + validated result dict, or None on:
      - empty / safety-blocked LLM response
      - JSON parse failure
      - schema-violating output (missing keys, bad enum, bad confidence)

    Cost gate is enforced inside `chat_json` (kind="llm_classifier" hits
    the same usage_ledger as every other LLM call).
    """
    settings = get_settings()
    messages = _build_messages(candidate)
    try:
        raw = await chat_json(
            messages=messages,
            kind="llm_classifier",
            model=settings.openrouter_model_classifier,
            temperature=0.0,
            max_tokens=400,
        )
    except (LLMEmptyResponse, LLMSafetyBlock, LLMInvalidJSON) as e:
        _log.warning("classifier_llm_failed", url=candidate.url, err=str(e))
        return None
    except Exception as e:
        _log.exception("classifier_unexpected_error", url=candidate.url, err=str(e))
        return None

    result = _coerce_result(raw)
    if result is None:
        _log.warning("classifier_bad_schema", url=candidate.url, raw_keys=list(raw.keys()) if isinstance(raw, dict) else None)
    return result


def apply_to_candidate(candidate: CandidateSource, result: dict[str, Any]) -> None:
    """Mutate candidate in place with classifier output."""
    candidate.classifier_confidence = result["confidence"]
    candidate.classifier_category = result["category"]
    candidate.classifier_rationale = result["rationale"]

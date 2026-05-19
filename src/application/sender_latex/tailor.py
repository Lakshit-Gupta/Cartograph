"""LLM-driven block tailoring.

Splits the legacy ``_llm_tailor_blocks`` body into four cohesive
functions:

1. :func:`_build_block_prompt` - pure prompt assembly (no I/O).
2. :func:`_call_llm_for_edits` - the cost-gated ``chat_json`` boundary.
3. :func:`_parse_llm_response` - pull the ``edits`` list out of the
   provider response.
4. :func:`_assert_response_shape` - schema-validate one edit entry.

The public entry point :func:`llm_tailor_blocks` orchestrates the four
in order. On any failure (missing prompt, LLM error, malformed JSON)
the function returns an empty mapping — the caller renders the
untailored tree, which still benefits from the metadata-scrub + qpdf
post-processing.

Cost gate: :func:`_call_llm_for_edits` routes through
``src.common.llm.chat_json`` so the ``daily_spend`` check fires before
the provider sees the request. ``kind="llm_writer"`` is mandatory —
V001 has no ``resume_tailor`` enum value and adding one is deferred
(see CLAUDE.md comment on the original site).
"""

from __future__ import annotations

import json
from typing import Any

from src.common.logger import get_logger
from src.common.secrets import get_settings

_log = get_logger(__name__)

_LLM_MAX_TOKENS = 1200
_LLM_TEMPERATURE = 0.2
_SYSTEM_PROMPT = "You rewrite resume bullets. Plain text only. Strict JSON. Never invent facts."


def _build_block_prompt(
    blocks: list[Any],
    opp_summary: dict[str, Any],
    variant_label: str,
) -> str | None:
    """Render the user prompt for the tailor LLM call.

    Returns ``None`` when the on-disk prompt template is missing — the
    caller treats that as "no edits".
    """
    from src.common.llm import fence_untrusted, load_prompt

    try:
        prompt = load_prompt("resume_tailor.txt")
    except FileNotFoundError:
        _log.warning("resume_tailor_prompt_missing")
        return None

    block_payload = [{"id": b.id, "kind": b.kind, "title": b.title, "bullets": b.bullets} for b in blocks]
    return prompt.format(
        opp_summary=fence_untrusted(json.dumps(opp_summary)),
        variant_label=variant_label,
        blocks_json=json.dumps(block_payload),
    )


async def _call_llm_for_edits(user_prompt: str) -> dict[str, Any] | None:
    """Cost-gated LLM call. Returns the raw JSON dict or ``None`` on error.

    The ``kind="llm_writer"`` argument is the V001-compatible cost-ledger
    enum value — see CLAUDE.md note in the legacy site for why a dedicated
    ``resume_tailor`` value isn't used.
    """
    from src.common.llm import chat_json

    try:
        return await chat_json(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            kind="llm_writer",
            model=get_settings().openrouter_model_writer,
            max_tokens=_LLM_MAX_TOKENS,
            temperature=_LLM_TEMPERATURE,
        )
    except Exception as e:
        _log.warning("resume_tailor_llm_failed", err=str(e))
        return None


def _parse_llm_response(data: dict[str, Any] | None) -> list[Any]:
    """Pull the ``edits`` list out of the provider response.

    Returns ``[]`` for any shape that is not ``{"edits": [...]}``.
    """
    if not isinstance(data, dict):
        return []
    edits_list = data.get("edits")
    return edits_list if isinstance(edits_list, list) else []


def _assert_response_shape(entry: Any) -> tuple[str, list[str]] | None:
    """Validate one edit entry. Returns ``(block_id, clean_bullets)`` or ``None``."""
    if not isinstance(entry, dict):
        return None
    bid = entry.get("id")
    bullets = entry.get("bullets")
    if not isinstance(bid, str) or not isinstance(bullets, list):
        return None
    cleaned = [str(b).strip() for b in bullets if str(b).strip()]
    if not cleaned:
        return None
    return bid, cleaned


async def llm_tailor_blocks(
    blocks: list[Any],
    opp_summary: dict[str, Any],
    variant_label: str,
) -> dict[str, list[str]]:
    """Rewrite the top-K block bullets via the LLM.

    Returns a mapping of ``block_id -> new bullets``. The caller treats
    an empty mapping as "no edits" and renders the untailored tree.
    """
    user_prompt = _build_block_prompt(blocks, opp_summary, variant_label)
    if user_prompt is None:
        return {}

    data = await _call_llm_for_edits(user_prompt)
    edits_list = _parse_llm_response(data)

    out: dict[str, list[str]] = {}
    for entry in edits_list:
        parsed = _assert_response_shape(entry)
        if parsed is not None:
            bid, cleaned = parsed
            out[bid] = cleaned
    return out


__all__ = ["llm_tailor_blocks"]

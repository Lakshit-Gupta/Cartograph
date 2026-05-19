"""OpenRouter client with cost ledger + daily-cap circuit breaker.

Every LLM call in the codebase goes through this module. Refuses when
daily_spend exceeds `cost_cap_daily_kill_usd`. Warns past `cost_cap_daily_usd`.

The orchestration `chat()` is intentionally split into small private helpers so
the cost-gate, the HTTP retry policy, and the bookkeeping (cost metric +
usage_ledger + daily_spend) can each be reasoned about and tested in isolation.

Invariants — all enforced below:

* Cost-gate refusal fires BEFORE the LLM HTTP request (see ``_assert_under_cap``).
* ``daily_spend`` UPDATE + ``usage_ledger`` INSERT share the same connection so
  they stay co-located (see ``_record_usage_ledger``).
* ``kind`` is opaque to this module — callers MUST pass one of the
  ``usage_kind_enum`` values declared in V001:
  ``llm_extract | llm_rerank | llm_writer | llm_classifier``.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.common.db import acquire
from src.common.logger import get_logger
from src.common.metrics import (
    llm_cost_usd_total,
    llm_refusals_total,
)
from src.common.secrets import get_settings

_log = get_logger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Tunables — module constants so ops can audit them in one place.
_HTTP_TIMEOUT_SECONDS = 60.0  # floor; OpenRouter sometimes streams long.
_RETRY_MAX_ATTEMPTS = 3  # transient-only; HTTPError class below.
_RETRY_WAIT_MIN = 1
_RETRY_WAIT_MAX = 10

# Cost-cap field names on the Settings object. Centralised so a rename in
# `secrets.py` is a one-line edit here.
_CAP_FIELD_HARD = "cost_cap_daily_kill_usd"
_CAP_FIELD_SOFT = "cost_cap_daily_usd"

# Approx per-1M-token pricing in USD. Refresh periodically. Conservative ceilings.
# Unknown slug falls back to (1.0, 5.0) — costs over-report, not under-report.
_PRICING: dict[str, tuple[float, float]] = {
    # Google Gemini (current as of 2026-05)
    "google/gemini-2.5-flash-lite": (0.10, 0.40),
    "google/gemini-2.5-flash": (0.15, 0.60),
    "google/gemini-3-flash-preview": (0.50, 3.00),
    "google/gemini-3.1-flash-lite": (0.25, 1.50),
    "google/gemini-3.1-flash-lite-preview": (0.25, 1.50),
    # Anthropic Claude (current)
    "anthropic/claude-haiku-4.5": (1.00, 5.00),
    "anthropic/claude-sonnet-4.6": (3.00, 15.00),
    # DeepSeek V4 family
    "deepseek/deepseek-v4-flash": (0.112, 0.224),
    "deepseek/deepseek-v4-pro": (0.435, 0.87),
    # xAI Grok (current)
    "x-ai/grok-4.20": (1.25, 2.50),
    "x-ai/grok-4.20-beta": (2.00, 6.00),
    # Moonshot Kimi (current)
    "moonshotai/kimi-k2.6": (0.73, 3.49),
    "moonshotai/kimi-k2.5": (0.40, 1.90),
    # OpenAI (reference)
    "openai/gpt-4o-mini": (0.15, 0.60),
    "openai/gpt-4o": (2.50, 10.00),
    # Legacy (kept for backward compat with old usage_ledger rows)
    "google/gemini-flash-1.5": (0.075, 0.30),
    "anthropic/claude-3.5-sonnet": (3.00, 15.00),
    "anthropic/claude-3.5-haiku": (0.80, 4.00),
}


class CostCapReached(RuntimeError):
    pass


class LLMEmptyResponse(RuntimeError):
    """Provider returned no content (empty string, null, or missing field)."""


class LLMSafetyBlock(RuntimeError):
    """Provider refused output via safety filter (finish_reason=content_filter)."""


class LLMInvalidJSON(RuntimeError):
    """Provider returned content but it failed json.loads."""


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------
def _prompt_path(filename: str) -> Path:
    settings = get_settings()
    return Path(settings.config_root) / "prompts" / filename


def load_prompt(filename: str) -> str:
    return _prompt_path(filename).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Cost gate
# ---------------------------------------------------------------------------
async def _today_spend_usd() -> float:
    async with acquire() as conn:
        rec = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(cost_usd_micros),0) AS m
            FROM usage_ledger
            WHERE ts >= NOW() - INTERVAL '24 hours'
            """
        )
    return float(rec["m"]) / 1_000_000.0


async def _assert_under_cap() -> None:
    """Raise ``CostCapReached`` (and bump refusal metric) when at/over hard cap.

    Soft-cap breach logs a warning but lets the call proceed. This MUST run
    before the HTTP request — never trust the provider to enforce our budget.
    """
    settings = get_settings()
    hard_cap = getattr(settings, _CAP_FIELD_HARD)
    soft_cap = getattr(settings, _CAP_FIELD_SOFT)

    spent = await _today_spend_usd()
    if spent >= hard_cap:
        llm_refusals_total.inc()
        raise CostCapReached(f"daily kill cap reached: ${spent:.2f}")
    if spent >= soft_cap:
        _log.warning("llm_cost_soft_cap_hit", spent=spent)


# ---------------------------------------------------------------------------
# Cost calculation + bookkeeping
# ---------------------------------------------------------------------------
def _calc_cost(model: str, in_tok: int, out_tok: int) -> float:
    in_price, out_price = _PRICING.get(model, (1.0, 5.0))
    return (in_tok / 1_000_000) * in_price + (out_tok / 1_000_000) * out_price


async def _record_usage_ledger(
    *,
    kind: str,
    model: str,
    in_tok: int,
    out_tok: int,
    cost_usd: float,
    correlation_id: str,
) -> None:
    """Insert into ``usage_ledger`` and upsert today's row in ``daily_spend``.

    Both writes share one connection so they remain co-located. ``kind`` MUST
    be a value from the ``usage_kind_enum`` (V001).
    """
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO usage_ledger(kind, provider, model, input_tokens, output_tokens,
                                     cost_usd_micros, correlation_id)
            VALUES ($1,'openrouter',$2,$3,$4,$5,$6)
            """,
            kind,
            model,
            in_tok,
            out_tok,
            int(round(cost_usd * 1_000_000)),
            correlation_id,
        )
        today = date.today()
        await conn.execute(
            """
            INSERT INTO daily_spend(date, source_id, tier, request_count, cents_spent)
            VALUES ($1, NULL, 0, 1, $2)
            ON CONFLICT (date, COALESCE(source_id,0), tier)
            DO UPDATE SET request_count = daily_spend.request_count + 1,
                          cents_spent  = daily_spend.cents_spent + $2
            """,
            today,
            int(round(cost_usd * 100)),
        )


# Back-compat alias — older import sites (and tests, scripts) may still reach
# for ``_record_usage``. The new canonical name is ``_record_usage_ledger``.
_record_usage = _record_usage_ledger


# ---------------------------------------------------------------------------
# HTTP transport + retry policy
# ---------------------------------------------------------------------------
def _retry_policy() -> AsyncRetrying:
    """Tenacity retry config — transient HTTP only, exponential backoff."""
    return AsyncRetrying(
        stop=stop_after_attempt(_RETRY_MAX_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=_RETRY_WAIT_MIN, max=_RETRY_WAIT_MAX),
        retry=retry_if_exception_type((httpx.HTTPError,)),
        reraise=True,
    )


def _build_payload(
    *,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    response_format: dict[str, Any] | None,
    reasoning_effort: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format:
        payload["response_format"] = response_format
    if reasoning_effort:
        # OpenRouter pass-through for reasoning-capable models (Gemini 3 thinking,
        # DeepSeek V4 high/xhigh, Grok 4.x). Ignored by non-reasoning models.
        payload["reasoning"] = {"effort": reasoning_effort}
    return payload


def _build_headers(
    *,
    api_key: str,
    extra_headers: dict[str, str] | None,
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": os.environ.get("OPENROUTER_REFERER", "https://github.com/cartograph"),
        "X-Title": "cartograph",
    }
    if extra_headers:
        headers.update(extra_headers)
    return headers


async def _call_openrouter(
    *,
    payload: dict[str, Any],
    headers: dict[str, str],
) -> dict[str, Any]:
    """POST to OpenRouter, retrying transient HTTP errors. Returns parsed JSON."""
    data: dict[str, Any] = {}
    try:
        async for attempt in _retry_policy():
            with attempt:
                async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
                    resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
    except RetryError as e:
        _log.error("llm_call_failed", err=str(e))
        raise
    if not data:
        raise RuntimeError("llm_call_empty_response")
    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def chat(
    *,
    messages: list[dict[str, str]],
    model: str | None = None,
    kind: str = "other",
    response_format: dict[str, Any] | None = None,
    temperature: float = 0.0,
    max_tokens: int = 1024,
    reasoning_effort: str | None = None,
    correlation_id: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Orchestrator: cost-gate → HTTP → cost metric + ledger.

    All public LLM call sites flow through here. The function is intentionally
    thin — each step delegates to a private helper that can be unit-tested in
    isolation.
    """
    settings = get_settings()
    model = model or settings.openrouter_model_classifier
    correlation_id = correlation_id or uuid.uuid4().hex

    # 1. Cost gate — MUST precede the HTTP request.
    await _assert_under_cap()

    # 2. Build the wire payload + headers.
    payload = _build_payload(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format=response_format,
        reasoning_effort=reasoning_effort,
    )
    headers = _build_headers(
        api_key=settings.openrouter_api_key,
        extra_headers=extra_headers,
    )

    # 3. HTTP call with retry on transient failures.
    data = await _call_openrouter(payload=payload, headers=headers)

    # 4. Cost metric + ledger row.
    usage = data.get("usage") or {}
    in_tok = int(usage.get("prompt_tokens", 0))
    out_tok = int(usage.get("completion_tokens", 0))
    cost = _calc_cost(model, in_tok, out_tok)
    llm_cost_usd_total.labels(kind=kind, model=model).inc(cost)
    await _record_usage_ledger(
        kind=kind,
        model=model,
        in_tok=in_tok,
        out_tok=out_tok,
        cost_usd=cost,
        correlation_id=correlation_id,
    )
    return data


# ---------------------------------------------------------------------------
# Response validation
# ---------------------------------------------------------------------------
def _validate_response_shape(data: dict[str, Any]) -> str:
    """Safely pull content out of an OpenRouter response, raising typed errors.

    Handles: missing choices, empty list, null/missing message, null/empty content,
    safety filter blocks. Each failure mode raises a distinct exception so callers
    and metrics can distinguish "model down" from "model refused" from "model
    returned junk".
    """
    if data.get("error"):
        raise LLMEmptyResponse(f"upstream_error: {data['error']}")
    choices = data.get("choices") or []
    if not choices:
        raise LLMEmptyResponse("no_choices_in_response")
    choice = choices[0] or {}
    finish_reason = (choice.get("finish_reason") or "").lower()
    if finish_reason in ("content_filter", "safety", "blocked"):
        raise LLMSafetyBlock(f"safety_block: {finish_reason}")
    msg = choice.get("message") or {}
    content = msg.get("content")
    if content is None or (isinstance(content, str) and not content.strip()):
        raise LLMEmptyResponse(f"empty_content: finish_reason={finish_reason or 'none'}")
    return content


# Back-compat alias for the previous internal name.
_extract_content = _validate_response_shape


async def chat_json(
    *,
    messages: list[dict[str, str]],
    schema_hint: str = "object",
    **kwargs: Any,
) -> Any:
    """Call chat() with a JSON response format and parse the result.

    Raises:
        LLMEmptyResponse: provider returned no content (empty/null).
        LLMSafetyBlock: provider refused via safety filter.
        LLMInvalidJSON: content present but not parseable as JSON.
    """
    data = await chat(
        messages=messages,
        response_format={"type": "json_object"} if schema_hint == "object" else None,
        **kwargs,
    )
    content = _validate_response_shape(data)
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        snippet = content[:200].replace("\n", "\\n")
        raise LLMInvalidJSON(f"json_decode_failed at pos {e.pos}: {snippet!r}") from e


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------
def fence_untrusted(text: str) -> str:
    """Wrap untrusted user-generated content in <IGNORE>...</IGNORE> sentinels."""
    return f"<IGNORE>\n{text}\n</IGNORE>"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()

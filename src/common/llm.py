"""OpenRouter client with cost ledger + daily-cap circuit breaker.

Every LLM call in the codebase goes through this module. Refuses when
daily_spend exceeds `cost_cap_daily_kill_usd`. Warns past `cost_cap_daily_usd`.
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

# Approx per-1M-token pricing in USD. Refresh periodically. Conservative ceilings.
# Unknown slug falls back to (1.0, 5.0) — costs over-report, not under-report.
_PRICING: dict[str, tuple[float, float]] = {
    # Google Gemini (current as of 2026-05)
    "google/gemini-2.5-flash-lite":            (0.10,  0.40),
    "google/gemini-2.5-flash":                 (0.15,  0.60),
    "google/gemini-3-flash-preview":           (0.50,  3.00),
    "google/gemini-3.1-flash-lite":            (0.25,  1.50),
    "google/gemini-3.1-flash-lite-preview":    (0.25,  1.50),
    # Anthropic Claude (current)
    "anthropic/claude-haiku-4.5":              (1.00,  5.00),
    "anthropic/claude-sonnet-4.6":             (3.00, 15.00),
    # DeepSeek V4 family
    "deepseek/deepseek-v4-flash":              (0.112, 0.224),
    "deepseek/deepseek-v4-pro":                (0.435, 0.87),
    # xAI Grok (current)
    "x-ai/grok-4.20":                          (1.25,  2.50),
    "x-ai/grok-4.20-beta":                     (2.00,  6.00),
    # Moonshot Kimi (current)
    "moonshotai/kimi-k2.6":                    (0.73,  3.49),
    "moonshotai/kimi-k2.5":                    (0.40,  1.90),
    # OpenAI (reference)
    "openai/gpt-4o-mini":                      (0.15,  0.60),
    "openai/gpt-4o":                           (2.50, 10.00),
    # Legacy (kept for backward compat with old usage_ledger rows)
    "google/gemini-flash-1.5":                 (0.075, 0.30),
    "anthropic/claude-3.5-sonnet":             (3.00, 15.00),
    "anthropic/claude-3.5-haiku":              (0.80,  4.00),
}


class CostCapReached(RuntimeError):
    pass


class LLMEmptyResponse(RuntimeError):
    """Provider returned no content (empty string, null, or missing field)."""


class LLMSafetyBlock(RuntimeError):
    """Provider refused output via safety filter (finish_reason=content_filter)."""


class LLMInvalidJSON(RuntimeError):
    """Provider returned content but it failed json.loads."""


def _prompt_path(filename: str) -> Path:
    settings = get_settings()
    return Path(settings.config_root) / "prompts" / filename


def load_prompt(filename: str) -> str:
    return _prompt_path(filename).read_text(encoding="utf-8")


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


async def _record_usage(
    *, kind: str, model: str, in_tok: int, out_tok: int, cost_usd: float, correlation_id: str
) -> None:
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO usage_ledger(kind, provider, model, input_tokens, output_tokens,
                                     cost_usd_micros, correlation_id)
            VALUES ($1,'openrouter',$2,$3,$4,$5,$6)
            """,
            kind, model, in_tok, out_tok, int(round(cost_usd * 1_000_000)), correlation_id,
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
            today, int(round(cost_usd * 100)),
        )


def _calc_cost(model: str, in_tok: int, out_tok: int) -> float:
    in_price, out_price = _PRICING.get(model, (1.0, 5.0))
    return (in_tok / 1_000_000) * in_price + (out_tok / 1_000_000) * out_price


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
    settings = get_settings()
    model = model or settings.openrouter_model_classifier
    correlation_id = correlation_id or uuid.uuid4().hex

    spent = await _today_spend_usd()
    if spent >= settings.cost_cap_daily_kill_usd:
        llm_refusals_total.inc()
        raise CostCapReached(f"daily kill cap reached: ${spent:.2f}")
    if spent >= settings.cost_cap_daily_usd:
        _log.warning("llm_cost_soft_cap_hit", spent=spent)

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

    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": os.environ.get("OPENROUTER_REFERER", "https://github.com/cartograph"),
        "X-Title": "cartograph",
    }
    if extra_headers:
        headers.update(extra_headers)

    data: dict[str, Any] = {}
    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type((httpx.HTTPError,)),
            reraise=True,
        ):
            with attempt:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
    except RetryError as e:
        _log.error("llm_call_failed", err=str(e))
        raise
    if not data:
        raise RuntimeError("llm_call_empty_response")

    usage = data.get("usage") or {}
    in_tok = int(usage.get("prompt_tokens", 0))
    out_tok = int(usage.get("completion_tokens", 0))
    cost = _calc_cost(model, in_tok, out_tok)
    llm_cost_usd_total.labels(kind=kind, model=model).inc(cost)
    await _record_usage(
        kind=kind, model=model, in_tok=in_tok, out_tok=out_tok,
        cost_usd=cost, correlation_id=correlation_id,
    )
    return data


def _extract_content(data: dict[str, Any]) -> str:
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
    content = _extract_content(data)
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        snippet = content[:200].replace("\n", "\\n")
        raise LLMInvalidJSON(f"json_decode_failed at pos {e.pos}: {snippet!r}") from e


def fence_untrusted(text: str) -> str:
    """Wrap untrusted user-generated content in <IGNORE>...</IGNORE> sentinels."""
    return f"<IGNORE>\n{text}\n</IGNORE>"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()

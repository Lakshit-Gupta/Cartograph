# LLM Model Selection — Phase 1 (2026-05-18)

Per-slot model picks for Cartograph's OpenRouter-mediated LLM calls. Each slot
is selected against the benchmark that actually predicts performance for that
slot's workload, with cost computed at projected Phase 1 volume.

---

## Workload contracts

| Slot | Calls/day | In tok | Out tok | Latency budget | Failure cost | Primary capability | Anti-capability |
|------|----------:|-------:|--------:|----------------|--------------|---------------------|-----------------|
| Extractor   | 100 | 4,000 |  400 (off) | 5–15s async    | 🟡 medium | JSON-schema adherence | Reasoning bloat |
| Reranker    |  30 | 3,000 |  500 (xhigh) | 30s `/digest` | 🔴 high   | Comparison reasoning + domain breadth | None |
| Classifier  |  30 | 1,000 |   10       | 60s IDLE       | 🟡 medium | Pure IFEval            | Reasoning, prose |
| Writer      |   5 | 4,000 |  400       | 8s modal       | 🔴🔴 critical | Natural prose, voice | IFEval rigidity |

Volume model assumes steady-state Day-14 traffic. Calls/day will be lower the
first week.

---

## Benchmark mapping

Each slot has at most two benchmarks that predict real performance. Composite
scores (Artificial Analysis Intelligence Index, etc.) are ignored because they
average across capabilities our workload does not exercise.

| Slot | Primary | Secondary | Rationale |
|------|---------|-----------|-----------|
| Extractor   | BFCL v3 (function-call AST adherence) | IFEval | BFCL rewards raw JSON without conversational wrapping — the exact failure mode we cannot tolerate. |
| Reranker    | MMLU-Pro | Arena Hard v2 | Domain breadth ("Rust backend" ≠ "Rust embedded") + nuanced comparison. |
| Classifier  | IFEval | — | Output must be exactly one of N enum strings, no prose. |
| Writer      | EQ-Bench Creative Writing v3 (Elo) | LMArena Creative slice | Trained-rater creative prose + human preference. |

Benchmarks explicitly **rejected** as misleading for our use:

- **MMLU / GSM8K / MATH / HumanEval** — we do no math or code generation.
- **Composite "Intelligence Index"** — dilutes the signal that matters per slot.
- **RULER @ 128K** — pages are 5–8K tokens; long-context not a constraint.

---

## Pricing snapshot (OpenRouter, 2026-05-18, USD per 1M tokens)

| Slug | Input | Output | Context | Notes |
|------|------:|-------:|--------:|-------|
| `deepseek/deepseek-v4-flash`            | 0.112 | 0.224 | 1M  | MoE 284B/13B active, thinking off/high/xhigh |
| `google/gemini-2.5-flash-lite`          | 0.10  | 0.40  | 1M  | GA, proven JSON schema, IFEval ~92 |
| `google/gemini-3.1-flash-lite`          | 0.25  | 1.50  | 1M  | GA 2026-05-07, thinking levels |
| `google/gemini-3-flash-preview`         | 0.50  | 3.00  | 1M  | Near-Pro reasoning |
| `moonshotai/kimi-k2.6`                  | 0.73  | 3.49  | 262K | Apache 2.0, EQ-Bench Creative 1808 Elo |
| `moonshotai/kimi-k2.5`                  | 0.40  | 1.90  | 262K | Cheaper Kimi variant |
| `x-ai/grok-4.20`                        | 1.25  | 2.50  | 2M  | #3 LMArena Creative, reasoning toggle |
| `anthropic/claude-haiku-4.5`            | 1.00  | 5.00  | 200K | Anthropic family stability |
| `anthropic/claude-sonnet-4.6`           | 3.00  | 15.00 | 200K | EQ-Bench Creative 1991 Elo |
| `anthropic/claude-opus-4.7`             | 5.00  | 25.00 | 200K | EQ-Bench Creative 2216 #1 |

---

## Final picks

### Extractor — `deepseek/deepseek-v4-flash` (reasoning off)

- BFCL strong, IFEval mid-tier (~85), JSON mode + function calling native.
- 1M context handles longest job pages with headroom.
- Reasoning toggle gives a free upgrade path: if extraction failure rate >5%,
  flip `reasoning_effort="high"` for hard pages.
- **Cost**: 100 × (4K × $0.112 + 0.4K × $0.224) / 1M = **$0.054/day**.
- Risk: V4 Flash is reasoning-trained; occasional `<think>` leak even with
  reasoning off. Mitigated by `LLMInvalidJSON` exception in `chat_json` →
  caller retries or falls back to T0 regex output.

### Reranker — `deepseek/deepseek-v4-flash` (reasoning xhigh)

- MMLU-Pro 86.4 (rank 13/124) ≥ floor (80).
- xhigh thinking adds chain-of-thought for nuanced comparisons.
- **Cost**: 30 × (3K × $0.112 + 0.5K × $0.224) / 1M = **$0.013/day**.
- Same model as extractor → one less moving part, one slug to monitor.
- Rejected alternative: Gemini 3 Flash Preview ($0.090/day) — 7x cost for no
  meaningful MMLU-Pro gain at our task complexity.

### Classifier — `google/gemini-2.5-flash-lite`

- Gemini family ≥92 IFEval, well above floor (85).
- No reasoning waste on 5-token enum output.
- **Cost**: 30 × (1K × $0.10 + 0.01K × $0.40) / 1M = **$0.003/day**.
- Rejected alternative: V4 Flash off ($0.003/day) — equal cost but reasoning-leak
  risk on terse outputs.

### Writer — `moonshotai/kimi-k2.6`

- EQ-Bench Creative Writing v3 = 1808 Elo (top 5 in 2026).
- Apache 2.0 → no vendor lock, no policy refusal surprises.
- 262K context handles full resume + opp + history.
- **Cost**: 5 × (4K × $0.73 + 0.4K × $3.49) / 1M = **$0.022/day**.
- Best $/Elo on the leaderboard: 82K Elo per daily dollar.
- Rejected alternatives:
  - `claude-sonnet-4.6` ($0.090/day, 1991 Elo): 4x cost for 10% Elo gain.
    Revisit if recruiter response rate < 5% after 30 days of data.
  - `x-ai/grok-4.20` ($0.030/day, ~1722 Elo): 36% more cost, 5% less Elo.
  - `claude-haiku-4.5` ($0.030/day, ~1650 Elo): worse on every axis.

### Total stack cost

- **Daily**: $0.054 + $0.013 + $0.003 + $0.022 = **$0.092/day**
- **Monthly**: ~**$2.76/mo**
- **Hard cap**: $3.00/day → **32x headroom**

---

## `secrets.yaml` lines

```yaml
openrouter_model_extractor:  deepseek/deepseek-v4-flash
openrouter_model_reranker:   deepseek/deepseek-v4-flash
openrouter_model_classifier: google/gemini-2.5-flash-lite
openrouter_model_writer:     moonshotai/kimi-k2.6
```

---

## Code-side dependencies (already in place)

| File | Change | Reason |
|------|--------|--------|
| `src/common/llm.py` `_PRICING` | Added v4-flash, kimi-k2.5/k2.6, grok-4.20, gemini-2.5/3.x | Accurate cost ledger |
| `src/common/llm.py` `chat()` | New `reasoning_effort` kwarg → OpenRouter `{"reasoning":{"effort":...}}` | Reranker can request xhigh |
| `src/common/llm.py` `chat_json()` | Typed `LLMEmptyResponse`/`LLMSafetyBlock`/`LLMInvalidJSON` | Handle empty/null/safety blocks per-cause |
| `src/ranker/llm_rerank.py` | Passes `reasoning_effort="xhigh"` | Activates V4 Flash thinking |

---

## Re-evaluation triggers (when to revisit these picks)

- **Extractor failure rate > 5%** over 7 days → flip reasoning on for retries
  or escalate to gemini-3-flash-preview.
- **Reranker** empties or wrong rankings → A/B kimi-k2.6 for 7 days, measure
  click-through-to-apply on top-5 opps.
- **Classifier** misclassification rate visible in `#tracker` → upgrade to
  gemini-3.1-flash-lite (~$0.005/day extra).
- **Writer** response rate < 5% after 30 days → A/B claude-sonnet-4.6 (3x cost,
  measure callback uplift).
- **Any preview model deprecated** → swap to GA equivalent (notice usually 30
  days; we have 32x cost headroom for emergency upgrades).

---

## Spec self-review

- Placeholder scan: none.
- Internal consistency: pricing math reconciles to total. Slug names match
  `_PRICING` keys in `src/common/llm.py`.
- Scope: focused on model selection for 4 existing slots; no new slot creation.
- Ambiguity: writer choice locked to kimi-k2.6 per user decision in
  brainstorming session.

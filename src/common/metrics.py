"""Prometheus instrumentation. Exposed via FastAPI /metrics in api-service."""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

REGISTRY = CollectorRegistry(auto_describe=True)

# Pipeline
fetch_latency_seconds = Histogram(
    "fetch_latency_seconds",
    "Per-fetch wall latency.",
    labelnames=("source", "tier"),
    registry=REGISTRY,
)
fetch_errors_total = Counter(
    "fetch_errors_total",
    "Fetch failures.",
    labelnames=("class",),
    registry=REGISTRY,
)
extract_selector_miss_total = Counter(
    "extract_selector_miss_total",
    "Tier-1 selector misses (fell back to T2).",
    labelnames=("source",),
    registry=REGISTRY,
)
extract_tier_distribution = Counter(
    "extract_tier_distribution",
    "Successful extractions by tier.",
    labelnames=("source", "tier"),
    registry=REGISTRY,
)
dedup_hits_total = Counter(
    "dedup_hits_total",
    "Opportunities collapsed by dedup.",
    labelnames=("lane",),
    registry=REGISTRY,
)
score_latency_seconds = Histogram(
    "score_latency_seconds",
    "Ranking latency per opp.",
    registry=REGISTRY,
)
llm_refusals_total = Counter(
    "llm_refusals_total",
    "LLM refused due to cost cap or guardrails.",
    registry=REGISTRY,
)
llm_cost_usd_total = Counter(
    "llm_cost_usd_total",
    "Cumulative LLM cost in USD.",
    labelnames=("kind", "model"),
    registry=REGISTRY,
)
digest_size = Gauge(
    "digest_size",
    "Most recent digest size.",
    registry=REGISTRY,
)
digest_attention_minutes = Gauge(
    "digest_attention_minutes",
    "Estimated minutes spent reviewing last digest.",
    registry=REGISTRY,
)
deliver_success_total = Counter(
    "deliver_success_total",
    "Successful notification deliveries.",
    labelnames=("channel",),
    registry=REGISTRY,
)
applications_sent_total = Counter(
    "applications_sent_total",
    "Applications sent.",
    labelnames=("method",),
    registry=REGISTRY,
)
outcome_events_total = Counter(
    "outcome_events_total",
    "Application outcomes observed via Gmail.",
    labelnames=("type",),
    registry=REGISTRY,
)

# Phase 4 auto-apply policy gate. Labels mirror the auto_apply_audit.decision
# CHECK constraint so the metric set is grep-equivalent to the audit table:
#   submit, submit_deferred_dryrun, refused_disabled, refused_method,
#   refused_source, refused_score, refused_no_score, refused_cap,
#   refused_no_submitter.
auto_apply_decisions_total = Counter(
    "auto_apply_decisions_total",
    "policy.should_auto_submit() outcomes by decision label.",
    labelnames=("decision", "method"),
    registry=REGISTRY,
)
# Phase 4 auto-apply browser pipeline (sidecar drains stream:apply_browser_result).
# `status` matches BrowserApplyResult.status: ok | failed | dry_run_captured.
auto_apply_browser_results_total = Counter(
    "auto_apply_browser_results_total",
    "Browser-driven auto-apply results published by the sidecar.",
    labelnames=("platform", "status"),
    registry=REGISTRY,
)

# Phase 4 Internshala browser-discovery worker (ThinkPad sidecar). One cycle =
# the full dropdown matrix; counters are per-combo where a combo label helps the
# operator localise a selector break or a slow combo.
discovery_cycles_total = Counter(
    "discovery_cycles_total",
    "Discovery cycles completed, by health.",
    labelnames=("healthy",),
    registry=REGISTRY,
)
discovery_cycle_failures_total = Counter(
    "discovery_cycle_failures_total",
    "Whole-cycle failures (cycle raised before producing a report).",
    registry=REGISTRY,
)
discovery_combo_timeouts_total = Counter(
    "discovery_combo_timeouts_total",
    "Per-combo 30s wall-clock timeouts.",
    labelnames=("combo",),
    registry=REGISTRY,
)
discovery_selector_miss_total = Counter(
    "discovery_selector_miss_total",
    "Per-combo selector misses (combo screenshotted + skipped).",
    labelnames=("combo", "key"),
    registry=REGISTRY,
)
discovery_cards_published_total = Counter(
    "discovery_cards_published_total",
    "Cards that cleared floor + dedup and were persisted/published.",
    registry=REGISTRY,
)
discovery_cards_rejected_total = Counter(
    "discovery_cards_rejected_total",
    "Cards dropped pre-publish, by reason (parse|subfloor|dedup).",
    labelnames=("reason",),
    registry=REGISTRY,
)
discovery_combo_duration_seconds = Histogram(
    "discovery_combo_duration_seconds",
    "Per-combo wall time (dropdown drive + scrape).",
    labelnames=("combo",),
    buckets=(1, 2, 5, 10, 15, 20, 30, 60),
    registry=REGISTRY,
)
discovery_heartbeat_timestamp = Gauge(
    "discovery_heartbeat_timestamp",
    "Unix ts of the last discovery worker heartbeat (mirrors Redis discovery:heartbeat).",
    registry=REGISTRY,
)

# CF (7 critical signals)
cf_clearance_solve_rate = Gauge(
    "cf_clearance_solve_rate",
    "Rolling cf_clearance solve rate (0-1).",
    registry=REGISTRY,
)
cf_challenge_appeared_rate = Gauge(
    "cf_challenge_appeared_rate",
    "Rolling rate of CF challenge appearances.",
    registry=REGISTRY,
)
cf_js_challenge_solve_time_ms = Histogram(
    "cf_js_challenge_solve_time_ms",
    "JS challenge solve time (ms).",
    buckets=(100, 250, 500, 1000, 2500, 5000, 10000, 30000),
    registry=REGISTRY,
)
cf_403_with_ray_header_per_hour = Gauge(
    "cf_403_with_ray_header_per_hour",
    "403 responses bearing cf-ray header.",
    registry=REGISTRY,
)
cf_attention_required_body_per_hour = Gauge(
    "cf_attention_required_body_per_hour",
    "Bodies containing 'Attention Required' marker.",
    registry=REGISTRY,
)
cf_checking_browser_persistent_per_hour = Gauge(
    "cf_checking_browser_persistent_per_hour",
    "Persistent 'Checking your browser' interstitial rate.",
    registry=REGISTRY,
)
cf_bm_cookie_rotation_rate = Gauge(
    "cf_bm_cookie_rotation_rate",
    "Rate of __cf_bm cookie rotations.",
    registry=REGISTRY,
)

# Infrastructure
postgres_connections = Gauge(
    "postgres_connections",
    "asyncpg pool active connections.",
    registry=REGISTRY,
)
redis_stream_length = Gauge(
    "redis_stream_length",
    "Length per Redis stream.",
    labelnames=("stream",),
    registry=REGISTRY,
)
identity_checkout_active_count = Gauge(
    "identity_checkout_active_count",
    "Identity checkouts currently leased.",
    registry=REGISTRY,
)
identity_ban_status_count = Gauge(
    "identity_ban_status_count",
    "Identities by ban status.",
    labelnames=("status",),
    registry=REGISTRY,
)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST

# Prometheus scrape setup runbook

## What this enables

Wires the Pi's existing host-level Prometheus daemon into the Cartograph stack so all 24 application metrics emitted by `api-service` — pipeline metrics (`fetch_latency_seconds`, `fetch_errors_total`, `extract_selector_miss_total`, `extract_tier_distribution`, `dedup_hits_total`, `score_latency_seconds`, `llm_refusals_total`, `llm_cost_usd_total`, `digest_size`, `digest_attention_minutes`, `deliver_success_total`, `applications_sent_total`, `outcome_events_total`), the 7 Cloudflare-evasion signals (`cf_clearance_solve_rate`, `cf_challenge_appeared_rate`, `cf_js_challenge_solve_time_ms`, `cf_403_with_ray_header_per_hour`, `cf_attention_required_body_per_hour`, `cf_checking_browser_persistent_per_hour`, `cf_bm_cookie_rotation_rate`), and infrastructure counters (`postgres_connections`, `redis_stream_length`, `identity_checkout_active_count`, `identity_ban_status_count`) — start flowing into Prometheus on a 15s scrape interval and become queryable from the pre-built Grafana dashboard at `grafana/dashboards/agent_jobs.json` (datasource UID `prom`).

Per-worker scrape targets are NOT needed: every Cartograph worker (crawler, extractor, ranker, notifier, gmail-watcher) writes into the shared `prometheus_client` registry inside the api-service process via `src/common/metrics.py`. One scrape covers the whole pipeline.

## Prerequisites

- Prometheus already running on the Pi host (typically as a systemd unit, with config at `/etc/prometheus/prometheus.yml` and listening on `:9091` or another non-9090 port — 9090 is now reserved for api-service's loopback bind).
- Docker + `docker compose` access on the Pi host, and SOPS already wired (`sops exec-env secrets.yaml '...'` works).
- The repo checked out at `/home/lakshit_gupta/coding/Marked_Path/`.

This runbook does NOT cover installing Prometheus itself. If Prometheus isn't running yet, install it first (DietPi software catalogue or upstream binary) before continuing.

## Steps

1. **Activate the compose override on the Pi.** From the repo root:
   ```bash
   cd /home/lakshit_gupta/coding/Marked_Path
   cp compose.override.example.yaml compose.override.yaml
   # or, to keep the activated file pinned to the template:
   # ln -s compose.override.example.yaml compose.override.yaml
   ```
   `compose.override.yaml` is gitignored — it stays Pi-local.

2. **Recreate api-service so the new port mapping takes effect:**
   ```bash
   sops exec-env secrets.yaml 'docker compose up -d --force-recreate api-service'
   ```
   Confirm the new bind exists:
   ```bash
   docker compose port api-service 9090
   # Expected: 127.0.0.1:9090
   ```

3. **Open the existing Prometheus config** on the Pi:
   ```bash
   sudo $EDITOR /etc/prometheus/prometheus.yml
   ```

4. **Paste the scrape job** from `deploy/prometheus_cartograph.yml` under the existing `scrape_configs:` key. Preserve two-space YAML indentation — the entry is a single list item beginning with `- job_name: cartograph`. The end result looks like:
   ```yaml
   scrape_configs:
     - job_name: node_exporter
       # ...existing entries...

     - job_name: cartograph
       scrape_interval: 15s
       scrape_timeout: 10s
       metrics_path: /metrics
       static_configs:
         - targets: ['127.0.0.1:9090']
           labels:
             project: cartograph
             env: pi-prod
   ```

5. **Reload Prometheus** without dropping the metric series store:
   ```bash
   sudo systemctl reload prometheus
   # or, if not managed by systemd:
   sudo kill -HUP "$(pidof prometheus)"
   ```

## Smoke check

Run these two commands on the Pi host immediately after step 5.

First, prove api-service is exposing metrics on the new loopback bind:
```bash
curl -fs http://localhost:9090/metrics | grep -E "fetch_latency_seconds|llm_cost_usd_total" | head -5
```
Expected: at least two non-empty lines of Prometheus text format, e.g.
```
# HELP fetch_latency_seconds Per-fetch wall latency.
# TYPE fetch_latency_seconds histogram
fetch_latency_seconds_bucket{source="greenhouse",tier="0",le="0.005"} 0.0
# HELP llm_cost_usd_total Cumulative LLM cost in USD.
# TYPE llm_cost_usd_total counter
```

Second, prove Prometheus itself accepted the new scrape job and is hitting the target. The Pi's Prometheus typically listens on `:9091` (since `:9090` is now api-service); if you run it on a different port, substitute below:
```bash
curl -fs http://localhost:9091/api/v1/targets | jq '.data.activeTargets[] | select(.labels.job == "cartograph") | .health'
```
Expected output:
```
"up"
```

If you see `"down"`, inspect the same record for the `lastError` field:
```bash
curl -fs http://localhost:9091/api/v1/targets | jq '.data.activeTargets[] | select(.labels.job == "cartograph") | {health, lastError, lastScrape}'
```
Common failure: Prometheus runs as user `prometheus` in a different network namespace (e.g. inside its own container). In that case loopback inside Prometheus isn't the host's loopback — either rebind Prometheus to host networking, or expose api-service on a Tailscale-bound port and point the scrape there.

## Trade-offs + Phase 4 hardening

**Why host-localhost in v1.** The Pi runs as a single-user system today (multi-user is locked to Phase 4 per CLAUDE.md). Binding api-service to `127.0.0.1:9090` keeps the metrics endpoint reachable to local Prometheus while invisible to LAN, Tailscale, and Cloudflared. The metrics endpoint has no auth — it exposes counters/gauges that, while not secret in the cryptographic sense, do reveal LLM spend, identity ban counts, and source health that should never reach the public internet. The simpler alternative (joining Prometheus to the `internal` Docker network) was rejected because it requires Prometheus to run as a container alongside Cartograph, but Prometheus on this Pi is a host systemd unit shared with Jellyfin and node_exporter.

**Phase 4 upgrade path.** When multi-user lands and metrics need to be reachable from a remote Prometheus (e.g. a central observability VPS, or a second Pi), swap the bind in `compose.override.yaml` from `127.0.0.1:9090:9090` to a Tailscale IP (e.g. `100.x.y.z:9090:9090` using the Pi's tailnet IP from `tailscale ip -4`). The scrape job's `targets:` entry moves to that same Tailscale IP, the scraping Prometheus must be in the same tailnet, and a Tailscale ACL restricts which nodes can hit port 9090. At that point, also add a Prometheus `basic_auth` or `authorization` block since Tailscale gives network reachability but not request-level authentication. The scrape job snippet at `deploy/prometheus_cartograph.yml` is forward-compatible with this change — only the `targets:` line moves.

# CDP sidecar — Phase 4.4 deferred extension

Status: **dormant addon**. Free-only by design — no paid service involved.
Activate only when sustained T2 (camoufox) failures appear in production.

## When (and only when) to activate

Watch these Prometheus signals over a rolling 7-day window:

- `cf_clearance_solve_rate{tier="t2"}` falls below 0.50 for the lowest two
  source priorities AND
- `cf_challenge_appeared_rate{tier="t2"}` rises above 0.40 AND
- `cf_bm_cookie_rotation_rate` rises above 0.30 (camoufox's residency
  fingerprint is being burned).

If only one or two of these tip, raise camoufox's behavioural-nudge knobs
first (cursor variance, page-think delay, scroll cadence). The CDP sidecar
is a hardware-cost step (a host-side mini-PC) — never the first move.

Two of the three conditions must hold simultaneously for ≥7 days before
activation is considered an option. Single-day spikes have always been
re-baselined by camoufox's behavioural nudges in production so far.

## Hardware target

The host is the user's spare **Lenovo ThinkCentre M720q** (already on the
home LAN, already on Tailscale, x86_64 — so Chrome ARM ports are a
non-issue).  No new spend. The Pi 5 keeps running the rest of the stack;
the sidecar joins the Tailscale mesh as `cdp-sidecar.<tailnet>` and
exposes one port: `9222` for the Chrome DevTools Protocol.

## Activation flow (when the time comes)

1. Flip the feature flag in SOPS-encrypted `secrets.yaml`:
   ```yaml
   mp_cdp_sidecar_enabled: "true"
   cdp_sidecar_endpoint: "http://cdp-sidecar.tailnet:9222"
   ```
2. Deploy `docker/cdp-sidecar.Dockerfile` (NOT yet authored — that is the
   first build step once activation is ratified) onto the M720q. It runs
   Chrome (or ungoogled-chromium) with `--remote-debugging-port=9222` and
   `--remote-debugging-address=0.0.0.0`. Tailscale ACLs scope the port to
   the Pi only.
3. Add a `cdp_fetcher.py` tier under `src/fetchers/browser/` that speaks
   CDP over WebSocket and slots into `tier_chain` as `T3` (between
   `camoufox` and the paid premium ladder, which today is empty).
4. Update `config/cf_tier_chain.yaml` to route the failing sources
   through tier `[0,1,2,3]`. Roll out per-source, NOT globally — one
   stubborn source at a time keeps blast radius small.

## Why this is NOT shipped today

User directive (2026-05-19): "skip resident proxy not banned yet. also i
am not paying until it is free if free add as addon or a switch". CDP
sidecar fits the addon shape — the hardware is free, the runtime is free,
the activation is a flag flip — so it lives here as a documented extension
rather than as deleted code.

If the hardware were ever paid (cloud VPS, dedicated rental), this extension
would convert into a hard refusal. The free-only constraint holds: nothing
in this addon may pull in a paid dependency without first re-running the
scope decision with the user.

## Out of scope

- Provisioning Chrome on the M720q (manual one-time set-up; runbook
  lands when activation is requested).
- A managed CDP service like Browserless.io — that is paid.
- Cloud-hosted Chrome on AWS Lambda / Fly.io free tiers — egress + cold-
  start costs invalidate the free claim once load grows.

## Reverting

Flip `mp_cdp_sidecar_enabled` back to `"false"` and restart the relevant
workers. The fetcher tier chain falls back to `[0,1,2]` and the sidecar
host goes idle. No DB migration involved — the addon never persisted
anything.

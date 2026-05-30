# Internshala browser discovery — operator runbook

Phase 4 discovery lane for Internshala. A camoufox-driven worker on the
ThinkPad sidecar drives Internshala's own dropdown UI (category, stipend,
work-mode), scrapes the resulting listing cards, and **post-filters in
code** against the real comp floor (default ₹30,000/month) that the
Internshala dropdown — which caps at "above ₹10,000" — cannot enforce.
Survivors are deduped against Redis and published onto `stream:rank` via
the existing `persist_and_publish`, exactly like the extractor path.

This worker **replaces** the Pi-side `curl_cffi` URL crawler for
Internshala. The crawler is not deleted — it is left dormant in the tree
and re-armed by a single column flip (`sources.discovery_method`); see
[Rollback](#rollback).

Pre-reqs (everything below already shipped):

- V025 (`sources.discovery_method`) + V026 (`discovery_cycle_log`)
  migrations applied on the Pi.
- `compose.sidecar.yaml` present on the ThinkPad with the autossh tunnel
  to the Pi's loopback Redis + Postgres already up (see
  `docs/runbooks/sidecar_setup.md`). The discovery worker reuses that
  tunnel — no new SSH forwards.
- `notify_discovery_cycle` registered in the Discord bot dispatch table
  (`src/notifiers/discord/bot.py` → `"discovery_cycle_report"`).
- A healthy `identities` row with `platform='internshala'`, cookies
  populated, `ban_status='healthy'` — the same identity the auto-apply
  worker uses (discovery + apply share one row in Phase 1). Confirm via:
  ```sql
  SELECT id, account_label, ban_status, last_used_at
  FROM identities WHERE platform='internshala';
  ```
- `LIBSODIUM_MASTER_KEY_HEX` present in `.env.sidecar` on the ThinkPad —
  cookies are decrypted locally on the spare; the Pi never reads decrypted
  Internshala session material.

---

## Architecture

```
ThinkPad sidecar (already runs apply-browser-worker)
┌──────────────────────────────────────────────────────────────┐
│ internshala-discovery-worker (single replica)                │
│  ├─ loop: run_cycle() → sleep(max(IDLE_SEC - elapsed, 0))    │
│  │   IDLE_SEC = 180 (testing) | 1800 (prod) — env / prefs    │
│  │   on whole-cycle error: exponential backoff from          │
│  │   BACKOFF_SEC (600s), capped at 1800s                     │
│  ├─ heartbeat task: SET discovery:heartbeat <ts> EX 90 / 30s │
│  ├─ identity leased at boot via autossh tunnel + asyncpg     │
│  │   → identity_vault.checkout(platform='internshala')       │
│  │   → libsodium decrypt cookies LOCALLY on the ThinkPad     │
│  ├─ browser: BrowserEngine (CamoufoxEngine impl)             │
│  │   • one Firefox held alive, fresh context per combo       │
│  │   • engine.restart() every MAX_CYCLES_PER_ENGINE cycles   │
│  ├─ per-combo (12 wfh combos × up to 3 Load-more pages):     │
│  │   ├─ asyncio.wait_for(run_combo, timeout=30)              │
│  │   ├─ goto(/internships/, wait_until=domcontentloaded)     │
│  │   ├─ dismiss onboarding modal (best-effort)               │
│  │   ├─ click dropdowns: stipend → category → wfh chip       │
│  │   ├─ wait_for_selector(card_root, 8s)  [NOT networkidle]  │
│  │   ├─ scrape cards from DOM (selectolax)                   │
│  │   ├─ parse stipend → reject < COMP_FLOOR_INR              │
│  │   ├─ Redis SET internshala:seen:<sha> EX 86400 NX (dedup) │
│  │   ├─ persist_and_publish(opp) → stream:rank               │
│  │   └─ on selector miss: screenshot + DOM clip, skip combo  │
│  ├─ end-of-cycle:                                            │
│  │   ├─ INSERT into discovery_cycle_log                      │
│  │   └─ XADD stream:notify → discovery_cycle_report card     │
│  └─ shutdown: SIGTERM → finish current combo → exit 0        │
└──────────────────────────────────────────────────────────────┘
                           │ autossh tunnel (Pi loopback 5432, 6379)
                           ▼
Pi 5 (downstream unchanged + 2 new migrations)
┌──────────────────────────────────────────────────────────────┐
│  jobs-scheduler:    emits stream:fetch ONLY where             │
│                     discovery_method = 'http_curl' (V025)     │
│  extractor-worker:  no change (this path bypasses it)        │
│  ranker-worker:     scores incoming opps from stream:rank    │
│  notifier-discord:  + post_discovery_cycle handler           │
│  applier-worker:    no change (existing auto-apply flow)     │
└──────────────────────────────────────────────────────────────┘
```

**Invariants:**

- The Pi never touches Internshala's web UI for discovery. The ThinkPad
  IP is the only browse surface.
- Sub-floor cards never reach `stream:rank`. The floor is enforced in the
  scraper, before publish.
- No raw card reaches Postgres without a 24-hour Redis uniqueness check.
- All browser interaction routes through the `BrowserEngine` Protocol.
  Camoufox is one impl; the worker holds no direct camoufox import.
- Selectors live in YAML, not Python constants, and hot-reload on SIGHUP.

---

## RECON-FIRST — do this before the first deploy

**This is the single most important operator step.** The selectors in
`config/sources/internshala_selectors.yaml` ship as **recon placeholders**
lifted from the legacy crawler and the design spec's best guess. They are
**not** verified against a live page. Internshala renders its
category/location filters with the jQuery **Chosen** plugin (not native
`<select>` elements), and the stipend control may be a slider rather than a
radio group — so the placeholders are very likely wrong.

The worker **refuses to boot** while `version: "RECON_PENDING"` is still in
the selector YAML, unless you set `INTERNSHALA_ALLOW_RECON_PENDING=1`
(that escape hatch exists only so a `--dry-run` smoke can run against
placeholders). Production deploy requires capturing the real selectors and
bumping `version` away from `RECON_PENDING`.

### How to capture the live selectors

1. SSH to the ThinkPad and open a real desktop browser (NOT the sidecar
   container — you need devtools):

   ```bash
   ssh remote_lakshit_gupta@<thinkpad-ip>
   xdg-open https://internshala.com/internships/
   ```

   Log in with the same Internshala account the identity vault holds, so
   the DOM you inspect matches what the worker (using that account's
   cookies) will see.

2. Open devtools → Elements and inspect each control below. For each, copy
   a stable CSS selector and paste it into the matching key in
   `config/sources/internshala_selectors.yaml`.

   **Dropdown triggers and option lists** (`selectors.dropdown.*`):
   - `stipend_button` — the control that opens the stipend filter.
   - `stipend_option_above_10000` — the "above ₹10,000" choice. If stipend
     is a **slider**, record the slider handle / input instead and note it
     in a YAML comment; the worker's `drive_dropdowns` clicks the button
     then the option, so a slider needs the selectors that map to those two
     clicks (or the option-key left empty if a single drag suffices).
   - `category_button` — the Chosen trigger for the category filter
     (typically a `*_chosen .chosen-single` element).
   - `category_options` — the rendered option rows
     (`*_chosen .chosen-results li`). The worker appends
     `>> text=<category>` to pick the row by visible text, so this must
     select the full `<li>` option list.
   - `work_mode_wfh_chip` — the "Work from home" filter chip / label.
   - `location_button` / `location_options` — the Chosen location widget
     (only needed once on-site combos land in Phase 1.5, but capture them
     now while you are in the DOM).

   **Listing card fields** (`selectors.listing.*`):
   - `card_root` — the repeating element that wraps one listing
     (`div.individual_internship`). Everything else is resolved per-card.
   - `card_title`, `card_company`, `card_location`, `card_stipend`,
     `card_apply_link`, `card_posted_relative` — the fields inside one card.

   **Pagination** (`selectors.paginate.*`):
   - `load_more_button` — the control the worker clicks to load the next
     page of results.
   - `list_end_marker` — the "no results" / empty-state element that tells
     the worker to stop paging early.

   **Failure markers** (top-level `selectors.*`):
   - `login_marker` — an element present only on the login page / modal.
     Its presence (or a `/login` URL) aborts the cycle.
   - `captcha_marker` — a captcha / interstitial iframe or container. Its
     presence aborts the cycle. The worker **never** attempts to solve a
     challenge.
   - `modal_dismiss` — the close button on Internshala's onboarding modal
     (best-effort; absence is the common, healthy case).

3. Bump the version away from the sentinel:

   ```yaml
   version: "RECON_PENDING"        # ← change this
   version: "2026.05.30.v1"        # ← to a dated identifier
   ```

   Bump `version` on **every** subsequent selector edit too — the worker
   logs `selectors_reloaded old=<v> new=<v>` on each SIGHUP, and the value
   is recorded in every `discovery_cycle_log` row.

4. Validate against a live page **before** the long-running deploy, using
   the CLI one-shot (placeholders allowed only here):

   ```bash
   # On the ThinkPad, inside the worker image / repo env:
   INTERNSHALA_ALLOW_RECON_PENDING=1 \
     mp internshala-discover --once --combo backend-development-wfh --dry-run
   ```

   > `mp` is this repo's admin CLI. The console-script entry point in
   > `pyproject.toml` is `carto` (`carto internshala-discover ...`); both
   > resolve to the same `src.cli.main:cli`, and `python -m src.cli.main
   > internshala-discover ...` is the import-path equivalent if neither
   > script is on `PATH`.

   A clean run prints `[dry-run] <title> @ <company> ...` lines for at
   least one card and exits 0. A `selector_miss: <key>` in the logs (plus a
   screenshot under `/tmp/discovery/miss/`) tells you exactly which key is
   still wrong — fix it and re-run.

Do not skip recon. A wrong selector means a combo silently scrapes nothing
until the miss surfaces in `#🛠-source-health`.

---

## Deploy

### 1. Build + ship the image (x86_64 native)

The ThinkPad is x86_64, same arch as the dev box — no QEMU. The discovery
image extends the apply-browser image, so build that chain first if it is
not already present on the ThinkPad.

```bash
# Dev box (x86_64 native)
docker buildx build \
  --platform linux/amd64 \
  --output type=docker \
  --build-arg BASE_IMAGE=cartograph-apply-browser:latest \
  -t marked_path-discovery:latest \
  -f docker/discovery.Dockerfile .

# Save + ship + load on the ThinkPad
docker save marked_path-discovery:latest | xz -1 > /tmp/discovery.tar.xz
rsync --partial /tmp/discovery.tar.xz remote_lakshit_gupta@<thinkpad-ip>:/tmp/
ssh remote_lakshit_gupta@<thinkpad-ip> 'xz -d < /tmp/discovery.tar.xz | docker load'
```

Alternatively, build natively on the ThinkPad itself (it runs the same
arch): `git pull && docker compose -f compose.sidecar.yaml build
internshala-discovery-worker`.

### 2. Apply the Pi-side migrations (V025 + V026)

```bash
ssh dietpi@192.168.1.240
cd /home/dietpi/coding/Cartograph
git pull        # so the tools image build context has V025 + V026

sops exec-env secrets.yaml 'docker compose run --rm \
  -v /home/dietpi/coding/Cartograph/migrations:/app/migrations:ro \
  tools python -m src.cli.main migrate'
```

A failed migrate is NOT a volume-wipe situation — each V*.sql self-rolls
back. Fix and re-run `make migrate`; it resumes at the failed file.

### 3. Verify the source flipped to camoufox

```bash
sops exec-env secrets.yaml 'docker exec -i cartograph-postgres-1 \
  psql -U "$postgres_user" -d "$postgres_db" \
  -c "SELECT slug, discovery_method FROM sources WHERE slug='\''in_internshala'\'';"'
# expect: in_internshala | camoufox_dropdown
```

`camoufox_dropdown` confirms the Pi scheduler has stopped emitting
`stream:fetch` for Internshala (`emit_for_active_sources` filters on
`discovery_method = 'http_curl'`).

### 4. Launch the worker on the ThinkPad

```bash
ssh remote_lakshit_gupta@<thinkpad-ip>
cd /home/remote_lakshit_gupta/Marked_Path
git pull

sops exec-env .env.sidecar 'docker compose -f compose.sidecar.yaml up -d \
  --force-recreate internshala-discovery-worker'

# Tail
docker compose -f compose.sidecar.yaml logs -f internshala-discovery-worker
```

Expect on first boot, in order:
- `discovery_config_loaded ... selectors_version=<your dated version>`
  (NOT `RECON_PENDING` — if it refuses to boot with a `ReconPendingError`,
  you skipped the recon step).
- `discovery_worker_ready worker_id=discovery-<host>-<pid> combos=12`.
- A heartbeat key appearing in Redis within ~30 s.
- The first `discovery_cycle_report` card in `#🛠-source-health` within a
  few minutes.

---

## Operations

All commands run on the **ThinkPad** unless marked **(Pi)**.

| Action | Command |
|---|---|
| Start worker | `docker compose -f compose.sidecar.yaml up -d internshala-discovery-worker` |
| Stop worker | `docker compose -f compose.sidecar.yaml stop internshala-discovery-worker` |
| Restart worker | `docker compose -f compose.sidecar.yaml restart internshala-discovery-worker` |
| Tail logs | `docker compose -f compose.sidecar.yaml logs -f --tail 100 internshala-discovery-worker` |
| Disable discovery (resume URL crawler) | **(Pi)** `UPDATE sources SET discovery_method='http_curl' WHERE slug='in_internshala';` — see [Rollback](#rollback) |
| Re-enable discovery | **(Pi)** `UPDATE sources SET discovery_method='camoufox_dropdown' WHERE slug='in_internshala';` |
| Change cadence (testing → prod) | Set `INTERNSHALA_IDLE_SEC: "1800"` in `compose.sidecar.yaml` and recreate, **or** set `discovery.internshala.idle_sec: 1800` in `config/profile/prefs.yaml` (prefs wins over env) and restart |
| Change comp floor | Set `INTERNSHALA_COMP_FLOOR_INR` in `compose.sidecar.yaml`, **or** `discovery.internshala.comp_floor_inr` in `prefs.yaml` (prefs wins) — then restart |
| Change engine restart cadence | `INTERNSHALA_MAX_CYCLES_PER_ENGINE` (env) or `discovery.internshala.max_cycles_per_engine` (prefs) |
| Hot-reload selectors (no rebuild) | Edit `config/sources/internshala_selectors.yaml` (bump `version`), then `docker compose -f compose.sidecar.yaml exec internshala-discovery-worker kill -HUP 1` (or `kill -HUP <pid>` for the in-container PID) |
| Force a one-shot cycle | `mp internshala-discover --once --combo backend-development-wfh --dry-run` |
| Watch cycle reports | `#🛠-source-health` Discord channel — one card per cycle, healthy or not |
| Inspect cycle history | **(Pi)** see `discovery_cycle_log` query below |
| Queue depth from spare | `redis-cli -h 127.0.0.1 -a "$REDIS_PASSWORD" --no-auth-warning XLEN stream:rank` |

### Cadence

The worker is **loop-with-sleep, not cron**. After each cycle it sleeps
`max(IDLE_SEC - elapsed, 0)` seconds. `IDLE_SEC=180` (3 min) is the
**testing** cadence — verbose, useful for proving the pipeline. Raise to
`1800` (30 min) for **production** once the remote pool is verified.
`prefs.yaml` overrides the env var when set, so the operator can retune by
editing one checked-in file. A cycle that runs longer than `IDLE_SEC`
sleeps zero seconds and runs back-to-back — the heartbeat keeps liveness
intact; this is intentional back-pressure-stable behaviour.

### `--combo` argument

The `--combo` filter takes the **generated combo name**, not the bare
category. The name is the lowercased category with spaces / punctuation
slugified, suffixed with the work mode — e.g. `Backend Development` +
`wfh` → `backend-development-wfh`; `Artificial Intelligence (AI)` + `wfh`
→ `artificial-intelligence-ai-wfh`. An unknown name fails loudly and
prints the known set.

### Inspect `discovery_cycle_log` (Pi)

```sql
SELECT started_at, duration_sec, combos_succeeded || '/' || combos_attempted AS combos,
       cards_scraped, cards_published,
       cards_rejected_subfloor AS subfloor, cards_rejected_dedup AS dedup,
       cards_rejected_parse AS parse, healthy,
       combo_timeouts, selector_misses, selectors_version
FROM discovery_cycle_log
WHERE source_slug = 'in_internshala'
ORDER BY started_at DESC
LIMIT 20;
```

---

## Verification window (24 h)

| Hour | Expected signal |
|---|---|
| T+0 | Heartbeat key `discovery:heartbeat` present in Redis; first cycle starts |
| T+5 min | First `discovery_cycle_log` row inserted; first `discovery_cycle_report` card in `#🛠-source-health` |
| T+15 min | ≥ 3 cycles completed; at least one with ≥ 10 cards published (testing cadence) |
| T+1 h | `opportunities.comp_min` for Internshala skewed ≥ 30k; no civil / mechanical / marketing noise |
| T+4 h | `/auto-apply-inter preview 10` returns backend / ML candidates with declared ≥ 30k stipend |
| T+24 h | No `discovery_dry_streak` and no `internshala_session_expired` alerts in `#🔔-alerts` |

Spot-check the floor enforcement directly (Pi):

```sql
SELECT count(*) AS leaked
FROM opportunities o JOIN sources s ON s.id = o.source_id
WHERE s.slug = 'in_internshala' AND o.comp_min < 30000;
-- expect 0 for opps published by the discovery worker
```

---

## Troubleshooting

The worker writes per-cycle outcomes to `#🛠-source-health` and escalates
**hard failures** (any selector miss, or a dry+unhealthy cycle) to
`#🔔-alerts` with the screenshot attached. Map the symptom to the action:

| Symptom | Meaning | Operator action |
|---|---|---|
| `selector_miss: <key>` in logs + screenshot in `#🛠-source-health` / `#🔔-alerts` | A required dropdown / card / pagination selector no longer matches (Internshala moved the DOM). The affected combo is skipped; other combos continue. | Open the screenshot + the clipped DOM under `/tmp/discovery/miss/<combo>_<ts>.{png,html}`. Recon the new selector (see [RECON-FIRST](#recon-first--do-this-before-the-first-deploy)), edit the key in `internshala_selectors.yaml`, **bump `version`**, then SIGHUP — no rebuild. |
| `discovery_challenge_detected kind=captcha` + red embed, cycle `healthy=false` | A captcha / interstitial appeared. The worker aborts the cycle and **never** tries to solve it. | Cool off: stop the worker for a while, verify the account in a real browser on the ThinkPad, let traffic settle. Re-enable. If it recurs, raise `IDLE_SEC` to reduce request rate. |
| `discovery_challenge_detected kind=login_redirect` (or `kind=login`) | The session cookie expired — Internshala bounced the worker to `/login`. Cycle aborts `healthy=false`. | Re-run identity warmup so a fresh cookie is written to the vault, then restart the worker. The same `platform='internshala'` identity row backs discovery and auto-apply, so a warmup fixes both. |
| `discovery_dry_streak` alert | ≥ 3 consecutive **healthy** cycles published **zero** cards. Usually an Internshala UX change that broke parsing without a hard selector miss, or the comp floor is set too high for current listings. | Run `mp internshala-discover --once --combo backend-development-wfh --dry-run` and read the per-card stdout. If cards parse but all reject sub-floor, the floor may be too aggressive; if nothing parses, recon the listing-card selectors. |
| Healthcheck failing / heartbeat stale | No `discovery:heartbeat` key for > 90 s — the worker stalled or crashed. The Pi alerter pages on this. | `docker compose -f compose.sidecar.yaml logs --tail 200 internshala-discovery-worker` to find the stall; restart the worker. Check the autossh tunnel is up (`sudo systemctl status carto-tunnel`) since a dead tunnel breaks the Redis SET. |
| `discovery_combo_timeout combo=<name>` | One combo exceeded the 30 s wall-clock cap (`asyncio.wait_for`). Its subtree is killed and the cycle continues. | Occasional timeouts under a slow page are normal. Persistent timeouts on one combo point at a slow / mis-targeted dropdown click — recon that combo's selectors. |
| `discovery_no_identity` + exit code 2 | No healthy `platform='internshala'` identity was available to lease at boot. | Confirm an `identities` row exists with `ban_status='healthy'` and cookies populated; the container will restart and retry the checkout. |
| Worker refuses to boot with `ReconPendingError` | `internshala_selectors.yaml` still has `version: RECON_PENDING`. | Complete recon and bump the version. Only for a placeholder `--dry-run` smoke, set `INTERNSHALA_ALLOW_RECON_PENDING=1`. |
| `selectors_reload_failed` after a SIGHUP | The edited YAML is malformed. The worker **keeps the old selectors** and logs the error — discovery stays online. | Fix the YAML syntax and SIGHUP again. Confirm `selectors_reloaded old=<v> new=<v>` follows. |
| `discovery_cycle_log_insert_failed` | The Postgres INSERT failed (tunnel hiccup), but the Discord card is still posted best-effort. | Usually transient. If persistent, check the autossh tunnel and the Pi's Postgres health. |

**On `networkidle`:** the worker intentionally does **not** gate readiness
on `networkidle`. Internshala fires background telemetry that keeps the
connection busy indefinitely, so a `networkidle` wait would hang every
combo until the 30 s timeout. Readiness is the **first card becoming
visible** (`wait_for_selector(card_root, 8s)`), with `goto(...,
wait_until="domcontentloaded")`. Do not "fix" a slow combo by switching to
`networkidle`.

---

## Rollback

`sources.discovery_method` is the kill switch. To stop browser discovery
and resume the original Pi-side URL crawler (Pi):

```sql
UPDATE sources SET discovery_method = 'http_curl' WHERE slug = 'in_internshala';
```

The Pi scheduler picks Internshala back up within one scheduler tick — its
`emit_for_active_sources` query starts matching the row again and emits
`stream:fetch` tasks for the URL crawler, which is still in the tree
(dormant, not deleted). The ThinkPad discovery worker becomes a cheap
no-op: its 24-hour Redis dedup TTL means a re-seen card is dropped before
any LLM or persist cost, so you can leave the worker running or stop it at
leisure.

Deletion of the legacy URL crawler
(`src/sources/india/internshala.py` and
`config/sources/internshala_filters.yaml`) is **deferred until 7 days** of
clean `discovery_method = 'camoufox_dropdown'` operation. Keep the dormant
crawler until then so rollback stays a single column flip.

To fully stop the worker on the ThinkPad:

```bash
docker compose -f compose.sidecar.yaml stop internshala-discovery-worker
```

This is invisible to every Pi-side service.

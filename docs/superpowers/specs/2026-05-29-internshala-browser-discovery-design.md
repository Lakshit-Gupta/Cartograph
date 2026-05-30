# Internshala Browser Discovery — Design

> Status: DRAFT (post-brainstorm, awaiting adversarial multi-lens review)
> Date: 2026-05-29
> Author: brainstorming session, ratified amendments from senior-architect audit
> Replaces: `src/sources/india/internshala.py` (URL-pattern crawler) + `config/sources/internshala_filters.yaml` (URL combo matrix)

---

## Problem

Cartograph today discovers Internshala opps via Pi-side `curl_cffi` crawl of URL paths shaped `/internships/<category>-internships/stipend-30000/page-N/`. Empirically the `/stipend-30000/` URL suffix does **not** constrain Internshala's response to ≥₹30,000/month listings — sub-₹10k mechanical-engineering opps still leak through. The Internshala UI dropdown for stipend exposes only one filter — "above ₹10,000" — and the dropdown-emitted URL likewise caps at `/stipend-10000/`. There is no URL-level mechanism to enforce a ≥₹30k floor.

The user has stated, explicit verbatim, "i want a agent or bot which can first collect the data use the internshala finder to get me a job either by using intenrshala own drop down meneu not limited to their fuzzy search". A browser-driven discovery agent that drives Internshala's dropdown UI, scrapes the resulting listing cards, and **post-filters in code** against the real user comp floor (₹30,000/month internship; ₹50,000/month or ₹6 LPA job) is required.

Concurrent constraint: Pi cannot host the browser. Detection surface for both crawling and auto-applying must stay off the Pi IP. The ThinkPad sidecar (Pop OS 24.04, user `remote_lakshit_gupta`, x86_64, LAN-resident, autossh tunnel to Pi loopback Postgres + Redis already provisioned for the Phase 4 auto-apply browser worker) is the chosen host.

---

## Locked decisions (from brainstorming Q1–Q4)

| Decision | Choice | Reason |
|---|---|---|
| Topology | **Replace URL crawler entirely** | Single Internshala source path → less code, no dedup overhead. Cost = browser-agent failure stops Internshala ingest entirely; mitigated by selector hot-reload + health alerting. |
| Runtime host | **ThinkPad sidecar** | Reuses existing camoufox stack, identity vault, autossh tunnel. Detection surface stays off Pi IP. |
| Filter stage | **Pre-publish in scraper** | Browser agent parses stipend per card and rejects sub-floor before publishing. Keeps `stream:rank` and DB clean. |
| Phase 1 scope | **Internships only; jobs Phase 2** | Jobs page DOM + LPA-vs-stipend taxonomy differs. Ship + verify internships clean first. |
| Crawl mechanic | **Approach A — Pure UI dropdown drive** | Camoufox interacts with dropdown UI for every page-load. Maximum stealth. ~3–4 min per cycle accepted. |

---

## Senior-architect amendments (folded in)

1. **No cron.** Loop-with-sleep inside worker. `IDLE_SEC=180` testing, `IDLE_SEC=1800` prod. Single-consumer, no tick-collision risk.
2. **Per-combo 30 s wall-clock timeout.** `asyncio.wait_for(combo, timeout=30)`. On timeout: kill subtree, screenshot, increment metric, continue.
3. **Redis-set dedup before persist.** `SET internshala:seen:<sha256(canonical_url)> 1 EX 86400 NX`. ~1 ms per card. Prevents LLM cost balloon under 3-minute testing cadence.
4. **Reuse `persist_and_publish`.** Browser agent calls existing `src/extractors/persist.py:persist_and_publish(opp)` → upserts on `opportunities.canonical_url UNIQUE` + emits `stream:rank`. No extractor in loop. Tier-1 already done; tier-2 LLM enrichment becomes opt-in per-opp Phase 2.
5. **`sources.discovery_method` enum.** V025 migration adds the column; Pi scheduler gates emission on `discovery_method='http_curl'`. Surgical decouple; URL crawler can be deleted in the same PR.
6. **Selector hot-reload via SIGHUP.** `INTERNSHALA_DISCOVERY_SELECTORS` lives in `config/sources/internshala_selectors.yaml`. Worker loads at boot and on `SIGHUP`. No rebuild-and-ship cycle for selector fixes.
7. **CLI for fast iteration.** `mp internshala-discover --once --combo backend-development --dry-run`. Single combo, no publish, single cycle, exits 0/1.
8. **`BrowserEngine` Protocol.** All browser work routes through `src/fetchers/browser/engine.py:BrowserEngine`. `CamoufoxEngine` is the only impl Phase 1; `NodriverEngine` / `PatchrightEngine` slot in by config when the deferred browser refresh triggers.
9. **`DiscoveryCycleReport` per cycle.** Written to `discovery_cycle_log` (V026). Discord short-form posted to `#🛠-source-health` every cycle, healthy or not. User sees failure within 3 min at testing cadence.
10. **Heartbeat to Redis.** `SET discovery:heartbeat <ts> EX 90` every 30 s. Pi alerter pages on missing heartbeat > 90 s.
11. **Stipend parser regression corpus.** ≥100 real Internshala stipend strings pinned in `tests/fixtures/stipend_strings.json`. Parser change must keep the corpus green.

---

## Section 1 — Architecture

```
ThinkPad sidecar (already runs apply-browser-worker)
┌──────────────────────────────────────────────────────────────┐
│ NEW: internshala-discovery-worker (single replica)           │
│  ├─ loop: cycle() → sleep(max(IDLE_SEC - elapsed, 0))        │
│  │   IDLE_SEC = 180 (testing) | 1800 (prod) — env-driven     │
│  │   on cycle error: sleep(BACKOFF_SEC=600), back off twice  │
│  ├─ heartbeat task: SET discovery:heartbeat <ts> EX 90 / 30s │
│  ├─ identity leased at boot via autossh tunnel + asyncpg     │
│  │   → identity_vault.checkout('in_internshala')             │
│  │   → libsodium decrypt cookies LOCALLY on ThinkPad         │
│  ├─ browser: BrowserEngine (CamoufoxEngine impl)             │
│  │   • warm context, restart every 10 cycles                 │
│  │   • restart on any IPC failure                            │
│  ├─ per-combo (12 combos × 3 pages = 36 page-loads):         │
│  │   ├─ asyncio.wait_for(run_combo(combo), timeout=30)       │
│  │   ├─ click dropdowns (stipend, category, work_mode, city) │
│  │   ├─ wait_for_selector(card_root, 8s)  [NOT networkidle] │
│  │   ├─ verify URL shape sanity                              │
│  │   ├─ scrape cards from DOM (3 pages via Load-more)        │
│  │   ├─ parse stipend → reject < 30k INR/month               │
│  │   ├─ Redis SET dedup check (24h TTL, NX semantics)        │
│  │   ├─ persist_and_publish(opp)                             │
│  │   └─ on miss: screenshot + DOM clip → notify_selector_miss│
│  ├─ end-of-cycle:                                            │
│  │   ├─ INSERT into discovery_cycle_log                      │
│  │   └─ XADD stream:notify → cycle-report Discord card       │
│  └─ shutdown: SIGTERM → finish current combo → exit 0        │
└──────────────────────────────────────────────────────────────┘
                           │ autossh tunnel (Pi loopback:5432, 6379)
                           ▼
Pi 5 (downstream unchanged + 2 new migrations)
┌──────────────────────────────────────────────────────────────┐
│  extractor-worker:  no change (this path bypasses it)        │
│  ranker-worker:     scores incoming opps from stream:rank    │
│  notifier-discord:  + new handler for cycle-report card      │
│  applier-worker:    no change (existing auto-apply flow)     │
│  jobs-scheduler:    gate emission on                         │
│                     sources.discovery_method = 'http_curl'   │
└──────────────────────────────────────────────────────────────┘

DELETED post-V025:
  • src/sources/india/internshala.py
  • config/sources/internshala_filters.yaml
  • Source plugin registry entry "india_internshala" (URL emitter)

NEW:
  • migrations/V025__discovery_method.sql
  • migrations/V026__discovery_cycle_log.sql
  • config/sources/internshala_selectors.yaml          (SIGHUP-reloadable)
  • config/sources/internshala_dropdown_matrix.yaml    (combo matrix)
  • src/common/stipend_parser.py                       (corpus-tested)
  • src/sources/india/internshala_card_parser.py       (DOM card → Opportunity)
  • src/fetchers/browser/engine.py                     (BrowserEngine Protocol)
  • src/fetchers/browser/camoufox_engine.py            (impl)
  • src/workers/internshala_discovery_worker.py        (main loop)
  • src/cli/internshala_discover.py                    (CLI for ad-hoc cycles)
  • src/notifiers/discord/handlers/notify_discovery_cycle.py
  • docker/discovery.Dockerfile                        (extends camoufox base)
  • compose.sidecar.yaml service "internshala-discovery-worker"
  • docs/runbooks/internshala_discovery.md             (operator runbook)
  • tests/fixtures/stipend_strings.json                (≥100 strings)
  • tests/fixtures/internshala/listing_card_*.html     (≥20 cards)
```

**Invariants:**
- Pi never touches Internshala's web UI for discovery. ThinkPad IP is the only browse surface.
- One identity row services discovery + apply for Internshala (Phase 1 acceptance; dual-identity deferred).
- Card-parser code is single-source. Extractor tier1 selectors module re-imports from the shared parser; behavioural compatibility preserved for any non-Internshala-on-tier1 callers.
- All browser interaction goes through `BrowserEngine` Protocol. Camoufox is one impl; replaceable.
- Cycle budget = ~4 min (12 combos × ~20 s per combo). Hard cap = 12 × 30 s = 6 min worst case.

---

## Section 2 — Components

### Migrations

**V025 `discovery_method.sql`**
```sql
BEGIN;

ALTER TABLE sources
  ADD COLUMN discovery_method TEXT NOT NULL DEFAULT 'http_curl'
    CHECK (discovery_method IN ('http_curl', 'camoufox_dropdown'));

UPDATE sources SET discovery_method = 'camoufox_dropdown' WHERE slug = 'in_internshala';

CREATE INDEX idx_sources_discovery_method ON sources (discovery_method);

INSERT INTO schema_migrations (version, applied_at) VALUES ('V025', NOW());
COMMIT;
```

**V026 `discovery_cycle_log.sql`**
```sql
BEGIN;

CREATE TABLE discovery_cycle_log (
  id                       BIGSERIAL PRIMARY KEY,
  cycle_id                 UUID        NOT NULL UNIQUE,
  worker_id                TEXT        NOT NULL,
  source_slug              TEXT        NOT NULL,
  started_at               TIMESTAMPTZ NOT NULL,
  duration_sec             REAL        NOT NULL,
  combos_attempted         INT         NOT NULL,
  combos_succeeded         INT         NOT NULL,
  combo_timeouts           TEXT[]      NOT NULL DEFAULT '{}',
  selector_misses          TEXT[]      NOT NULL DEFAULT '{}',
  cards_scraped            INT         NOT NULL,
  cards_published          INT         NOT NULL,
  cards_rejected_subfloor  INT         NOT NULL,
  cards_rejected_dedup     INT         NOT NULL,
  cards_rejected_parse     INT         NOT NULL,
  healthy                  BOOLEAN     NOT NULL,
  selectors_version        TEXT        NOT NULL,
  matrix_version           TEXT        NOT NULL,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_discovery_cycle_log_started_at ON discovery_cycle_log (started_at DESC);
CREATE INDEX idx_discovery_cycle_log_source_slug ON discovery_cycle_log (source_slug, started_at DESC);

INSERT INTO schema_migrations (version, applied_at) VALUES ('V026', NOW());
COMMIT;
```

### `config/sources/internshala_selectors.yaml`

> **RECON-FIRST INVARIANT.** The selector values below are *recon placeholders*.
> Before first deploy, capture the real selectors from a live, logged-in
> Internshala session on the ThinkPad (devtools → inspect each dropdown trigger,
> each Chosen option list, each listing card field, the Load-more button, and the
> login-redirect / captcha markers). Internshala uses the jQuery **Chosen** plugin
> for category/location (`*_chosen` containers with `.chosen-results li` options),
> and the stipend control may be a slider rather than a radio group. The worker
> MUST refuse to start if `selectors.version` still equals the placeholder
> `RECON_PENDING` — guard in `load_config()`. This is the single highest-risk
> assumption in the design; do not skip recon.

```yaml
# SIGHUP-reloadable selector store. Single source of truth.
# Bump version on every edit. Worker logs old → new on reload.
# version MUST NOT be "RECON_PENDING" at deploy — worker refuses to boot.
version: "RECON_PENDING"   # → bump to e.g. "2026.05.29.v1" after live recon
selectors:
  page_root: "body"
  modal_dismiss: "#newonboardpage_modal .close, .newonboardpage_modal_close"
  dropdown:
    stipend_button:   "#select_stipend"
    stipend_option_above_10000: "label[for='stipend_radio_4']"   # "10000"
    category_button:  "#select_category_chosen"
    category_options: "#select_category_chosen .chosen-results li"
    work_mode_wfh_chip:  "label[for='work_from_home']"
    work_mode_partime_chip: "label[for='part_time']"
    location_button:  "#select_location_chosen"
    location_options: "#select_location_chosen .chosen-results li"
  listing:
    card_root:        "div.individual_internship"
    card_title:       ".heading_4_5.profile, .job-internship-name"
    card_company:     ".company_and_premium .company-name, p.company a"
    card_location:    ".locations span a, .location_link"
    card_stipend:     ".stipend, .stipend_container_table_cell"
    card_apply_link:  "a.view_detail_button"
    card_posted_relative: ".posted-by, .status-success"
  paginate:
    load_more_button: "#load_more_internships_button, a.click_source"
    list_end_marker:  ".no-search-results, .empty-listing"
```

### `config/sources/internshala_dropdown_matrix.yaml`

```yaml
version: "2026.05.29.v1"
# Each combo expands to one dropdown click sequence.
# stipend is always max-supported ("above 10000") — code-level floor
# enforces ≥30k against parsed card stipend.
matrix:
  # Backend / Python persona × remote
  - { category: "Backend Development",          work_mode: "wfh" }
  - { category: "Python/Django Development",    work_mode: "wfh" }
  - { category: "Full Stack Development",       work_mode: "wfh" }
  - { category: "DevOps",                       work_mode: "wfh" }
  - { category: "API Development",              work_mode: "wfh" }
  - { category: "Data Engineering",             work_mode: "wfh" }
  # ML persona × remote
  - { category: "Machine Learning",             work_mode: "wfh" }
  - { category: "Data Science",                 work_mode: "wfh" }
  - { category: "Artificial Intelligence (AI)", work_mode: "wfh" }
  - { category: "Deep Learning",                work_mode: "wfh" }
  - { category: "Natural Language Processing (NLP)", work_mode: "wfh" }
  - { category: "Computer Vision",              work_mode: "wfh" }
# On-site combos deferred to Phase 1.5 once remote pool is verified.
```

### `src/common/stipend_parser.py`

```python
"""Normalise Internshala stipend strings → INR per month.

Single source of truth for stipend → comp_min_inr conversion. Replaces
ad-hoc regex in `tier1_selectors/internshala.py`. Backed by a regression
corpus in `tests/fixtures/stipend_strings.json` (≥100 real strings).
"""

from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class ParsedStipend:
    comp_min_inr_per_month: float | None
    comp_max_inr_per_month: float | None
    comp_min_native: float | None
    comp_max_native: float | None
    native_currency: str
    native_period: str
    raw: str

def parse_stipend(raw: str) -> ParsedStipend | None:
    """Returns None on unparseable input (caller drops the card)."""
    # impl: lakhs (L), crores (Cr), k-suffix, hyphen/en-dash ranges,
    # "/month", "/hour", "/year", currency symbols (₹ Rs USD), garbage
    # ("Negotiable", "Performance based", "Unpaid", "").
    ...
```

### `src/sources/india/internshala_card_parser.py`

```python
"""DOM card → Opportunity.

Single source of truth. The legacy tier1_selectors module re-imports
this for backward compatibility with `stream:extract`-driven flows.
"""
def parse_card(card_html: str) -> Opportunity | None: ...
```

### `src/fetchers/browser/engine.py`

```python
from typing import Protocol, AsyncContextManager

class BrowserEngine(Protocol):
    async def open_context(
        self, *, cookies: list[dict], ua: str, viewport: dict
    ) -> AsyncContextManager["BrowserContext"]: ...
    async def shutdown(self) -> None: ...
    def is_alive(self) -> bool: ...
```

### `src/fetchers/browser/camoufox_engine.py`

Camoufox-backed `BrowserEngine` impl. Wraps `camoufox.AsyncCamoufox` with the same fingerprint and Xvfb pattern used by `apply-browser-worker`. Adds `restart_after_cycles` lifecycle hook.

### `src/workers/internshala_discovery_worker.py`

Main loop. Structure:

```python
async def main() -> int:
    cfg = load_config()
    register_sighup(cfg)
    redis_q = await RedisQ.connect(...)
    db = await asyncpg.connect(...)
    identity = await checkout_identity(db, "in_internshala")
    engine: BrowserEngine = CamoufoxEngine(...)
    asyncio.create_task(heartbeat_loop(redis_q))
    cycle_index = 0
    error_streak = 0
    while not _shutdown.is_set():
        t0 = time.monotonic()
        try:
            report = await run_cycle(engine, identity, redis_q, db, cfg, cycle_index)
            error_streak = 0
            await persist_cycle_report(db, redis_q, report)
            idle = max(cfg.idle_sec - (time.monotonic() - t0), 0)
        except Exception as exc:
            error_streak += 1
            await emit_cycle_failure_alert(redis_q, exc)
            idle = min(cfg.backoff_sec * (2 ** (error_streak - 1)), 1800)
        if cfg.once: break
        await _sleep_or_shutdown(idle)
        cycle_index += 1
        if cycle_index % 10 == 0:
            await engine.restart()
    await engine.shutdown()
    await release_identity(db, identity)
    return 0
```

### `src/cli/internshala_discover.py`

```
mp internshala-discover [--once] [--combo NAME] [--dry-run]
                        [--matrix-version VER] [--selectors-version VER]
```
`--once` runs one cycle and exits. `--combo` filters matrix to one entry. `--dry-run` skips Redis publish + Postgres persist (cards print to stdout).

### `src/notifiers/discord/handlers/notify_discovery_cycle.py`

One handler. Two flavours:
- **healthy** — `✓ 47 cards • 12/12 combos • 3m12s • selectors v2026.05.29.v1` (single-line, low-noise).
- **degraded / failed** — embed with `combo_timeouts`, `selector_misses`, the latest screenshot attached.

### `docker/discovery.Dockerfile`

```Dockerfile
ARG BASE_IMAGE=cartograph-apply-browser:latest
FROM ${BASE_IMAGE}
COPY src/ /app/src/
ENV PYTHONPATH=/app
ENTRYPOINT ["python", "-m", "src.workers.internshala_discovery_worker"]
```

### `compose.sidecar.yaml` — new service

```yaml
  internshala-discovery-worker:
    image: cartograph-discovery:latest
    build:
      context: .
      dockerfile: docker/discovery.Dockerfile
    env_file: .env.sidecar
    environment:
      INTERNSHALA_IDLE_SEC: "180"           # 3 min testing
      INTERNSHALA_BACKOFF_SEC: "600"
      INTERNSHALA_COMP_FLOOR_INR: "30000"
      INTERNSHALA_SELECTORS_PATH: /app/config/sources/internshala_selectors.yaml
      INTERNSHALA_MATRIX_PATH:    /app/config/sources/internshala_dropdown_matrix.yaml
    volumes:
      - ./config:/app/config:ro             # SIGHUP-reload friendly
      - discovery_screenshots:/tmp/discovery # selector-miss artefacts
    shm_size: "2gb"
    tmpfs:
      - /tmp:size=2g
    mem_limit: 1500m
    network_mode: host
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import sys, redis, os; r = redis.Redis(...); sys.exit(0 if r.get('discovery:heartbeat') else 1)"]
      interval: 60s
      timeout: 5s
      retries: 3
```

### Files to modify

- `src/workers/scheduler.py` — gate Internshala emission on `discovery_method='http_curl'`.
- `src/extractors/tier1_selectors/internshala.py` — re-import `parse_card` from `src/sources/india/internshala_card_parser.py`; keep symbol for non-Internshala-on-tier1 callers; remove duplicate regex.
- `config/profile/prefs.yaml` — add `discovery.internshala.idle_sec` + `discovery.internshala.comp_floor_inr` (overrides env when set).
- `src/cli/main.py` — register `internshala-discover` subcommand.
- `CLAUDE.md` — append "Internshala Browser Discovery (Phase 4)" section after "Browser engine refresh".

### Files to delete (post-V025)

- `src/sources/india/internshala.py`
- `config/sources/internshala_filters.yaml`
- Scheduler integration test referencing `india_internshala` URL plugin (replace with discovery_method-gated test).

---

## Section 3 — Data flow

```
                ┌──────────────────────────────────────────────────────┐
                │ ThinkPad: internshala-discovery-worker boot          │
                │  • read env (IDLE_SEC, BACKOFF_SEC, COMP_FLOOR_INR)  │
                │  • read selectors.yaml + dropdown_matrix.yaml        │
                │  • register SIGHUP → re-read both YAMLs              │
                │  • autossh tunnel up (PG 127.0.0.1:5432, Redis 6379) │
                │  • checkout identity, decrypt cookies (libsodium)    │
                │  • engine.open_context(cookies, ua, viewport)        │
                │  • heartbeat loop started                            │
                └──────────────────────────────────────────────────────┘
                                       │
                                       ▼
                ┌──────────────────────────────────────────────────────┐
                │ For combo in matrix (12 combos):                     │
                │   asyncio.wait_for(combo, timeout=30):               │
                │     page = context.new_page()                        │
                │     page.goto("/internships/")                       │
                │     dismiss_modal()                                  │
                │     click stipend → "above 10000"                    │
                │     click category → combo.category                  │
                │     click work_mode chip                             │
                │     wait_for_selector(card_root, timeout=8s)         │
                │       # NOT networkidle — IS telemetry never quiets  │
                │     captured_url = page.url   (recorded into report) │
                │     for page_n in 1..3:                              │
                │       html = page.content()                          │
                │       cards = parse_listing(html, selectors)         │
                │       for card in cards:                             │
                │         stipend = parse_stipend(card.stipend_text)   │
                │         if stipend.comp_min_inr_per_month is None:   │
                │           drop (cards_rejected_parse += 1); continue │
                │         if stipend.comp_min_inr_per_month < FLOOR:   │
                │           drop (cards_rejected_subfloor += 1)        │
                │           continue                                   │
                │         sha = sha256(card.canonical_url).hex         │
                │         if not SET internshala:seen:<sha> 1 EX 86400 │
                │            NX:                                       │
                │           drop (cards_rejected_dedup += 1); continue │
                │         opp = build_opportunity(card, stipend)       │
                │         opp_id = await persist_and_publish(redis_q,  │
                │                                            opp)      │
                │         cards_published += 1                         │
                │       if page_n < 3: click_load_more()                │
                │     close page                                       │
                └──────────────────────────────────────────────────────┘
                                       │
                                       ▼
                ┌──────────────────────────────────────────────────────┐
                │ Cycle end:                                           │
                │   report = DiscoveryCycleReport(...)                 │
                │   INSERT into discovery_cycle_log                    │
                │   XADD stream:notify → notify_discovery_cycle        │
                │   sleep(max(IDLE_SEC - elapsed, 0))                  │
                │   every 10 cycles: engine.restart()                  │
                └──────────────────────────────────────────────────────┘
                                       │
                                       ▼  (pi-side; stream:rank)
                ┌──────────────────────────────────────────────────────┐
                │ ranker-worker (no change)                            │
                │   score opp, write opportunity_scores + comp_min_inr │
                │   transition seen → ranked                           │
                ├──────────────────────────────────────────────────────┤
                │ notifier-discord (one new handler)                   │
                │   • per-opp digest card (existing)                   │
                │   • discovery-cycle-report card (NEW)                │
                ├──────────────────────────────────────────────────────┤
                │ applier-worker (no change)                           │
                │   /apply uses existing in-platform_internshala path  │
                └──────────────────────────────────────────────────────┘
```

**Stream contract — `stream:rank` payload from this worker:**
Identical to the existing extractor-emitted payload. Browser agent is a *producer parity* — must include `opportunity_id` (returned by `persist_and_publish`). No new contract.

**Stream contract — `stream:notify` cycle-report payload:**

```json
{
  "kind": "discovery_cycle_report",
  "cycle_id": "uuid",
  "source_slug": "in_internshala",
  "started_at": "iso8601",
  "duration_sec": 192.4,
  "summary": "✓ 47 cards • 12/12 combos • 3m12s",
  "healthy": true,
  "screenshot_b64": null,
  "details": { ... full DiscoveryCycleReport dict ... }
}
```

---

## Section 4 — Error handling

| Failure mode | Detection | Action | RTO | RPO |
|---|---|---|---|---|
| Selector miss (wait_for_selector raises) | per-action timeout (10 s default) | `screenshot + DOM clip (50 KB) → /tmp/discovery/miss/<combo>_<ts>` + `notify_selector_miss`; combo skipped, others continue | < 30 s | 0 |
| Combo wall-clock exceeded | `asyncio.wait_for(combo, timeout=30)` | kill subtree, record in `combo_timeouts`, continue next combo | < 30 s | 0 |
| Internshala session expired (login redirect) | `page.url` matches `/login` | abort cycle, mark `healthy=False`, alert `internshala_session_expired`, exit code 2 → identity warmup re-runs | manual ~5 min | 0 |
| Camoufox crash / IPC dead | `engine.is_alive()` false at top of combo | `engine.restart()`; if 3 consecutive restart fails → exit 1; container restart picks up | < 60 s | 0 |
| ThinkPad reboot | systemd-managed container | `restart: unless-stopped`; identity re-leased at boot | < 90 s | 0 |
| Internshala 429 / shadow-ban | `page.title()` contains "Access Denied" or card count is zero for ≥ 3 consecutive combos | exponential backoff (5 min → 15 min → 30 min), `notify_source_rate_limited`; if 4th attempt fails → mark source `auto_apply_enabled=false` and alert | 5 min → 30 min | 0 |
| **Captcha / interstitial challenge** | challenge selector present (`iframe[src*='captcha']`, `.cf-challenge`, `#px-captcha`) OR expected listing root absent after nav | abort cycle, `healthy=False`, screenshot → `notify_discovery_captcha` in `#🔔-alerts`, exit code 2 → cool-off + identity warmup. NEVER attempt to solve. | manual | 0 |
| **`networkidle` never settles (Internshala background telemetry / polling keeps the connection busy)** | DO NOT gate on `networkidle`. Gate on `wait_for_selector(listing.card_root, timeout=8s)` — readiness = first card visible, not network-quiet. Optional `wait_for_load_state('domcontentloaded')` only. | card_root absent after 8 s → treat as selector miss (screenshot + skip combo) | < 30 s | 0 |
| Stipend parse fail | `parse_stipend` returns None | drop card silently, `cards_rejected_parse += 1`, log `stipend_parse_fail` with raw + sha truncated | n/a | n/a |
| Redis OOM mid-XADD | `OutOfMemoryError` raised by RedisQ.publish | log + retry once after 5 s; if still failing drop card (dedup TTL ensures we re-see it next cycle) | < 90 s | 0 |
| Postgres tunnel down | asyncpg connect raises | exponential backoff (5 s → 30 s → 5 min), `notify_pg_tunnel_down`; if down > 5 min → exit 2 | < 5 min | 0 |
| Heartbeat stale | Pi alerter sees no `discovery:heartbeat` for > 90 s | page user via `#🔔-alerts` | < 90 s | 0 |
| Selectors YAML edited | SIGHUP received | reload + log `selectors_reloaded old=<v> new=<v>`; bad YAML → keep old + log `selectors_reload_failed` | < 1 s | 0 |
| Matrix YAML edited | SIGHUP received | same | < 1 s | 0 |
| `discovery_cycle_log` insert fails | exception in `persist_cycle_report` | log + emit `notify_pg_persist_failed`; report still posted to Discord (best-effort) | n/a | 0 |
| Healthy cycle but zero published cards | `cards_published == 0 AND healthy=True` for ≥ 3 cycles in a row | alert `discovery_dry_streak` (likely Internshala UX change or floor too high) | 3 cycles | 0 |
| Engine restart cycle counter | every 10 cycles | tear down context, fresh `engine.open_context()`; mitigates camoufox memory drift | n/a | 0 |

**Exit codes:**
- `0` — orderly SIGTERM shutdown after completing current combo.
- `1` — fatal engine-restart loop or unrecoverable internal error.
- `2` — fatal external (Postgres unreachable, identity invalid). Container restarts, retries identity checkout.

---

## Section 5 — Testing

### Test layers

1. **Unit — stipend parser corpus**
   - `tests/common/test_stipend_parser.py`
   - Loads `tests/fixtures/stipend_strings.json` (≥100 real strings already extracted from current `opportunities` table).
   - Categories covered: single value, range, per-hour, per-day, per-week, per-year, lakh-suffix, crore-suffix, k-suffix, ₹ vs Rs vs no symbol, mixed casing, leading/trailing whitespace, garbage ("Negotiable", "Performance based", "Unpaid", "").
   - Format: `{"raw": "...", "expected_min_inr_per_month": ..., "expected_max_inr_per_month": ..., "expected_currency": "INR", "expected_period": "month"}`.
   - Any new parse format that surfaces in prod must be added before the parser change ships.

2. **Unit — card parser**
   - `tests/sources/india/test_internshala_card_parser.py`
   - Fixture HTML cards in `tests/fixtures/internshala/listing_card_*.html` (≥20 snapshots).
   - Covers: posted-N-days variants, multi-skill chips, "Apply by X" suffix, closed-application banner, full-page listing wrapper, on-site vs remote, range stipend, single stipend.

3. **Unit — combo iterator**
   - `tests/workers/test_internshala_discovery_combos.py`
   - Validates `internshala_dropdown_matrix.yaml` expands to exactly the expected combo set; rejects unknown axes.

4. **Unit — `BrowserEngine` Protocol conformance**
   - `tests/fetchers/browser/test_engine_protocol.py`
   - Static check via `typing.get_type_hints` + runtime smoke via a fake `BrowserEngine` impl.

5. **Unit — cycle report serialisation**
   - Round-trip `DiscoveryCycleReport` → SQL row → re-loaded dict matches.

6. **Unit — dedup logic**
   - Mock Redis SET NX returning `True` (fresh) and `None` (dup); assert publish skipped on dup.

7. **Unit — comp-floor filter**
   - Fixture of 30 opps with comp_min_inr_per_month spanning {None, 5000, 29999, 30000, 80000}; assert only ≥ 30000 pass.

8. **Unit — SIGHUP reload**
   - Worker receives SIGHUP; mock filesystem reload; assert new version logged + matrix swapped atomically.

9. **Integration — single combo smoke (`integration` marker, opt-in)**
   - `tests/integration/test_internshala_discovery_smoke.py`
   - Spins headless camoufox locally against Internshala live with throwaway cookies.
   - Asserts ≥ 1 card scraped + zero selector miss for `backend-development` combo.
   - CI-skipped by default; run via `pytest -m integration` from ThinkPad post-deploy.

10. **Integration — selector health probe**
    - `tests/integration/test_internshala_selector_health.py`
    - Designed to be cron-able hourly post-deploy. Runs `mp internshala-discover --once --combo backend-development --dry-run`. On failure: posts to `#🛠-source-health`.

11. **End-to-end — fail-fast CLI**
    - Manual: `mp internshala-discover --once --combo backend-development --dry-run` returns ≥ 1 card to stdout and exits 0 within 60 s.

12. **Pi-side — scheduler gating**
    - `tests/workers/test_scheduler_discovery_method_gate.py`
    - Asserts scheduler does NOT emit `stream:fetch` for sources where `discovery_method='camoufox_dropdown'`.

13. **Pi-side — extractor backward compatibility**
    - `tests/extractors/tier1_selectors/test_internshala_compat.py`
    - Existing HTML fixtures keep passing under shared-parser refactor.

### Smoke acceptance criteria (manual, post-deploy)

1. Worker boots, heartbeat appears in Redis within 60 s.
2. First cycle completes within 6 min; `discovery_cycle_log` row inserted; Discord cycle-report card posted.
3. ≥ 10 cards published in the first cycle (testing cadence).
4. `SELECT comp_min_inr FROM opportunities WHERE source_id = <internshala>` shows zero rows with `comp_min_inr < 30000`.
5. `/auto-apply preview 10` shows backend/ML candidates only — no civil/mechanical/marketing/sales noise.
6. Editing `internshala_selectors.yaml` + `kill -HUP <pid>` produces `selectors_reloaded` log line within 1 s.
7. Sending SIGTERM → worker completes current combo and exits 0 within 30 s.

---

## Section 6 — Deployment + verification

### Build + ship

```bash
# Dev box (x86_64 native — ThinkPad target is x86_64, no QEMU needed)
docker buildx build \
  --platform linux/amd64 \
  --output type=docker \
  --build-arg BASE_IMAGE=cartograph-apply-browser:latest \
  -t cartograph-discovery:latest \
  -f docker/discovery.Dockerfile .

# Save + ship + load on ThinkPad
docker save cartograph-discovery:latest | xz -1 > /tmp/discovery.tar.xz
rsync --partial /tmp/discovery.tar.xz remote_lakshit_gupta@<thinkpad-ip>:/tmp/
ssh remote_lakshit_gupta@<thinkpad-ip> 'xz -d < /tmp/discovery.tar.xz | docker load'
```

### Pi-side migration

```bash
# Apply V025 + V026 (tools image must include the new migrations)
ssh dietpi@192.168.1.240
cd /home/dietpi/coding/Cartograph
sops exec-env secrets.yaml 'docker compose run --rm \
  -v /home/dietpi/coding/Cartograph/migrations:/app/migrations:ro \
  tools python -m src.cli.main migrate'

# Verify
sops exec-env secrets.yaml 'docker exec -i cartograph-postgres-1 \
  psql -U "$postgres_user" -d "$postgres_db" \
  -c "SELECT slug, discovery_method FROM sources WHERE slug = '\''in_internshala'\'';"'
# expect: in_internshala | camoufox_dropdown
```

### ThinkPad-side launch

```bash
ssh remote_lakshit_gupta@<thinkpad-ip>
cd /home/remote_lakshit_gupta/cartograph
git pull
sops exec-env .env.sidecar 'docker compose -f compose.sidecar.yaml up -d --force-recreate internshala-discovery-worker'

# Tail
docker compose -f compose.sidecar.yaml logs -f internshala-discovery-worker
```

### Verification window (24 h)

| Hour | Expected signal |
|---|---|
| T+0 | Heartbeat in Redis; first cycle starts |
| T+5 min | First `discovery_cycle_log` row; Discord cycle-report card |
| T+15 min | ≥ 3 cycles completed; at least one with ≥ 10 cards published |
| T+1 h | `comp_min_inr` distribution skewed ≥ 30k; no civil/mechanical |
| T+4 h | `/auto-apply preview 10` returns backend/ML candidates with declared ≥ 30k |
| T+24 h | No `discovery_dry_streak` alerts; no `internshala_session_expired` alert |

### Rollback

`sources.discovery_method` is enum-flippable. To roll back:
```sql
UPDATE sources SET discovery_method = 'http_curl' WHERE slug = 'in_internshala';
```
Pi scheduler resumes URL crawler within 1 tick. ThinkPad worker becomes idempotent no-op (dedup TTL keeps it cheap). Delete URL crawler only **after** 7 days of clean discovery-method = camoufox_dropdown operation.

---

## Section 7 — Risk register

| Risk | Mitigation |
|---|---|
| Internshala detects automation → cookies invalidated | Camoufox stealth + ghost-cursor + 3-min cycle ceiling testing only (jitter to ±30 s) + identity warmup re-runs on session expiry. Single identity used; dual-identity flagged for Phase 1.5. |
| Internshala UI change breaks selectors | `INTERNSHALA_DISCOVERY_SELECTORS` versioned + hot-reloadable. `notify_selector_miss` posts screenshot + DOM clip within 30 s of break. Auto-fallback to `manual_apply_ready` for affected combos. |
| Dropdown click sequence dependent on element load order | `wait_for_selector` per dropdown element before clicking it. Readiness = target element visible, never `networkidle` (Internshala fires background telemetry that keeps the network busy indefinitely). Combo 30 s timeout is the backstop. |
| Internshala dropdowns are jQuery **Chosen** plugin (`*_chosen` containers), not native `<select>` | Selectors in YAML target the Chosen-rendered `.chosen-results li` option nodes + the `.chosen-single` trigger. **Recon-first invariant**: real selectors captured from a live logged-in session BEFORE first deploy; YAML ships with recon-verified values, not guesses. Stipend control may render as a slider rather than radio — recon confirms the actual control and the YAML encodes whichever it is. |
| Stipend filter UI exposes only "above ₹10,000" max | Accepted by design — code-level `INTERNSHALA_COMP_FLOOR_INR` floor (default ₹30,000) does the real ≥30k enforcement on parsed card stipend. The dropdown is set to its max supported value purely to thin the server-side result set. |
| Stipend parse silently drops a new format | Regression corpus blocks parser PRs that don't add the new format. `cards_rejected_parse` metric tracks drift. Discord alert at > 20% drop ratio. |
| ThinkPad sleep / lid close | systemd-inhibit (covered by existing apply-browser-worker runbook); BIOS "AC stay-on" toggle documented. |
| Stream `apply_browser` and `rank` collide under load | Existing per-stream `_MAXLEN` caps + Redis `noeviction`. Browser agent publishes only after dedup + floor passes, so per-cycle output ≤ ~50 opps. |
| Single-identity SPOF for discovery + apply | Phase 1 acceptance. Dual-identity strategy: second `in_internshala` account warmed for read-only use; auto-apply continues on primary. Implementation deferred but spec calls it out. |
| Cookie key compromise on ThinkPad | LUKS on ThinkPad disk; restricted user `remote_lakshit_gupta`; no other untrusted workloads. Same posture as auto-apply browser worker. |
| Cycle takes longer than IDLE_SEC | `idle = max(IDLE_SEC - elapsed, 0)` — sleeps zero seconds, runs continuously. Heartbeat keeps liveness signal alive. Documented as "back-pressure stable". |
| Discord rate-limit on cycle-report posts | 3-min cadence × 1 post/cycle = 20 posts/hour. Far under Discord limits. Failure cards include screenshot (one extra file upload). |
| Selector hot-reload races mid-cycle | Selectors read once at the start of each cycle. Mid-cycle reload applies to NEXT cycle only. |

---

## Section 8 — Open items, deferred

- Jobs section (`/jobs/`) discovery → Phase 2. Different DOM, LPA-vs-stipend conversion, full-time category taxonomy.
- Dual-identity for discovery vs apply → Phase 1.5.
- On-site combos (BLR / HYD / PUN) → Phase 1.5, once remote pool validated.
- LLM tier-2 description enrichment per opp → Phase 2 toggle.
- Discovery cadence auto-tune from `discovery_cycle_log` history → Phase 2.
- Multi-engine `BrowserEngine` impls (NodriverEngine, PatchrightEngine) → tied to "browser engine refresh" deferred item in CLAUDE.md.

---

## Section 9 — CLAUDE.md addendum (drafted)

Append after the **Browser engine refresh** section:

```markdown
## Internshala browser discovery (Phase 4 — shipped 2026-05-XX)

ThinkPad-resident camoufox worker that drives Internshala's dropdown UI,
scrapes listing cards, post-filters in code against `INTERNSHALA_COMP_FLOOR_INR`
(default ₹30,000/month), dedups against Redis `SET internshala:seen:<sha>`
(24 h TTL), and calls `extractors.persist.persist_and_publish` to upsert +
emit on `stream:rank`. Pi scheduler ceases URL crawling for Internshala
via `sources.discovery_method = 'camoufox_dropdown'` (V025).

Loop-with-sleep semantics: `IDLE_SEC=180` (testing) → `IDLE_SEC=1800` (prod).
Per-combo `asyncio.wait_for(timeout=30)`. Selectors hot-reload on SIGHUP.

Files:
- src/workers/internshala_discovery_worker.py
- src/sources/india/internshala_card_parser.py
- src/common/stipend_parser.py
- src/fetchers/browser/{engine.py,camoufox_engine.py}
- config/sources/internshala_{selectors,dropdown_matrix}.yaml
- migrations/V025__discovery_method.sql, V026__discovery_cycle_log.sql

Hard rules — non-negotiable
1. **Pi never browses Internshala.** Discovery + apply only on ThinkPad.
2. **Pre-publish floor enforcement.** Sub-floor cards never reach `stream:rank`.
3. **Redis dedup before persist.** No raw cards reach Postgres without 24 h
   uniqueness check.
4. **Selectors live in YAML, not Python constants.** SIGHUP-reloadable.
5. **`BrowserEngine` Protocol mandatory.** No direct camoufox imports in
   worker code.
6. **Loop-with-sleep, no cron.** Single consumer per Internshala identity.

Operations: docs/runbooks/internshala_discovery.md.
```

---

## Section 10 — Worktree + parallel agent execution (handoff to writing-plans)

After spec approval, implementation enters via **writing-plans skill**:

1. **Worktree** — `/git-worktree-manager` creates `feat/internshala-browser-discovery` branch + isolated worktree. Main branch unaffected, allowing parallel sessions on unrelated features.
2. **Parallel-agent execution** — `/dispatching-parallel-agents` fans out:
   - Agent A — migrations + scheduler gate (`V025`, `V026`, `scheduler.py`).
   - Agent B — stipend parser + corpus + tests.
   - Agent C — card parser + tier1 backward-compat refactor.
   - Agent D — `BrowserEngine` Protocol + `CamoufoxEngine` impl + reuse of existing camoufox plumbing from `fetchers/browser/`.
   - Agent E — discovery worker main loop + CLI + Dockerfile + compose.sidecar entry.
   - Agent F — Discord cycle-report handler.
   - Agent G — runbook + CLAUDE.md addendum.
3. Each agent commits to the same worktree branch; final consolidation runs the test matrix and prepares the PR.

Worktree + parallel agents are **invoked at writing-plans hand-off**, not during brainstorming.

---

## Appendix A — Stream / table summary

| Stream | Producer | Consumer | Payload kind |
|---|---|---|---|
| `stream:rank` | discovery-worker (this spec), extractor-worker (others) | ranker-worker | `{opportunity_id, ...}` |
| `stream:notify` | discovery-worker (cycle reports), ranker-worker, etc. | notifier-discord | `{kind, ...}` |

| Table | Writer | Reader | Lifetime |
|---|---|---|---|
| `discovery_cycle_log` | discovery-worker | dashboards, runbooks | retained; manual prune > 90 d |
| `sources.discovery_method` | DBA / migration | scheduler, discovery-worker | permanent |

---

## Appendix B — Environment variables (ThinkPad)

| Var | Default | Purpose |
|---|---|---|
| `INTERNSHALA_IDLE_SEC` | 180 | Idle sleep between cycles (testing); raise to 1800 in prod |
| `INTERNSHALA_BACKOFF_SEC` | 600 | Initial back-off on cycle exception |
| `INTERNSHALA_COMP_FLOOR_INR` | 30000 | Per-month INR floor for publish |
| `INTERNSHALA_MAX_CYCLES_PER_ENGINE` | 10 | Engine restart cadence |
| `INTERNSHALA_SELECTORS_PATH` | `/app/config/sources/internshala_selectors.yaml` | SIGHUP-reload source |
| `INTERNSHALA_MATRIX_PATH` | `/app/config/sources/internshala_dropdown_matrix.yaml` | SIGHUP-reload source |
| `INTERNSHALA_IDENTITY_LABEL` | `raju_internshala` | identity_vault checkout key |
| `LIBSODIUM_MASTER_KEY_HEX` | (from sops-decrypted `.env.sidecar`) | cookie decryption |
| `POSTGRES_*` | (tunnel) | autossh-forwarded to Pi loopback |
| `REDIS_*` | (tunnel) | autossh-forwarded to Pi loopback |

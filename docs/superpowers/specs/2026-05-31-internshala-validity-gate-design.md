# Internshala discovery — expired/invalid validity gate

**Shipped 2026-05-31.** Adds a "still open to apply" filter to the camoufox
discovery worker so expired internships never reach `stream:rank` / Postgres,
and existing-but-since-expired rows stop surfacing in the digest + auto-apply.

## Problem

The discovery worker scraped Internshala cards, enforced the ≥₹30k floor,
deduped, and persisted — but did NOT check whether the listing was still open
to apply. Expired internships landed in `opportunities`. The user wanted only
currently-valid (as of the current date) ≥30k listings, integrated into the
existing Pi / dev-box / ThinkPad topology with no new commands.

## Decisions (user-confirmed)

1. **Signal**: explicit "Apply By" deadline is authoritative; a posted-age
   window is the backstop for cards lacking a deadline.
2. **Scope**: filter new crawls + hide existing expired at read-time. No
   physical sweep / UPDATE of old rows (they age out).
3. **Degradation**: fail-open — a card with no parseable date is kept
   (Internshala's listing only surfaces open internships by default, so a
   missing date ≠ expired; failing closed would drain the feed on selector
   drift).

## Design

### Layer 1 — crawl-time gate (primary)

- New pure module `src/common/internshala_posted_parser.py`:
  - `parse_apply_by("Apply By 30 Jun' 26", now=...)` → inclusive end-of-day
    `datetime(2026,6,30,23,59,59)` (the apply-by day still counts as valid).
  - `parse_posted_relative("Posted 3 days ago", now=...)` → absolute
    `posted_at`. Both return `None` on garbage (fail-open).
- `internshala_card_parser.parse_card` gained a `now` kwarg and two selectors
  (`card_apply_by`, `card_posted_relative`); it now populates `expires_at` and
  `posted_at` (both already existed on `Opportunity` + the `opportunities`
  table — zero schema change for the opp itself).
- `report.passes_validity(opp, now, max_age_days)` — deadline-primary,
  age-backstop, fail-open. Wired into `cycle._ingest_card` right after
  `passes_floor`, before the dry-run / Redis-dedup / persist side effects.
  Rejects increment `report.cards_rejected_expired` +
  `discovery_cards_rejected_total{reason="expired"}`.
- Config `max_age_days` (default 14) via the existing prefs > env > default
  resolver; prefs key `discovery.internshala.max_age_days`.

### Layer 2 — read-time guard (defense in depth)

`AND (o.expires_at IS NULL OR o.expires_at > NOW())` added to:
- `notify_digest._load_top_opps` (digest query).
- `auto_apply_engine.find_eligible` where-clauses.

NULL passes (fail-open). Catches rows valid at crawl whose deadline passed
afterward.

### Observability

- `discovery_cycle_log.cards_rejected_expired` (migration **V027**, additive
  `INT NOT NULL DEFAULT 0`); `persistence.py` INSERT + `DiscoveryCycleReport`
  field carry it.
- The degraded #🛠-source-health embed's "Rejected" line shows
  `… · expired N`.

## RECON dependency

`config/sources/internshala_selectors.yaml` is still `version: RECON_PENDING`
(worker refuses to boot otherwise). The new `card_apply_by` selector is a
placeholder derived from the test fixture — confirm it (and whether every card
carries an apply-by node) during the mandatory live ThinkPad recon, same pass
that clears the other RECON_PENDING selectors.

## Tests

- `tests/common/test_internshala_posted_parser.py` — date-parser corpus.
- `tests/sources/india/test_internshala_card_parser.py` — `expires_at` /
  `posted_at` population + past-deadline still-parses + fixtures 13/14.
- `tests/workers/test_internshala_discovery.py` — `passes_validity` table,
  `max_age_days` config resolution, `cards_rejected_expired` row mapping.

## Not in scope

- Retroactive expiry of rows already in `opportunities` (Layer 2 hides them;
  no UPDATE sweep per user decision).
- The pre-existing snooze-vs-deadline `expires_at` overload (snoozed opps are
  excluded from both read queries by state, so no conflict here).

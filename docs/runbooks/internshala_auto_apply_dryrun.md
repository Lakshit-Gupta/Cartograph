# Internshala auto-apply — dry-run verification window

Phase 1 of the Phase 4 auto-apply rollout. Before flipping
`auto_apply.dry_run=false` in `prefs.yaml`, this runbook proves the
entire pipeline end-to-end against THREE real Internshala opportunities
**without** clicking Submit. The user reviews each screenshot in
Discord; only after all three pass does real submission go live.

Pre-reqs (everything below already shipped):

- V022 migration applied (`auto_apply_enabled` column, `auto_apply_daily_count`,
  `auto_apply_audit`).
- `compose.sidecar.yaml` running on the spare Pop OS 24.04 desktop with
  `apply-browser-worker` and the autossh tunnel up.
- `apply-result-worker` running on the Pi.
- `notify_auto_apply` registered in the Discord bot dispatch table.
- A healthy `identities` row exists with `platform='internshala'`,
  cookies populated, `ban_status='healthy'`. Confirm via:
  ```sql
  SELECT id, account_label, ban_status, last_used_at
  FROM identities WHERE platform='internshala';
  ```

---

## 1. Flip the flags

On the Pi (under `sops exec-env`):

```bash
cd /home/dietpi/coding/Cartograph

# 1a — whitelist the Internshala source row
docker exec -i cartograph-postgres-1 psql -U "$postgres_user" -d "$postgres_db" <<'EOF'
UPDATE sources SET auto_apply_enabled = TRUE WHERE slug = 'in_internshala';
EOF

# 1b — edit config/profile/prefs.yaml under sops exec-env
${EDITOR:-nano} config/profile/prefs.yaml
# Confirm the auto_apply block reads:
#   auto_apply:
#     enabled: true            # WAS false
#     dry_run: true            # keep TRUE for this entire runbook
#     min_score: 0.80
#     max_per_day: 3
#     methods:
#       - in_platform_internshala

# 1c — restart Pi-side services so they reload the prefs
sops exec-env secrets.yaml 'docker compose restart applier-worker apply-result-worker notifier-discord'
```

`apply-browser-worker` on the spare needs no restart — it reads
nothing from prefs.yaml; the dry-run flag rides in every
`stream:apply_browser` payload.

---

## 2. Pick three Internshala opps

Open Discord. Run `/source list` to confirm `in_internshala` shows
`active`. Pull three recent opps:

```sql
SELECT o.id, o.title, o.company, os.score
FROM opportunities o
JOIN sources s ON s.id = o.source_id
LEFT JOIN opportunity_scores os ON os.opportunity_id = o.id AND os.user_id = 1
WHERE s.slug = 'in_internshala'
  AND o.first_seen > NOW() - INTERVAL '7 days'
  AND os.score >= 0.80
ORDER BY os.score DESC
LIMIT 3;
```

Three UUIDs in hand. Note them.

---

## 3. Fire the three dry-runs

In Discord, run for each opp:

```
/apply <uuid_1>
/apply <uuid_2>
/apply <uuid_3>
```

Within ~30s each:

- `applications` row written with `state='applied'`, `payload->>'auto_apply'='true'`.
- `auto_apply_audit` row with `decision='submit_deferred_dryrun'`,
  `dry_run=true`, `score≥0.80`.
- `stream:apply_browser` carries one task (XLEN bumps by 1).
- Sidecar consumes it, opens Internshala, fills the modal, screenshots.
- `stream:apply_browser_result` carries the result with
  `status='dry_run_captured'`.
- `apply-result-worker` updates `applications.payload->>'browser_status'`
  and publishes `kind='auto_apply_dry_run'` onto `stream:notify`.
- Discord posts a card in `#🛠-source-health` (or wherever your
  `source_health` channel routes) with the embedded screenshot.

---

## 4. Verify (per opp)

The Discord card MUST show:

- [ ] Modal opened on the correct Internshala URL.
- [ ] PDF filename visible in the upload control (the
      `Resume.pdf` name from `internshala.py`).
- [ ] Cover letter textarea contains the LLM-generated text — no markdown
      asterisks/underscores leaking through.
- [ ] Custom Q&A fields filled (when present in the opp). Blanks
      acceptable when `qa_defaults` doesn't cover all of Internshala's
      questions.
- [ ] Submit button visible but NOT clicked. No success banner.

SQL spot-check (run on Pi):

```sql
-- applications: 3 rows, all dry_run
SELECT id, opportunity_id, payload->>'browser_status' AS browser_status,
       response_status, payload->>'task_id' AS task_id
FROM applications
WHERE response_status IN ('auto_apply_dry_run', 'auto_apply_dispatched', 'auto_apply_failed')
ORDER BY id DESC LIMIT 5;

-- audit: 3 rows, decision = submit_deferred_dryrun
SELECT id, opportunity_id, decision, dry_run, score, source_slug, occurred_at
FROM auto_apply_audit
ORDER BY id DESC LIMIT 5;

-- daily counter: 0 (dry-runs do NOT bump)
SELECT * FROM auto_apply_daily_count WHERE apply_date = CURRENT_DATE;
```

If any of the three cards fail to render or show selector miss, see §6
below before flipping.

---

## 5. Daily cap sanity check

Run a 4th `/apply` on another Internshala opp the same day (still
under `dry_run=true`). Because dry-runs don't bump the counter, this
also fires a dry-run capture (4th audit row, still `decision=submit_deferred_dryrun`).

Then in `prefs.yaml`, set `dry_run: false` momentarily and run THREE
real `/apply` commands on test opps you don't actually want to apply to
(e.g. internships in unrelated fields). After the 3rd, the 4th should
log `decision=refused_cap` in `auto_apply_audit`. Revert
`dry_run: true` immediately.

---

## 6. Selector drift / failure recovery

A `status='failed'` result with `error="selector_miss: <key>"` means
Internshala redesigned the relevant DOM element. Recovery:

1. SSH to the spare as `remote_lakshit_gupta`.
2. Open Internshala in a regular browser, log in with the same account
   the identity vault holds, click Easy Apply on a known job.
3. Devtools → Elements → copy the new selector for the failing key.
4. Edit `src/application/submitters/internshala_browser.py`:
   - Replace the value in `INTERNSHALA_SELECTORS[<key>]`.
   - Bump `INTERNSHALA_SELECTORS_VERSION` to today's date.
5. Rebuild the sidecar image: `docker compose -f compose.sidecar.yaml
   build apply-browser-worker && docker compose -f compose.sidecar.yaml
   up -d apply-browser-worker`.
6. Re-run one `/apply` against an Internshala opp; confirm the
   dry-run capture succeeds.
7. Commit the selector + version bump.

---

## 7. Flip to live

Once all three dry-runs pass AND the cap-test fires `refused_cap`
exactly once:

```bash
# 7a — turn off dry-run
${EDITOR:-nano} config/profile/prefs.yaml
# auto_apply.dry_run: false

# 7b — reload the applier so it picks up the new prefs
sops exec-env secrets.yaml 'docker compose restart applier-worker'
```

The next eligible `/apply` (Internshala, source whitelisted, score
≥0.80, daily count <3) will fire a real Easy Apply submit. The
Discord card lands in `#✅-applied` with the green colour and the
"submitted_at" timestamp populated.

Hold `max_per_day=3` for the first week. Bump only after seven days
with no failed submits and no Internshala bot-detection events. The
identity vault `ban_status` column will flip to `'suspect'` or
`'banned'` automatically on the next warmup probe if Internshala
locks the account; in that case stop auto-apply immediately
(`auto_apply.enabled: false`) and investigate.

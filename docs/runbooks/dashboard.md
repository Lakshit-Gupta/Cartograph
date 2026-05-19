# Dashboard runbook — Phase 5.2 web dashboard

> Shipped 2026-05-19 as the Phase 5.2 deliverable. Read-only,
> Tailscale-only, free-only. No Vercel, no Node build step, no JWT.

## What this is

A browser-facing read-only view over Cartograph's Postgres data, served
from the same Pi that runs the rest of the pipeline. It exists so the
operator can answer "what did Hop find for me today?" without `psql`
or scraping Discord scrollback.

The frontend is plain HTML + CSS + ES modules in `dashboard/`. There is
no `package.json`, no bundler, and no build step — the files Docker
serves are the files you edited. The backend is **PostgREST** running
as a Docker Compose service, querying a dedicated `dash` schema of
views (`dash.v_*`) created by `migrations/V019__dashboard_views.sql`.
The browser does NOT talk to PostgREST directly; it talks to the
existing `api-service` FastAPI process, which reverse-proxies a single
prefix (`/api/postgrest/*`) into PostgREST. That keeps same-origin
semantics, lets us strip dangerous headers on the way through, and
prevents any code-path from leaking PostgREST onto the host network.

Hard constraints (from CLAUDE.md and the Phase 5 scope cut):

- **Read-only.** No write verbs reach PostgREST through the proxy.
  Every view lives in the `dash` schema with `SELECT`-only grants to
  `pgrst_anon`.
- **Tailscale-only.** PostgREST has no host port published; the only
  way to reach it is via the api-service container, and the only way to
  reach api-service is via Tailscale (Cloudflared is whitelisted to
  `/webhooks/*` per CLAUDE.md).
- **Free-only.** No Vercel, no managed PostgREST cloud, no SaaS APM.
  Everything runs on the same Pi 5 that runs the rest of the stack.

## Architecture

```
┌──────────────────────┐
│ Browser              │
│ (tailscale device,   │
│  no auth today —     │
│  single-tenant Pi)   │
└──────────┬───────────┘
           │ http://<pi-tailscale-name>:9090/dashboard/
           │ http://<pi-tailscale-name>:9090/api/postgrest/v_*
           ▼
┌──────────────────────────────────────────────┐
│ api-service (FastAPI, uvicorn, port 9090)    │
│   - StaticFiles('/dashboard/') → dashboard/  │
│   - /api/postgrest/* reverse proxy           │
│     · forwards GET / HEAD / OPTIONS only     │
│     · strips client Cookie + Authorization   │
│     · enforces finite httpx timeout          │
└──────────┬───────────────────────────────────┘
           │ http://postgrest:3000/v_*          (Docker network 'internal')
           ▼
┌──────────────────────────────────────────────┐
│ postgrest:3000  (PostgREST, image pinned in  │
│                  compose.yaml)               │
│   - PGRST_DB_SCHEMAS=dash                    │
│   - PGRST_DB_ANON_ROLE=pgrst_anon            │
│   - PGRST_JWT_SECRET=""  (disabled — see     │
│     "Security posture" below)                │
│   - NO host port published                   │
└──────────┬───────────────────────────────────┘
           │ postgres:5432  (Docker network 'internal')
           ▼
┌──────────────────────────────────────────────┐
│ postgres:16  +  pgvector                      │
│   - schema dash (views)                       │
│   - schema public (tables — NEVER exposed)    │
│   - role pgrst_anon GRANT SELECT ON dash.*    │
└──────────────────────────────────────────────┘
```

Every arrow above is a Docker-internal hop except the first. The
internet sees nothing of PostgREST, ever.

## Starting it

```bash
cd /home/lakshit_gupta/coding/cartograph

# 1. Apply V019 if not already applied (creates dash schema + views +
#    pgrst_anon role + grants). The migration is idempotent.
make migrate

# 2. Bring the postgrest service up. It depends on postgres being
#    healthy (compose handles that).
docker compose up -d postgrest

# 3. The api-service ships the static dashboard + the proxy. If it was
#    already running before V019 you need to recreate so it picks up
#    the new route map.
docker compose up -d --force-recreate api-service

# 4. Verify both are healthy.
docker compose ps postgrest api-service
docker compose logs --tail=50 postgrest
```

Expected: `postgrest` logs end with `Listening on port 3000` and
`Connected to PostgreSQL`. The api-service logs include
`api_started`.

## Where to point your browser

The api-service uvicorn process listens inside the container on port
`9090` (see `compose.yaml` → `api-service` →
`command: ["uvicorn", "src.api.main:app", "--host", "0.0.0.0",
"--port", "9090"]`). By default `compose.yaml` does NOT publish that
port — the same posture as Postgres, Redis, and PostgREST. You enable
host-side reachability via the existing `compose.override.yaml`
pattern (see `compose.override.example.yaml` and
`docs/runbooks/prometheus_scrape_setup.md`).

Two practical access modes — pick whichever matches your topology:

**Mode A — SSH tunnel from a Tailscale device** (zero compose
change beyond the loopback-only override that's already documented for
Prometheus scrape):

```bash
# On the Pi, ensure the loopback override is active. This is the same
# override the Prometheus runbook uses — apply once per Pi.
cd /home/lakshit_gupta/coding/Marked_Path
[ -f compose.override.yaml ] || cp compose.override.example.yaml compose.override.yaml
sops exec-env secrets.yaml 'docker compose up -d --force-recreate api-service'

# From your laptop, over Tailscale:
ssh -L 9090:127.0.0.1:9090 <pi-tailscale-name>
# then in your browser:  http://localhost:9090/dashboard/
```

**Mode B — Tailscale-IP-bound listener** (recommended for daily use;
mirrors the Phase-4 Prometheus pattern described in
`docs/runbooks/prometheus_scrape_setup.md`):

Replace the bind line in `compose.override.yaml` (Pi-local, gitignored)
with the Pi's Tailscale IP from `tailscale ip -4`:

```yaml
# compose.override.yaml — Pi-local, do NOT commit
services:
  api-service:
    ports:
      # Substitute YOUR Pi's Tailscale IPv4 from `tailscale ip -4`.
      # MUST be a tailnet IP, NEVER 0.0.0.0 — Cloudflared / LAN
      # auto-discover host ports and would publish the dashboard.
      - "100.x.y.z:9090:9090"
```

Then recreate the container:

```bash
sops exec-env secrets.yaml 'docker compose up -d --force-recreate api-service'
```

Browser:

```
http://<pi-tailscale-name>:9090/dashboard/
```

Replace `<pi-tailscale-name>` with whatever your Pi is called on your
tailnet (or use the IP directly). The trailing slash matters —
FastAPI `StaticFiles` is mounted at `/dashboard/`.

Smoke check from any Tailscale device once the bind is active:

```bash
curl -fsS http://<pi-tailscale-name>:9090/api/postgrest/v_overview \
  | head -c 200
```

That should return a JSON array (possibly empty if no opps yet
ingested). Anything else — `connection refused`, `403`, hung
connection — is a finding; jump to "Troubleshooting" below.

### Why no 0.0.0.0 bind, ever

The Pi runs Cloudflared and may run other tunnels that auto-publish
host ports. A `0.0.0.0:9090:9090` bind would push the dashboard (and
the proxy that fronts PostgREST) out onto the public internet. The
read-only contract holds against accidental writes; it does NOT hold
against running the proxy unauthenticated on the open net. Always
bind to loopback or a tailnet IP — never `0.0.0.0`.

## Security posture

| Concern                  | Mitigation                                                  |
|--------------------------|-------------------------------------------------------------|
| Public exposure          | No host port mapping on `postgrest`. Cloudflared ACL pins   |
|                          | public ingress to `/webhooks/*`. Dashboard reachable only   |
|                          | over Tailscale.                                              |
| Write smuggling          | api-service proxy refuses POST/PUT/DELETE/PATCH with 405    |
|                          | BEFORE forwarding. PostgREST role `pgrst_anon` has SELECT   |
|                          | only on `dash.*` (no INSERT/UPDATE/DELETE grant anywhere).   |
| Auth-header smuggling    | api-service strips client-set `Authorization` and `Cookie`  |
|                          | headers before forwarding. PostgREST never sees a JWT it    |
|                          | didn't get from a server-side trusted source.                |
| Cross-tenant data leak   | Single-tenant Pi today (user_id=1). The Phase 4.2 multi-    |
|                          | tenant cutover (V017) added the `current_tenant` context,   |
|                          | but Phase 5.2 ships views that read the founding solo       |
|                          | tenant only. Adding multi-tenant filtering is the           |
|                          | precondition for ever exposing this dashboard outside       |
|                          | Tailscale — see "Adding JWT later" below.                    |
| Schema fingerprinting    | PostgREST is configured with `PGRST_DB_SCHEMAS=dash` so     |
|                          | OpenAPI introspection (`GET /`) only enumerates the views,  |
|                          | never the underlying tables.                                 |
| Long-lived sessions      | No sessions. No cookies. Every request is stateless.        |

There is **no JWT today**. The Pi is single-tenant, the dashboard is
behind Tailscale, and adding JWT for a one-user system is more risk
(key management) than reward. The proxy still strips Authorization
defensively so adding JWT later is a code change in one place, not a
re-architecture.

### Adding JWT later (deferred)

If the dashboard ever needs to be exposed outside Tailscale (e.g. a
co-worker joins, or a paid customer audits their own data), the
upgrade path is:

1. Configure PostgREST with `PGRST_JWT_SECRET=<sops-loaded>` and roles
   per tenant (e.g. `tenant_42`, `tenant_43`).
2. Add a `/login` endpoint to `api-service` that issues a signed JWT
   to authenticated users (Discord OAuth2 is the cheapest path —
   already wired for the bot).
3. Have the proxy stamp the trusted server-issued JWT onto the
   forwarded request's `Authorization` header AFTER stripping any
   client-set value. The strip-then-set order is what the existing
   contract test (`tests/api/test_postgrest_proxy.py::
   test_client_authorization_header_stripped`) pins.
4. Update each view to filter by `current_setting('request.jwt.claim.
   tenant_id')::bigint` (or equivalent) so a JWT for tenant 42 can
   never read tenant 43's rows.

None of that is shipped today. Documenting the path so it stays a
flag-flip away rather than a re-architecture.

## Adding a new view

The dashboard's data surface is the set of `dash.v_*` views in
Postgres. Adding a new card or table to the dashboard is a three-file
change:

1. **Write the view** in a new migration `migrations/Vnn__<topic>.sql`
   (use the next free `Vnn` — `ls migrations/` and add one). The view
   MUST live in the `dash` schema, and its grant MUST be SELECT-only:

   ```sql
   BEGIN;

   CREATE OR REPLACE VIEW dash.v_recent_offers AS
   SELECT
       o.id            AS opportunity_id,
       o.title,
       o.company,
       a.sent_at,
       a.response_status
   FROM applications a
   JOIN opportunities o ON o.id = a.opportunity_id
   WHERE a.response_status = 'offer'
     AND a.sent_at > NOW() - INTERVAL '90 days'
   ORDER BY a.sent_at DESC;

   -- pgrst_anon is the un-authenticated PostgREST role. We grant
   -- SELECT, never INSERT/UPDATE/DELETE/TRUNCATE.
   GRANT SELECT ON dash.v_recent_offers TO pgrst_anon;

   INSERT INTO schema_migrations (version) VALUES ('Vnn__recent_offers');

   COMMIT;
   ```

   Run `make migrate-test` before committing — the pre-commit
   `migrate-replay` hook will reject the migration if it has SQL
   errors against an ephemeral pgvector container.

2. **Apply the migration** locally to dev and (on next deploy) to the
   Pi:

   ```bash
   make migrate
   ```

3. **Add the card / table to the frontend**:
   - Pick the view: it lives at `/api/postgrest/v_recent_offers` from
     the browser's perspective (the proxy strips the `/api/postgrest`
     prefix and forwards the rest to PostgREST verbatim).
   - Add a fetch + render block in `dashboard/views/` (one file per
     tab is the convention). Use `getView('v_recent_offers')` from
     `dashboard/lib/postgrest.js`.
   - Append the tab link to `dashboard/index.html`'s `<nav class="tabs">`.

That's it — no rebuild, no redeploy of the frontend (the static mount
serves the new file on next request). The api-service does need a
restart if you've also added a new route to `src/api/main.py`, but
for a new view alone there is no api-service change.

## Troubleshooting

Three real failure modes ranked by frequency:

### 1. PostgREST returns 401

Symptom: the dashboard's view rows say `permission denied for view
v_xxx`. Browser network tab shows `GET /api/postgrest/v_xxx` →
`401`.

Cause: the migration that created the view granted SELECT to
`pgrst_anon` against the table, not the view; or the GRANT statement
was missing entirely; or the view was created in `public` instead of
`dash`.

Fix:

```bash
docker compose exec postgres psql -U "$postgres_user" -d "$postgres_db" -c "
  \\dp dash.v_xxx
"
# Confirm pgrst_anon=r (SELECT). If not, run:
docker compose exec postgres psql -U "$postgres_user" -d "$postgres_db" -c "
  GRANT SELECT ON dash.v_xxx TO pgrst_anon;
"
# PostgREST caches the schema; reload it without restarting the container:
docker compose exec postgres psql -U "$postgres_user" -d "$postgres_db" -c "
  NOTIFY pgrst, 'reload schema';
"
```

If `GRANT` was missing from the migration, also add it via a
follow-up `Vnn` so the fix is reproducible — never patch a migrated
DB directly without leaving a trail.

### 2. Browser shows the "stale" dot

Symptom: the topbar status dot stays in `boot` or transitions to
`error`; the network tab shows `GET /api/postgrest/*` → `502` /
`503` / `504`; or the request hangs and is eventually cancelled by
the client.

Cause: the api-service proxy can't reach `postgrest:3000`.

Diagnose:

```bash
docker compose ps
# expected: postgrest=Up (healthy). If not:
docker compose logs --tail=200 postgrest
```

Common sub-causes:

| Sub-cause                                | Sign in logs                                          | Fix |
|------------------------------------------|-------------------------------------------------------|-----|
| `dash` schema missing                    | `relation "dash.v_overview" does not exist`           | Run `make migrate`; re-check `dash` exists with `\\dn`. |
| `pgrst_anon` role missing                | `role "pgrst_anon" does not exist`                    | V019 didn't apply cleanly; re-run `make migrate`. |
| Postgres unreachable from postgrest      | `could not connect to server: Connection refused`     | `docker compose ps postgres` — restart if `Restarting`. |
| Postgres password mismatch               | `password authentication failed`                      | Confirm `secrets.yaml` decrypted into compose env; restart postgrest. |
| api-service can't resolve `postgrest`    | api-service logs show `Name or service not known`     | api-service didn't join `internal` network; check `compose.yaml`. |

The runbook's hard contract: the proxy returns 502/503/504 within its
own httpx timeout — it must NEVER hang the browser. If you see hung
connections (the dot stays in `boot` for minutes), that is a proxy
bug, not a PostgREST bug; capture the api-service logs and file it
against `src/api/postgrest_proxy.py`.

### 3. View returns wrong data

Symptom: a view returns rows that look stale, empty, or filtered by
the wrong column.

Cause: the view itself, not the wire. PostgREST is a passthrough.

Diagnose directly against Postgres so PostgREST + the proxy are
removed from the picture:

```bash
docker compose exec postgres psql -U "$postgres_user" -d "$postgres_db" -c "
  SELECT * FROM dash.v_xxx LIMIT 5;
"
# If those rows match what the browser sees, the issue is upstream
# (data), not the dashboard. If they DON'T match, PostgREST is
# returning a cached schema — reload it:
docker compose exec postgres psql -U "$postgres_user" -d "$postgres_db" -c "
  NOTIFY pgrst, 'reload schema';
"
# If even that doesn't match, the view's WHERE clause is wrong —
# edit the migration and ship a follow-up Vnn (do NOT patch the DB).
```

## Acceptance

The dashboard is healthy when:

1. `curl -fsS http://<pi-tailscale-name>:9090/dashboard/` returns the
   `index.html` payload (HTTP 200).
2. `curl -fsS http://<pi-tailscale-name>:9090/api/postgrest/v_overview`
   returns a JSON array within 2 seconds. Empty array is acceptable —
   it means no opps yet; non-array or 5xx is a finding.
3. `tests/api/test_postgrest_proxy.py` is green (`uv run pytest
   tests/api/test_postgrest_proxy.py -q`). This pins the read-only
   contract; if it ever goes red, do NOT ship — write paths or
   header-smuggling regressions are not allowed to land.
4. `docker compose ps postgrest` shows no host port published — only
   the internal `3000/tcp` port. If the port column shows `0.0.0.0:`
   or `:::`, the security posture is broken; revert the compose
   change.

## Out of scope (deferred to later phases)

- **Authentication on the dashboard.** Single-tenant Pi today; see
  "Adding JWT later" above for the upgrade path.
- **Write endpoints.** The dashboard is read-only by design. If the
  apply / skip / snooze actions need a web UI in future, they live as
  POST endpoints under `/admin/*` (which already exist for the
  Discord bot's reaction handler), behind the same Tailscale ACL —
  NOT smuggled into PostgREST.
- **Cross-tenant views.** Multi-tenant filtering is the precondition
  for ever exposing the dashboard outside Tailscale. Phase 4.2 added
  the resolver; the views ship single-tenant for now.
- **Realtime updates.** No WebSocket / SSE. Auto-refresh is a 60s
  poll from the frontend; that's sufficient for a 5-applies/day
  pipeline and avoids an extra connection per browser tab.
- **External APM / SaaS observability.** `node_filesystem_avail` and
  the existing Prometheus + Grafana stack already cover the Pi; the
  dashboard adds zero new monitored services beyond `postgrest`
  itself, which Prometheus picks up via its container scrape.

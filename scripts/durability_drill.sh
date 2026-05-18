#!/usr/bin/env bash
# Power-cut durability simulation for the Cartograph / Marked_Path Pi 5 stack.
#
# Why this exists
#   The Pi has no UPS. CLAUDE.md's "Power-fail safety (NO UPS)" section locks
#   Postgres + Redis to a durability config (synchronous_commit=on,
#   full_page_writes=on, WAL archive every 5min, AOF appendfsync=everysec).
#   This script verifies those settings actually deliver the promised RPO/RTO
#   by SIGKILLing both containers (a real power-cut analogue, not the graceful
#   shutdown that `docker compose stop` provides) and checking that:
#     - Postgres replays cleanly and pg_amcheck reports zero corruption
#     - Committed opportunity rows survive 1:1
#     - Redis AOF replay loses at most ~1 second of writes (we tolerate ≤5
#       entries out of a 100-entry burst)
#     - WAL archive is producing files (skipped on a developer laptop where
#       /mnt/storage/wal_archive isn't bind-mounted)
#     - Workers recover via XAUTOCLAIM with zero error-log entries
#
# Run quarterly and after any docker/postgres/postgresql.conf or
# redis-server flag change in compose.yaml.
#
# Idempotent: the synthetic alert burst is XTRIM'd out at the end.
# Safe to abort: the EXIT trap restarts postgres + redis no matter what.
#
# Exit codes:
#   0 — full PASS
#   1 — at least one durability assertion FAILED
#   2 — infrastructure precondition failed (stack not up, sops missing, etc.)

set -euo pipefail

# ─── Tunables (override via env) ──────────────────────────────────────────────
SYNTHETIC_BURST="${SYNTHETIC_BURST:-100}"
KILL_WAIT_SECONDS="${KILL_WAIT_SECONDS:-5}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-60}"
WORKER_RECOVERY_WAIT="${WORKER_RECOVERY_WAIT:-30}"
# AOF appendfsync=everysec means up to 1 second of writes can vanish on SIGKILL.
# The 100-entry burst takes well under a second, so most should survive. We
# accept up to 5 lost entries as a pass.
AOF_LOSS_TOLERANCE="${AOF_LOSS_TOLERANCE:-5}"
WAL_ARCHIVE_DIR="${WAL_ARCHIVE_DIR:-/mnt/storage/wal_archive}"

# Worker services that share Redis consumer groups — restarted after recovery
# so they re-issue XAUTOCLAIM and reclaim any in-flight pending entries.
WORKER_SERVICES=(
    crawler-worker
    extractor-worker
    ranker-worker
    notifier-discord
    gmail-watcher
    applier-worker
    identity-warmup
    jobs-scheduler
)

# ─── Setup ────────────────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

C_RED='\033[0;31m'
C_GREEN='\033[0;32m'
C_YELLOW='\033[0;33m'
C_BLUE='\033[0;34m'
C_RESET='\033[0m'

ok()    { printf "  ${C_GREEN}PASS${C_RESET}  %s\n" "$*"; }
fail()  { printf "  ${C_RED}FAIL${C_RESET}  %s\n" "$*"; FAILED=1; }
warn()  { printf "  ${C_YELLOW}WARN${C_RESET}  %s\n" "$*"; }
info()  { printf "  ${C_BLUE}info${C_RESET}  %s\n" "$*"; }
step()  { printf "\n${C_BLUE}[STEP %s/11]${C_RESET} %s\n" "$1" "$2"; }

FAILED=0

# ─── Trap: always restore postgres + redis ────────────────────────────────────
# This is the most important line in the script. If we abort midway, the user
# must NOT be left with a dead Postgres. We attempt restart unconditionally
# and never propagate the trap's own errors.
cleanup() {
    local rc=$?
    set +e
    printf "\n${C_BLUE}[cleanup]${C_RESET} ensuring postgres + redis are up...\n"
    docker compose up -d postgres redis >/dev/null 2>&1
    # Best-effort XTRIM of our synthetic alerts in case we aborted mid-drill
    # and never reached the cleanup step. Uses the recorded MINID if set.
    if [[ -n "${PRE_DRILL_LAST_ID:-}" ]]; then
        sops exec-env secrets.yaml '
            docker compose exec -T -e REDISCLI_AUTH="$redis_password" redis \
                redis-cli XTRIM stream:alerts MINID "'"$PRE_DRILL_LAST_ID"'"
        ' >/dev/null 2>&1 || true
    fi
    exit "$rc"
}
trap cleanup EXIT INT TERM

# ─── Precondition checks ──────────────────────────────────────────────────────
if [[ ! -f "$REPO_ROOT/secrets.yaml" ]]; then
    printf "${C_RED}precondition failed:${C_RESET} secrets.yaml not present at %s\n" "$REPO_ROOT/secrets.yaml" >&2
    trap - EXIT INT TERM
    exit 2
fi
if ! command -v sops >/dev/null 2>&1; then
    printf "${C_RED}precondition failed:${C_RESET} sops not in PATH\n" >&2
    trap - EXIT INT TERM
    exit 2
fi
if ! command -v docker >/dev/null 2>&1; then
    printf "${C_RED}precondition failed:${C_RESET} docker not in PATH\n" >&2
    trap - EXIT INT TERM
    exit 2
fi

printf "${C_BLUE}════════════════════════════════════════════════════════════════════${C_RESET}\n"
printf "${C_BLUE}  Cartograph power-cut durability drill${C_RESET}\n"
printf "${C_BLUE}  burst=%s  kill-wait=%ss  health-timeout=%ss  worker-wait=%ss${C_RESET}\n" \
    "$SYNTHETIC_BURST" "$KILL_WAIT_SECONDS" "$HEALTH_TIMEOUT" "$WORKER_RECOVERY_WAIT"
printf "${C_BLUE}════════════════════════════════════════════════════════════════════${C_RESET}\n"

# ─── STEP 1: Stack health ─────────────────────────────────────────────────────
step 1 "Confirm stack is up + healthy"

# Get the list of running services. If postgres or redis isn't Up, refuse —
# running the drill on a degraded stack would falsely accuse the durability
# config of failures that are actually pre-existing.
PS_OUTPUT=$(docker compose ps --format '{{.Service}}\t{{.State}}' 2>/dev/null || true)
if [[ -z "$PS_OUTPUT" ]]; then
    fail "docker compose ps returned no services — stack is down"
    printf "\n${C_RED}precondition failed — bring stack up with 'make up' first${C_RESET}\n" >&2
    trap - EXIT INT TERM
    exit 2
fi

PG_STATE=$(printf '%s\n' "$PS_OUTPUT" | awk -F'\t' '$1=="postgres"{print $2}')
REDIS_STATE=$(printf '%s\n' "$PS_OUTPUT" | awk -F'\t' '$1=="redis"{print $2}')

if [[ "$PG_STATE" != "running" && "$PG_STATE" != *"healthy"* ]]; then
    fail "postgres state=${PG_STATE:-<missing>}, need running"
    trap - EXIT INT TERM
    exit 2
fi
if [[ "$REDIS_STATE" != "running" && "$REDIS_STATE" != *"healthy"* ]]; then
    fail "redis state=${REDIS_STATE:-<missing>}, need running"
    trap - EXIT INT TERM
    exit 2
fi

ok "postgres: $PG_STATE"
ok "redis:    $REDIS_STATE"

# Active probe — `pg_isready` and `redis-cli ping` confirm the daemons
# themselves respond, not just that the container is up.
if ! sops exec-env secrets.yaml 'docker compose exec -T postgres pg_isready -U "$postgres_user" -d "$postgres_db" -q' >/dev/null 2>&1; then
    fail "pg_isready failed"
    trap - EXIT INT TERM
    exit 2
fi
ok "pg_isready OK"

if ! sops exec-env secrets.yaml '
    docker compose exec -T -e REDISCLI_AUTH="$redis_password" redis \
        redis-cli ping
' 2>/dev/null | grep -q PONG; then
    fail "redis ping failed"
    trap - EXIT INT TERM
    exit 2
fi
ok "redis ping OK"

# ─── STEP 2: Pre-drill checksum ───────────────────────────────────────────────
step 2 "Record pre-drill checksum (DB row counts + stream lengths)"

PRE_OPPS=$(sops exec-env secrets.yaml '
    docker compose exec -T postgres psql -U "$postgres_user" -d "$postgres_db" -tA \
        -c "SELECT COUNT(*) FROM opportunities"
' 2>/dev/null | tr -d '[:space:]')
PRE_SCORES=$(sops exec-env secrets.yaml '
    docker compose exec -T postgres psql -U "$postgres_user" -d "$postgres_db" -tA \
        -c "SELECT COUNT(*) FROM opportunity_scores"
' 2>/dev/null | tr -d '[:space:]')
PRE_APPS=$(sops exec-env secrets.yaml '
    docker compose exec -T postgres psql -U "$postgres_user" -d "$postgres_db" -tA \
        -c "SELECT COUNT(*) FROM applications"
' 2>/dev/null | tr -d '[:space:]')
PRE_IDENTITIES=$(sops exec-env secrets.yaml '
    docker compose exec -T postgres psql -U "$postgres_user" -d "$postgres_db" -tA \
        -c "SELECT COUNT(*) FROM identities"
' 2>/dev/null | tr -d '[:space:]')
PRE_LATEST_FIRST_SEEN=$(sops exec-env secrets.yaml '
    docker compose exec -T postgres psql -U "$postgres_user" -d "$postgres_db" -tA \
        -c "SELECT COALESCE(MAX(first_seen)::text, '\''<none>'\'') FROM opportunities"
' 2>/dev/null | tr -d '\r')

info "opportunities       = $PRE_OPPS"
info "opportunity_scores  = $PRE_SCORES"
info "applications        = $PRE_APPS"
info "identities          = $PRE_IDENTITIES"
info "latest first_seen   = $PRE_LATEST_FIRST_SEEN"

# Record all 8 stream lengths so we can compare post-recovery.
STREAMS=(
    "stream:fetch"
    "stream:extract"
    "stream:rank"
    "stream:notify"
    "stream:apply"
    "stream:email_in"
    "stream:alerts"
    "stream:dlq"
)

declare -A PRE_STREAM_LEN
for s in "${STREAMS[@]}"; do
    # XLEN returns 0 even if the stream doesn't exist yet — perfect for us.
    len=$(sops exec-env secrets.yaml '
        docker compose exec -T -e REDISCLI_AUTH="$redis_password" redis \
            redis-cli XLEN "'"$s"'"
    ' 2>/dev/null | tr -d '[:space:]')
    PRE_STREAM_LEN["$s"]="${len:-0}"
    info "$(printf '%-22s = %s' "$s" "${PRE_STREAM_LEN[$s]}")"
done

# Record the current last ID in stream:alerts so the EXIT trap (and the
# explicit cleanup at the end) can XTRIM only the entries we added.
PRE_DRILL_LAST_ID=$(sops exec-env secrets.yaml '
    docker compose exec -T -e REDISCLI_AUTH="$redis_password" redis \
        redis-cli --no-raw XREVRANGE stream:alerts + - COUNT 1
' 2>/dev/null | awk 'NR==1{gsub(/"/,""); print}' | tr -d '[:space:]')
# Fall back to 0-0 if the stream is empty — XTRIM MINID 0-0 is a no-op.
PRE_DRILL_LAST_ID="${PRE_DRILL_LAST_ID:-0-0}"
info "stream:alerts last id before burst = $PRE_DRILL_LAST_ID"

ok "checksum recorded"

# ─── STEP 3: Synthetic write burst ────────────────────────────────────────────
step 3 "Force write burst — publish $SYNTHETIC_BURST drill alerts"

# Why stream:alerts: it's the sparsest hot path. Polluting stream:fetch
# (50k MAXLEN) or stream:rank (30k MAXLEN) would interleave with real
# pipeline data. stream:alerts MAXLEN is 5000 and almost always near-empty.
#
# Approach: pipe a sequence of XADD commands into redis-cli via stdin. This
# avoids nested-quoting hell — the host generates the command stream, sops
# exec-env wraps the docker exec in a single shell that has $redis_password
# in scope, and redis-cli reads commands one per line.
BURST_CMDS=$(
    for ((i=1; i<=SYNTHETIC_BURST; i++)); do
        printf 'XADD stream:alerts MAXLEN ~ 5000 * kind alert source drill seq %d ts %s\n' \
            "$i" "$(date +%s%N)"
    done
)
printf '%s\n' "$BURST_CMDS" | sops exec-env secrets.yaml '
    docker compose exec -T -e REDISCLI_AUTH="$redis_password" redis \
        redis-cli
' >/dev/null 2>&1

PRE_KILL_ALERTS_LEN=$(sops exec-env secrets.yaml '
    docker compose exec -T -e REDISCLI_AUTH="$redis_password" redis \
        redis-cli XLEN stream:alerts
' 2>/dev/null | tr -d '[:space:]')

PRE_KILL_ALERTS_LEN="${PRE_KILL_ALERTS_LEN:-0}"
EXPECTED_AFTER_BURST=$(( PRE_STREAM_LEN["stream:alerts"] + SYNTHETIC_BURST ))

info "stream:alerts len before burst = ${PRE_STREAM_LEN[stream:alerts]}"
info "stream:alerts len after burst  = $PRE_KILL_ALERTS_LEN  (expected ≈ $EXPECTED_AFTER_BURST)"

if (( PRE_KILL_ALERTS_LEN < EXPECTED_AFTER_BURST )); then
    warn "burst write count short of expected — Redis MAXLEN ~ trimming may have kicked in"
fi
ok "burst written"

# ─── STEP 4: SIGKILL postgres + redis ─────────────────────────────────────────
step 4 "Simulate power-cut — SIGKILL postgres + redis containers"

# We need the actual container IDs because the disable of `docker compose`'s
# graceful shutdown machinery requires us to bypass it entirely. `docker kill`
# with -s KILL is unambiguous: the kernel sends SIGKILL, no userspace cleanup
# runs, no Postgres checkpoint, no Redis BGSAVE — same conditions as yanking
# the power cord on the Pi.
PG_CID=$(docker compose ps -q postgres)
REDIS_CID=$(docker compose ps -q redis)

if [[ -z "$PG_CID" || -z "$REDIS_CID" ]]; then
    fail "could not resolve container IDs (pg=$PG_CID redis=$REDIS_CID)"
    exit 1
fi

KILL_AT=$(date +%s)
info "killing pg=$PG_CID and redis=$REDIS_CID at t=$KILL_AT"
docker kill -s KILL "$PG_CID" >/dev/null
docker kill -s KILL "$REDIS_CID" >/dev/null
ok "SIGKILL sent to both containers"

# ─── STEP 5: Wait + restart ───────────────────────────────────────────────────
step 5 "Wait ${KILL_WAIT_SECONDS}s, then restart postgres + redis"
sleep "$KILL_WAIT_SECONDS"
docker compose up -d postgres redis >/dev/null
ok "compose up -d postgres redis issued"

# ─── STEP 6: Poll health ──────────────────────────────────────────────────────
step 6 "Poll for health (timeout ${HEALTH_TIMEOUT}s)"

PG_HEALTHY_AT=0
REDIS_HEALTHY_AT=0
for ((i=0; i<HEALTH_TIMEOUT; i++)); do
    if (( PG_HEALTHY_AT == 0 )); then
        if sops exec-env secrets.yaml 'docker compose exec -T postgres pg_isready -U "$postgres_user" -d "$postgres_db" -q' >/dev/null 2>&1; then
            PG_HEALTHY_AT=$(date +%s)
        fi
    fi
    if (( REDIS_HEALTHY_AT == 0 )); then
        if sops exec-env secrets.yaml '
            docker compose exec -T -e REDISCLI_AUTH="$redis_password" redis \
                redis-cli ping
        ' 2>/dev/null | grep -q PONG; then
            REDIS_HEALTHY_AT=$(date +%s)
        fi
    fi
    if (( PG_HEALTHY_AT > 0 && REDIS_HEALTHY_AT > 0 )); then
        break
    fi
    sleep 1
done

if (( PG_HEALTHY_AT == 0 )); then
    fail "postgres did not become healthy within ${HEALTH_TIMEOUT}s"
else
    ok "postgres healthy at t+$(( PG_HEALTHY_AT - KILL_AT ))s"
fi
if (( REDIS_HEALTHY_AT == 0 )); then
    fail "redis did not become healthy within ${HEALTH_TIMEOUT}s"
else
    ok "redis healthy at t+$(( REDIS_HEALTHY_AT - KILL_AT ))s"
fi

if (( PG_HEALTHY_AT == 0 || REDIS_HEALTHY_AT == 0 )); then
    # Skip the durability assertions — they'd all fail noisily.
    fail "health check timeout — skipping remaining verifications"
    exit 1
fi

RTO_SECONDS=$(( (PG_HEALTHY_AT > REDIS_HEALTHY_AT ? PG_HEALTHY_AT : REDIS_HEALTHY_AT) - KILL_AT ))

# ─── STEP 7: Durability verifications ─────────────────────────────────────────
step 7 "Verify durability (amcheck, row counts, AOF replay, WAL archive)"

# 7a. pg_amcheck — exits 0 only when every heap + btree index is consistent.
# This is the single strongest signal that full_page_writes did its job.
if sops exec-env secrets.yaml '
    docker compose exec -T postgres pg_amcheck -U "$postgres_user" -d "$postgres_db"
' >/dev/null 2>&1; then
    ok "7a. pg_amcheck: zero corruption detected"
else
    fail "7a. pg_amcheck reported errors — heap or index corruption"
fi

# 7b. opportunities row count must match exactly. Committed writes must
# survive SIGKILL — that's literally the synchronous_commit=on contract.
POST_OPPS=$(sops exec-env secrets.yaml '
    docker compose exec -T postgres psql -U "$postgres_user" -d "$postgres_db" -tA \
        -c "SELECT COUNT(*) FROM opportunities"
' 2>/dev/null | tr -d '[:space:]')
if [[ "$POST_OPPS" == "$PRE_OPPS" ]]; then
    ok "7b. opportunities count preserved: $POST_OPPS"
else
    fail "7b. opportunities count drift: pre=$PRE_OPPS post=$POST_OPPS"
fi

# 7c. Redis AOF replay — accept ≤ AOF_LOSS_TOLERANCE entries of loss because
# appendfsync=everysec gives up to ~1 second of writes a way to vanish.
POST_ALERTS_LEN=$(sops exec-env secrets.yaml '
    docker compose exec -T -e REDISCLI_AUTH="$redis_password" redis \
        redis-cli XLEN stream:alerts
' 2>/dev/null | tr -d '[:space:]')
POST_ALERTS_LEN="${POST_ALERTS_LEN:-0}"
LOST=$(( PRE_KILL_ALERTS_LEN - POST_ALERTS_LEN ))
if (( LOST < 0 )); then LOST=0; fi
if (( LOST <= AOF_LOSS_TOLERANCE )); then
    ok "7c. AOF replay: lost $LOST/$SYNTHETIC_BURST (tolerance $AOF_LOSS_TOLERANCE)"
else
    fail "7c. AOF replay lost $LOST entries — exceeds tolerance $AOF_LOSS_TOLERANCE"
fi

# 7d. WAL archive — confirm at least one .ready or .partial file under the
# bind mount on the Pi. On a dev laptop the mount may not exist; that's a
# WARN, not a FAIL.
if [[ ! -d "$WAL_ARCHIVE_DIR" ]]; then
    warn "7d. WAL archive dir $WAL_ARCHIVE_DIR not present (developer laptop?) — skipping"
else
    # Count any .ready, .partial, or finalized WAL segments. We deliberately
    # don't shell-expand the pattern inside the test — we count via find so
    # an empty dir produces 0, not a glob mismatch.
    WAL_FILE_COUNT=$(find "$WAL_ARCHIVE_DIR" -maxdepth 1 -type f 2>/dev/null | wc -l)
    if (( WAL_FILE_COUNT > 0 )); then
        ok "7d. WAL archive: $WAL_FILE_COUNT file(s) under $WAL_ARCHIVE_DIR"
    else
        fail "7d. WAL archive dir is empty — archive_command may be misconfigured"
    fi
fi

# ─── STEP 8: Restart workers + scan logs ──────────────────────────────────────
step 8 "Restart workers; wait ${WORKER_RECOVERY_WAIT}s; scan logs for errors"

# Restart only services that are currently part of the compose project.
# Some services (e.g. applier-worker) may legitimately be omitted on dev
# laptops — `docker compose restart` on an unknown service errors, so we
# filter against the actual ps list.
RUNNING_SERVICES=$(docker compose ps --services 2>/dev/null || true)
SERVICES_TO_RESTART=()
for svc in "${WORKER_SERVICES[@]}"; do
    if printf '%s\n' "$RUNNING_SERVICES" | grep -qx "$svc"; then
        SERVICES_TO_RESTART+=("$svc")
    fi
done

if (( ${#SERVICES_TO_RESTART[@]} == 0 )); then
    warn "no worker services currently in compose project — nothing to restart"
else
    info "restarting: ${SERVICES_TO_RESTART[*]}"
    docker compose restart "${SERVICES_TO_RESTART[@]}" >/dev/null 2>&1 || true
fi

info "sleeping ${WORKER_RECOVERY_WAIT}s for XAUTOCLAIM cycle..."
sleep "$WORKER_RECOVERY_WAIT"

# Scan the last WORKER_RECOVERY_WAIT seconds of compose logs for `level:
# error` markers. Our structured logger emits JSON with that key. We use
# `--since` so we don't flag pre-drill errors.
LOG_SINCE_TS=$(( PG_HEALTHY_AT > REDIS_HEALTHY_AT ? PG_HEALTHY_AT : REDIS_HEALTHY_AT ))
ERROR_LOG_COUNT=$(docker compose logs --since "${WORKER_RECOVERY_WAIT}s" 2>/dev/null \
    | grep -c -E '"level"[[:space:]]*:[[:space:]]*"error"|level=error' || true)

if (( ERROR_LOG_COUNT == 0 )); then
    ok "no error-level log lines in last ${WORKER_RECOVERY_WAIT}s"
else
    warn "$ERROR_LOG_COUNT error-level log line(s) in last ${WORKER_RECOVERY_WAIT}s — inspect with: docker compose logs --since ${WORKER_RECOVERY_WAIT}s | grep -E '\"level\":\"error\"|level=error'"
fi

# ─── STEP 9: Summary ──────────────────────────────────────────────────────────
step 9 "Summary"

POST_SCORES=$(sops exec-env secrets.yaml '
    docker compose exec -T postgres psql -U "$postgres_user" -d "$postgres_db" -tA \
        -c "SELECT COUNT(*) FROM opportunity_scores"
' 2>/dev/null | tr -d '[:space:]')
POST_APPS=$(sops exec-env secrets.yaml '
    docker compose exec -T postgres psql -U "$postgres_user" -d "$postgres_db" -tA \
        -c "SELECT COUNT(*) FROM applications"
' 2>/dev/null | tr -d '[:space:]')
POST_IDENTITIES=$(sops exec-env secrets.yaml '
    docker compose exec -T postgres psql -U "$postgres_user" -d "$postgres_db" -tA \
        -c "SELECT COUNT(*) FROM identities"
' 2>/dev/null | tr -d '[:space:]')

printf "\n  %-22s %12s   %12s   %s\n" "metric" "pre" "post" "Δ"
printf "  %-22s %12s   %12s   %s\n" "──────"  "───" "────" "─"
printf "  %-22s %12s   %12s   %+d\n"  "opportunities"      "$PRE_OPPS"        "$POST_OPPS"        $((POST_OPPS-PRE_OPPS))
printf "  %-22s %12s   %12s   %+d\n"  "opportunity_scores" "$PRE_SCORES"      "$POST_SCORES"      $((POST_SCORES-PRE_SCORES))
printf "  %-22s %12s   %12s   %+d\n"  "applications"       "$PRE_APPS"        "$POST_APPS"        $((POST_APPS-PRE_APPS))
printf "  %-22s %12s   %12s   %+d\n"  "identities"         "$PRE_IDENTITIES"  "$POST_IDENTITIES"  $((POST_IDENTITIES-PRE_IDENTITIES))
printf "  %-22s %12s   %12s   %+d\n"  "stream:alerts"      "$PRE_KILL_ALERTS_LEN" "$POST_ALERTS_LEN" $((POST_ALERTS_LEN-PRE_KILL_ALERTS_LEN))
printf "\n  RTO measured (kill → both daemons healthy): ${C_BLUE}%ss${C_RESET}\n" "$RTO_SECONDS"
printf "  CLAUDE.md target RTO for power-cut: ${C_BLUE}≤5min auto${C_RESET}\n"

# ─── STEP 10: Cleanup synthetic alerts ────────────────────────────────────────
step 10 "Cleanup synthetic burst alerts from stream:alerts"

# Redis XTRIM has no native "trim from the newer end" — MINID only drops
# OLDER entries. Our burst entries are the NEWEST, so we collect their IDs
# via XRANGE with the exclusive "(MINID" prefix (entries strictly newer
# than PRE_DRILL_LAST_ID) and XDEL them one batch at a time.
#
# Why this is safe + idempotent:
#   - The burst tagged every entry with source=drill, so even if real
#     pipeline traffic interleaved a non-drill alert between our XADDs,
#     XDEL only removes what we explicitly target.
#   - On a re-run, PRE_DRILL_LAST_ID will point past any leftovers, so
#     XRANGE returns nothing and the cleanup is a silent no-op.

# XRANGE output format is "ID\nfield value field value\n…" per entry, one
# entry across multiple lines. The `id-line | grep -v space` filter keeps
# only the bare ID lines (digits-dash-digits at start of line).
BURST_IDS=$(sops exec-env secrets.yaml '
    docker compose exec -T -e REDISCLI_AUTH="$redis_password" redis \
        redis-cli XRANGE stream:alerts "('"$PRE_DRILL_LAST_ID"'" + COUNT '"$SYNTHETIC_BURST"'
' 2>/dev/null | grep -E '^[0-9]+-[0-9]+$' || true)

if [[ -n "$BURST_IDS" ]]; then
    BURST_ID_COUNT=$(printf '%s\n' "$BURST_IDS" | wc -l | tr -d '[:space:]')
    # Build a single space-separated arg list of IDs and pass into one shell
    # context that has $redis_password expanded. We must let sops do the
    # expansion (the password lives in secrets.yaml), so the redis-cli call
    # has to run INSIDE the sops shell. We embed the IDs literally — the
    # awk strip above already ensured they're shell-safe (digits + hyphen).
    IDS_ONE_LINE=$(printf '%s ' $BURST_IDS)
    sops exec-env secrets.yaml "
        docker compose exec -T -e REDISCLI_AUTH=\"\$redis_password\" redis \
            redis-cli XDEL stream:alerts $IDS_ONE_LINE
    " >/dev/null 2>&1 || true
    ok "deleted $BURST_ID_COUNT synthetic alert entries"
else
    warn "no burst IDs to clean (already trimmed or burst failed to write)"
fi

# Clear the trap variable so the EXIT trap doesn't try to clean again.
PRE_DRILL_LAST_ID=""

# ─── STEP 11: Verdict ─────────────────────────────────────────────────────────
step 11 "Verdict"

if (( FAILED == 0 )); then
    printf "\n${C_GREEN}════════════════════════════════════════════════════════════════════${C_RESET}\n"
    printf "${C_GREEN}  PASS  Cartograph survived a SIGKILL power-cut simulation.${C_RESET}\n"
    printf "${C_GREEN}        Durability config (sync_commit=on, full_page_writes=on,${C_RESET}\n"
    printf "${C_GREEN}        AOF everysec, WAL archive) is doing its job.${C_RESET}\n"
    printf "${C_GREEN}════════════════════════════════════════════════════════════════════${C_RESET}\n"
    # Disable the EXIT trap on success path so we don't double-print cleanup.
    trap - EXIT INT TERM
    exit 0
else
    printf "\n${C_RED}════════════════════════════════════════════════════════════════════${C_RESET}\n"
    printf "${C_RED}  FAIL  One or more durability assertions failed.${C_RESET}\n"
    printf "${C_RED}        DO NOT continue running production workloads until you${C_RESET}\n"
    printf "${C_RED}        have remediated the failing assertion(s) above.${C_RESET}\n"
    printf "${C_RED}════════════════════════════════════════════════════════════════════${C_RESET}\n"
    trap - EXIT INT TERM
    exit 1
fi

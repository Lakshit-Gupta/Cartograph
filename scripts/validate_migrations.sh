#!/usr/bin/env bash
# Replay every migrations/V*.sql against an ephemeral pgvector container.
# Catches PostgresSyntaxError, InvalidObjectDefinitionError, ordering bugs,
# missing extensions, etc. BEFORE the SQL is committed.
#
# Why ephemeral + tmpfs:
#   - Real PG engine catches the failure class static linters miss
#     (non-IMMUTABLE function in partial index, PK with function call,
#      gin_trgm_ops without pg_trgm extension, etc).
#   - tmpfs data dir keeps the run fast (~3-15s) and makes prod
#     durability config (synchronous_commit=on, full_page_writes=on)
#     irrelevant — this container never touches disk.
#   - pgvector/pgvector:pg16 is multi-arch (amd64 + arm64), so this
#     script runs identically on laptop and Pi 5.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MIG_DIR="${REPO_ROOT}/migrations"
IMAGE="pgvector/pgvector:pg16"
CONTAINER="marked-path-migrate-validate-$$"

if [ ! -d "$MIG_DIR" ]; then
    echo "no migrations/ dir at $MIG_DIR" >&2
    exit 1
fi

cleanup() {
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

echo "→ spinning ephemeral $IMAGE as $CONTAINER"
docker run -d --rm \
    --name "$CONTAINER" \
    --tmpfs /var/lib/postgresql/data \
    -e POSTGRES_PASSWORD=validate \
    -e POSTGRES_DB=cartograph \
    -e POSTGRES_USER=postgres \
    "$IMAGE" >/dev/null

echo -n "→ waiting for postgres ready"
for _ in $(seq 1 60); do
    if docker exec "$CONTAINER" pg_isready -U postgres -q 2>/dev/null; then
        echo " ✓"
        break
    fi
    echo -n "."
    sleep 0.5
done

if ! docker exec "$CONTAINER" pg_isready -U postgres -q; then
    echo " ✗ postgres never became ready" >&2
    docker logs "$CONTAINER" >&2 || true
    exit 1
fi

# Replay every V*.sql in V-number order. ON_ERROR_STOP=1 aborts on the first
# error per file. Every migration file already wraps its body in BEGIN/COMMIT,
# so we deliberately do NOT pass --single-transaction here — psql complains
# about nested BEGINs and it just adds noise. If a future migration forgets
# its BEGIN/COMMIT, V-number ordering still aborts the whole replay on first
# error and leaves the ephemeral container in a known-broken state we throw
# away anyway.
shopt -s nullglob
mapfile -t files < <(printf '%s\n' "$MIG_DIR"/V*.sql | sort -V)

if [ ${#files[@]} -eq 0 ]; then
    echo "no V*.sql files in $MIG_DIR — nothing to validate" >&2
    exit 1
fi

for f in "${files[@]}"; do
    name="$(basename "$f")"
    echo "→ applying $name"
    if ! docker exec -i "$CONTAINER" \
            psql -U postgres -d cartograph \
                 -v ON_ERROR_STOP=1 \
                 -q < "$f"; then
        echo "✗ $name failed on ephemeral postgres" >&2
        exit 1
    fi
done

echo "✓ all ${#files[@]} migrations replay clean against $IMAGE"

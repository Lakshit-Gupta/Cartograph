#!/usr/bin/env bash
# Cross-compile the Marked_Path image set for linux/arm64 (Pi 5) on this
# x86_64 dev host, then ship to the Pi via SSH tarball load.
#
# Why this exists: building the full image set ON the Pi takes >1h (uv sync
# alone is ~25 min on ARM CPU). Building under QEMU emulation on the dev box
# finishes in ~20 min and pushes ~10 GB of compressed images over LAN/Tailscale.
#
# Usage:
#   PI_HOST=user@pi-tailscale-name.tail-xxxx.ts.net \
#   scripts/ship_to_pi.sh
#
# Optional env overrides:
#   PI_REMOTE_DIR=/home/user/marked_path     # repo path on Pi, default below
#   BUILD_TAG=arm64                          # tag suffix, default below
#   PARALLEL_LOAD=0                          # set to 1 to pipeline xz|load
#   SKIP_BROWSER=1                           # skip camoufox image (it's heavy
#                                            # and currently restart-looping)
#   SKIP_BUILD=1                             # reuse already-built arm64 images
#   SKIP_PUSH=1                              # dry-run: build only, no ssh
#
# Hard rules:
#   * Never bake secrets into images. Pi's secrets.yaml stays on disk.
#   * Never publish ports on the Pi side beyond what compose.yaml already does
#     (Tailscale-only; no host bindings).
#   * On failure, no partial state on the Pi: we `docker load` only after the
#     full transfer succeeds, and we `compose up` only after all loads pass.
set -euo pipefail

PI_HOST="${PI_HOST:-}"
PI_REMOTE_DIR="${PI_REMOTE_DIR:-/home/lakshit_gupta/coding/Marked_Path}"
BUILD_TAG="${BUILD_TAG:-arm64}"
PARALLEL_LOAD="${PARALLEL_LOAD:-0}"
SKIP_BROWSER="${SKIP_BROWSER:-0}"
SKIP_BUILD="${SKIP_BUILD:-0}"
SKIP_PUSH="${SKIP_PUSH:-0}"

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if [[ -z "$PI_HOST" && "$SKIP_PUSH" != "1" ]]; then
  echo "PI_HOST must be set (e.g. PI_HOST=lakshit@pi.tail-xxxx.ts.net) or SKIP_PUSH=1 for dry run." >&2
  exit 2
fi

# Image set we OWN (built locally). External images (pgvector, redis,
# flaresolverr, postgrest) Pi pulls directly from Docker Hub — no need
# to cross-compile + ship those.
declare -a OWNED_IMAGES=(
  "marked_path-jobs-bot:latest|docker/jobs-bot.Dockerfile|."
  "marked_path-tools:latest|docker/tools.Dockerfile|."
  "marked_path-applier-worker:latest|docker/applier.Dockerfile|."
)
if [[ "$SKIP_BROWSER" != "1" ]]; then
  OWNED_IMAGES+=( "marked_path-camoufox-worker:latest|docker/camoufox.Dockerfile|." )
fi

# Buildx setup (idempotent). Two paths:
#   * If the host's Docker daemon runs with the containerd snapshotter
#     (modern Docker Desktop / Docker CE with `features.containerd-snapshotter`
#     enabled), the DEFAULT `docker` driver supports multi-platform builds
#     directly via the host's image store. This is faster + reuses the host's
#     network/DNS + the local image cache, so we prefer it.
#   * Otherwise we fall back to a dedicated `docker-container` driver builder.
#     The docker-container builder has its own isolated cache + network — slower
#     to warm + occasionally flaky DNS — but works on stock daemons.
#
# Override the auto-detect via `BUILDER_NAME=foo BUILDER_DRIVER=docker-container`.
BUILDER_NAME="${BUILDER_NAME:-}"
BUILDER_DRIVER="${BUILDER_DRIVER:-}"

ensure_buildx() {
  echo "==> ensuring buildx + qemu emulators"
  # Install qemu-user-static binfmt entries. Idempotent.
  docker run --privileged --rm tonistiigi/binfmt --install arm64 >/dev/null 2>&1 || true

  if [[ -z "$BUILDER_NAME" ]]; then
    # Auto-detect: containerd snapshotter -> use default builder.
    if docker info --format '{{.DriverStatus}}' 2>/dev/null | grep -q "io.containerd.snapshotter.v1"; then
      BUILDER_NAME="default"
      echo "==>   containerd snapshotter detected, using default docker driver"
    else
      BUILDER_NAME="cartograph-cross"
      BUILDER_DRIVER="docker-container"
    fi
  fi

  if [[ "$BUILDER_NAME" != "default" ]]; then
    if ! docker buildx inspect "$BUILDER_NAME" >/dev/null 2>&1; then
      # `network=host` so the builder uses the host's DNS — otherwise the
      # docker-container builder's isolated resolver can EAI_AGAIN on
      # files.pythonhosted.org / ghcr.io.
      docker buildx create --name "$BUILDER_NAME" \
        --driver "${BUILDER_DRIVER:-docker-container}" \
        --driver-opt network=host \
        --use >/dev/null
    fi
  fi
  docker buildx use "$BUILDER_NAME" >/dev/null
  docker buildx inspect --bootstrap >/dev/null
}

# Build one image for linux/arm64 and load it into the local docker daemon
# under the `:${BUILD_TAG}` tag suffix so we don't trample the x86 latest.
build_one() {
  local spec="$1"
  local image="${spec%%|*}"
  local rest="${spec#*|}"
  local dockerfile="${rest%%|*}"
  local context="${rest#*|}"
  # Convert tag `:latest` → `:arm64` so we don't clobber the native amd64 image.
  local arm_tag="${image%:*}:${BUILD_TAG}"
  echo "==> building $arm_tag (from $dockerfile)"
  # `--output type=docker` materialises the result into the local daemon
  # so `docker save` works downstream. `--platform linux/arm64` runs the
  # whole Dockerfile under QEMU emulation.
  docker buildx build \
    --builder "$BUILDER_NAME" \
    --platform linux/arm64 \
    --file "$dockerfile" \
    --tag "$arm_tag" \
    --output type=docker \
    --provenance=false \
    "$context"
}

# Save one arm64 image to a xz-compressed tarball. xz at default settings
# wins ~3x over gzip on these debian-slim Python images.
save_one() {
  local image="$1"
  local arm_tag="${image%:*}:${BUILD_TAG}"
  local fname; fname="$(echo "$arm_tag" | tr '/:' '__').tar.xz"
  echo "==> saving $arm_tag → dist/$fname"
  mkdir -p dist
  docker save "$arm_tag" | xz -T0 -3 > "dist/$fname"
}

# Transfer all dist/*.tar.xz over SSH. rsync is resumable so a flaky link
# doesn't restart 10 GB from zero.
ship_all() {
  echo "==> rsync dist/*.tar.xz → ${PI_HOST}:${PI_REMOTE_DIR}/dist/"
  ssh "$PI_HOST" "mkdir -p ${PI_REMOTE_DIR}/dist"
  rsync -avhP --partial dist/*.tar.xz "$PI_HOST:${PI_REMOTE_DIR}/dist/"
}

# Load images on the Pi side. `xz -d | docker load` streams without
# materialising the uncompressed tarball — saves ~30 GB of disk I/O.
load_remote() {
  echo "==> docker load on Pi"
  for spec in "${OWNED_IMAGES[@]}"; do
    local image="${spec%%|*}"
    local arm_tag="${image%:*}:${BUILD_TAG}"
    local fname; fname="$(echo "$arm_tag" | tr '/:' '__').tar.xz"
    echo "==>   loading $arm_tag from $fname"
    ssh "$PI_HOST" "xz -d < '${PI_REMOTE_DIR}/dist/${fname}' | docker load"
    # Re-tag :arm64 → :latest on the Pi so compose.yaml's `image: <name>:latest`
    # references resolve. Idempotent.
    ssh "$PI_HOST" "docker tag '${arm_tag}' '${image}'"
  done
}

# Final step: bring the stack up on the Pi. We don't `--build` here — the
# images are pre-loaded; building on the Pi defeats the whole purpose.
compose_up_remote() {
  echo "==> docker compose up -d (no rebuild) on Pi"
  ssh "$PI_HOST" "cd ${PI_REMOTE_DIR} && sops exec-env secrets.yaml 'docker compose up -d'"
}

# --- main ---------------------------------------------------------------
if [[ "$SKIP_BUILD" != "1" ]]; then
  ensure_buildx
  for spec in "${OWNED_IMAGES[@]}"; do
    build_one "$spec"
  done
fi

for spec in "${OWNED_IMAGES[@]}"; do
  image="${spec%%|*}"
  save_one "$image"
done

if [[ "$SKIP_PUSH" == "1" ]]; then
  echo "==> SKIP_PUSH=1, stopping after save. Tarballs in dist/."
  ls -lh dist/*.tar.xz
  exit 0
fi

ship_all
load_remote
compose_up_remote

echo "==> done. Verify on Pi: ssh ${PI_HOST} 'cd ${PI_REMOTE_DIR} && docker compose ps'"

# Cross-compile + ship to Pi 5 (Option A — tarball over SSH)

Build the Marked_Path images for `linux/arm64` on the dev workstation (x86_64),
save them as compressed tarballs, transfer over SSH, and load them on the Pi.
The Pi never compiles anything — `docker compose up -d` against pre-loaded
images takes seconds instead of an hour.

## When to use this

- Pi 5 native build is painfully slow (uv sync alone is ~25 min).
- Code changes are large enough that `docker compose build` on the Pi would
  take the whole stack offline for >30 min.
- You don't want to spin up a registry just for one-off pushes (Option B/C).

## What gets cross-compiled

| Image | Dockerfile | Approx. compressed size | Cross-build time (x86 host, QEMU) |
| --- | --- | --- | --- |
| `marked_path-jobs-bot:latest` | `docker/jobs-bot.Dockerfile` | ~2 GB | ~8-12 min |
| `marked_path-tools:latest` | `docker/tools.Dockerfile` | ~1.5 GB | ~5-8 min |
| `marked_path-applier-worker:latest` | `docker/applier.Dockerfile` | ~3 GB | ~10-15 min (tex bundle warmup is slow under QEMU) |
| `marked_path-camoufox-worker:latest` | `docker/camoufox.Dockerfile` | ~3.5 GB | ~15-25 min (Firefox download + Xvfb setup) |

External images (`pgvector/pgvector:pg16`, `redis:7-alpine`, `postgrest:v12.0.2`,
`flaresolverr`) the Pi pulls directly from Docker Hub — they're already multi-arch
upstream. No cross-compile required.

## Pre-flight on the dev workstation

1. Docker daemon running, buildx ≥ 0.30.
2. `binfmt-misc` arm64 emulator installed (the ship script installs it
   idempotently via `tonistiigi/binfmt`).
3. Enough disk: each image saves ~5 GB uncompressed plus the xz output, so
   plan for ~50 GB free in `dist/`.
4. SSH access to the Pi (key auth, no password prompts mid-rsync).
5. Pi already has `sops` + `age` keys installed and the repo cloned at
   `${PI_REMOTE_DIR}` — the script does NOT bootstrap a fresh Pi.

## Pre-flight on the Pi

1. `docker` + `docker compose` v2 installed.
2. `xz` available on the path (it's in coreutils on DietPi).
3. `secrets.yaml` already SOPS-decryptable on the Pi (operator pre-staged the
   age private key in `~/.config/sops/age/keys.txt`).
4. Bind-mount paths exist:
   - `/mnt/storage/wal_archive` (postgres WAL archive)
   - `<repo>/var/telegram/` (Telethon session)
   - `<repo>/dashboard/` (frontend SPA — should already be in the git checkout)

## Run

```bash
# Dry-run (build + save, no SSH push). Confirms the cross-compile actually works
# without touching the Pi. Tarballs land in dist/.
SKIP_PUSH=1 SKIP_BROWSER=1 scripts/ship_to_pi.sh

# Full pipeline. Defaults target the live Pi 5 — dietpi@192.168.1.240,
# repo at /home/dietpi/coding/Cartograph. Override env vars below for
# any other target.
scripts/ship_to_pi.sh
```

### Override knobs

| Env var | Default | Effect |
| --- | --- | --- |
| `PI_HOST` | `dietpi@192.168.1.240` | SSH target. Override for a different host. |
| `PI_REMOTE_DIR` | `/home/dietpi/coding/Cartograph` | Pi-side repo root. |
| `BUILD_TAG` | `arm64` | Tag suffix on local images so the cross-built artefacts don't clobber the native amd64 `:latest`. |
| `SKIP_BROWSER` | `0` | Set to `1` to skip the heavy camoufox image. |
| `SKIP_BUILD` | `0` | Reuse images already in the local daemon under `:${BUILD_TAG}`. |
| `SKIP_PUSH` | `0` | Stop after saving tarballs — no SSH activity. |
| `PARALLEL_LOAD` | `0` | (Reserved for a future tweak that pipelines xz | docker load on the Pi.) |

## What the script actually does

1. Installs the `qemu-aarch64` binfmt handler via `tonistiigi/binfmt`.
2. Creates a `docker-container` buildx builder named `cartograph-cross`
   (idempotent — reuses on next run).
3. For each owned image, runs
   `docker buildx build --platform linux/arm64 --output type=docker --tag
   <image>:arm64`. This loads the cross-built image into the local daemon.
4. `docker save <image>:arm64 | xz -T0 -3 > dist/<image>__arm64.tar.xz` per
   image.
5. `rsync -avhP dist/*.tar.xz $PI_HOST:$PI_REMOTE_DIR/dist/` — resumable
   transfer.
6. For each image, SSHes to the Pi and runs
   `xz -d < dist/<file>.tar.xz | docker load`. Then re-tags `:arm64` → `:latest`
   so `compose.yaml`'s `image: <name>:latest` resolves.
7. `cd $PI_REMOTE_DIR && sops exec-env secrets.yaml 'docker compose up -d'`
   on the Pi. Note: NO `--build` here — pre-loaded images are the whole point.

## Recovery + verification

Verify on the Pi:

```bash
ssh $PI_HOST 'cd $PI_REMOTE_DIR && docker compose ps'
```

Every service should show `Up` (camoufox + crawler may restart-loop pending
the browser-tier fix; that's a separate runbook).

If a load fails partway:

```bash
ssh $PI_HOST 'cd $PI_REMOTE_DIR && ls -lh dist/'
# Resume rsync (idempotent):
SKIP_BUILD=1 scripts/ship_to_pi.sh
```

The `SKIP_BUILD=1` flag skips the cross-build (since the images are already
loaded locally as `:arm64`) and rsync's `--partial` handles the resume.

## When NOT to use this

- Single-file fix to the dashboard JS. Just `scp dashboard/views/foo.js`
  to the Pi — no image rebuild needed.
- secrets.yaml edit. The script never bakes secrets into images.
- Migration-only change. Run `sops exec-env secrets.yaml 'docker compose run
  --rm tools python -m src.cli.main migrate'` on the Pi directly — but
  remember the `tools` image must already contain the new migration files,
  so this script (or a `tools`-only rebuild) is the prerequisite.

## Trade-offs accepted

- QEMU-emulated builds run ~3-5× slower than native, so a full clean build
  is still ~45-60 min on the dev box. Subsequent incremental builds hit
  buildx's layer cache and finish in 5-10 min.
- xz compression at level 3 prioritises speed over ratio; ~10 GB compressed
  for the full image set is the steady-state size.
- We DON'T multi-arch push. The dev-box image set is built fresh as
  `:arm64` per session; the native `:latest` from prior local work stays
  intact for IDE / pytest workflows.

## Future: switch to Option B (GHCR multi-arch)

If we end up shipping more than once a week, the tarball workflow gets
tedious. Option B (`buildx --push` to GHCR with a multi-arch manifest)
pays for itself in three or four runs. The owner needs to:

1. Create a GHCR personal access token with `write:packages`.
2. Add it to SOPS as `ghcr_token`.
3. `docker login ghcr.io -u lakshit-gupta` with that token.
4. Switch the ship script to `buildx build --platform linux/amd64,linux/arm64
   --push -t ghcr.io/lakshit-gupta/cartograph-<image>:latest .` and replace
   the SSH/load steps with `docker compose pull && docker compose up -d`
   on the Pi.

We stay on Option A until the per-ship cost becomes painful.

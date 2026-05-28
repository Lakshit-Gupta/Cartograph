# Phase 4 auto-apply — sidecar `apply-browser-worker` image.
#
# Extends the camoufox image so we inherit Firefox + Xvfb + the camoufox
# python deps without rebuilding them. Different ENTRYPOINT/CMD: instead
# of joining g:crawlers on stream:fetch, this worker consumes
# stream:apply_browser and drives camoufox through Internshala's Easy
# Apply modal.
#
# Built natively on the spare Pop OS 24.04 desktop (x86_64) — NO QEMU
# cross-compile needed, the spare runs the same arch as the build host.
# The Pi never runs this image; the spare-only manifest is
# `compose.sidecar.yaml`.

# Use the camoufox image as the base — must already exist locally as
# `marked_path-camoufox-worker:latest` from compose.sidecar.yaml's
# previous service (or built first by the bootstrap script).
ARG BASE_IMAGE=marked_path-camoufox-worker:latest
FROM ${BASE_IMAGE} AS base

USER root

# tmpfs mount target — base64 PDF lands here for set_input_files. /tmp
# itself is tmpfs (compose.sidecar.yaml), so this is just a marker dir.
# We chown to the camoufox uid so the unprivileged process can write.
RUN mkdir -p /tmp/apply \
    && chown -R camoufox:camoufox /tmp/apply

USER camoufox

# Same entrypoint (Xvfb wrapper) but different CMD — invoke the
# auto-apply worker module instead of the crawler.
CMD ["python", "-m", "src.workers.apply_browser_worker"]

# Phase 4 Internshala browser-discovery worker image.
#
# Extends the apply-browser image (which itself extends the camoufox image) so
# Firefox + Xvfb + ghost-cursor + the camoufox python deps are already present —
# this worker drives the same browser stack, just against Internshala's listing
# dropdowns instead of the Easy-Apply modal. Adds nothing but the new
# ENTRYPOINT.
#
# Built natively on the spare Pop OS 24.04 desktop (x86_64) — NO QEMU
# cross-compile needed; the spare runs the same arch as the build host. The Pi
# never runs this image. The spare-only manifest is `compose.sidecar.yaml`.

ARG BASE_IMAGE=cartograph-apply-browser:latest
FROM ${BASE_IMAGE} AS base

USER root

# Selector-miss artefacts land here (screenshot + clipped DOM). /tmp is tmpfs in
# compose.sidecar.yaml, so this just pre-creates the dir owned by the camoufox
# uid so the unprivileged process can write.
RUN mkdir -p /tmp/discovery/miss \
    && chown -R camoufox:camoufox /tmp/discovery

# Refresh the source tree on top of the base image so a discovery-only code
# change does not require rebuilding the camoufox/apply layers.
COPY src/ /app/src/
COPY config/ /app/config/

ENV PYTHONPATH=/app

USER camoufox

ENTRYPOINT ["python", "-m", "src.workers.internshala_discovery_worker"]

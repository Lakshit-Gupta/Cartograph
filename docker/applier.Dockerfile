# docker/applier.Dockerfile
#
# Dedicated image for the applier-worker. Extends the lean jobs-bot image
# with tectonic (LaTeX engine), qpdf (PDF linearisation), and exiftool
# (metadata scrub). Only this worker compiles tailored resumes — keeping
# the toolchain out of jobs-bot saves ~250 MB across the other 8 services.
#
# Hard rules (CLAUDE.md "LaTeX resume subsystem"):
# - tectonic always invoked with --untrusted by src/application/resume_latex/compile.py
# - subprocess timeout 30s, kill_group=True
# - PDF metadata scrubbed via exiftool post-compile
# - artifacts written to /var/lib/agent/resume_artifacts (durable, never tmpfs)
#
# Why tectonic via upstream tarball (not apt): Debian Bookworm ships no
# `tectonic` package. The static musl tarballs from the upstream
# tectonic-typesetting/tectonic GitHub release are self-contained, ~10 MB,
# and exist for both aarch64 (Pi 5) and x86_64 (dev laptop). Pinned version
# keeps reproducible builds; bump deliberately, not silently.

# Base image is tagged `marked_path-jobs-bot:latest` by compose
# (see x-jobs-bot-image anchor in compose.yaml). Build the base first via
# `docker compose build jobs-scheduler` before building this image.
FROM marked_path-jobs-bot:latest AS base

ARG TARGETARCH
ARG TECTONIC_VERSION=0.16.9

USER root
# qpdf for PDF linearisation, exiftool for metadata scrub. Both packaged.
RUN apt-get update && apt-get install -y --no-install-recommends \
        qpdf \
        libimage-exiftool-perl \
        curl \
        ca-certificates \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

# tectonic — static musl binary from upstream release. Picks the right
# arch (amd64 on dev, arm64 on Pi). Pin matches TECTONIC_VERSION above.
RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
        amd64)  tarch=x86_64 ;; \
        arm64)  tarch=aarch64 ;; \
        *) echo "unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    url="https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40${TECTONIC_VERSION}/tectonic-${TECTONIC_VERSION}-${tarch}-unknown-linux-musl.tar.gz"; \
    curl -fL -o /tmp/tectonic.tgz "$url"; \
    tar -xzf /tmp/tectonic.tgz -C /usr/local/bin tectonic; \
    rm /tmp/tectonic.tgz; \
    tectonic --version

# Pre-warm the tectonic font + package bundle. Without this, the first
# compile fetches the bundle from CTAN over the network (~30s cold).
# Pre-warming bakes the resolved cache into the image and brings cold
# compiles down to ~2s. We use printf so the backslashes survive /bin/sh
# (echo strips them); --outdir /tmp/warm keeps all artefacts in a scratch
# dir we can rm at the end, leaving only the populated cache.
RUN mkdir -p /var/lib/tectonic /tmp/warm \
    && printf '%s\n%s\n%s\n' '\documentclass{article}' '\begin{document}warm' '\end{document}' > /tmp/warm/warmup.tex \
    && XDG_CACHE_HOME=/var/lib/tectonic tectonic -X compile \
        --untrusted --outdir /tmp/warm /tmp/warm/warmup.tex \
    && rm -rf /tmp/warm \
    && chown -R 1000:1000 /var/lib/tectonic

ENV XDG_CACHE_HOME=/var/lib/tectonic

USER 1000

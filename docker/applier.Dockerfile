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

# Base image is tagged `marked_path-jobs-bot:latest` by compose
# (see x-jobs-bot-image anchor in compose.yaml). Build the base first via
# `docker compose build jobs-scheduler` before building this image.
FROM marked_path-jobs-bot:latest AS base

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
        tectonic \
        qpdf \
        libimage-exiftool-perl \
    && rm -rf /var/lib/apt/lists/*

# Pre-warm the tectonic font + package bundle. Without this, the first
# compile fetches the bundle from CTAN over the network (~30s cold).
# Pre-warming bakes the resolved cache into the image and brings cold
# compiles down to ~2s. `--keep-intermediates=false` keeps the layer small.
RUN echo '\documentclass{article}\begin{document}warm\end{document}' > /opt/warmup.tex \
    && mkdir -p /var/lib/tectonic \
    && XDG_CACHE_HOME=/var/lib/tectonic tectonic -X compile \
        --untrusted --keep-intermediates=false /opt/warmup.tex \
    && rm /opt/warmup.tex /opt/warmup.pdf \
    && chown -R 1000:1000 /var/lib/tectonic

ENV XDG_CACHE_HOME=/var/lib/tectonic

USER 1000

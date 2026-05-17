# Marked_Path main Python image. ARM64.
FROM python:3.11-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# System deps: libsodium for pynacl, libpq for asyncpg connect, build for sentence-transformers
RUN apt-get update && apt-get install -y --no-install-recommends \
      libsodium23 \
      libpq5 \
      libssl3 \
      ca-certificates \
      tzdata \
      curl \
      git \
    && rm -rf /var/lib/apt/lists/*

# uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dep layer
COPY pyproject.toml ./
RUN uv venv /opt/venv && uv sync --no-dev --frozen || uv sync --no-dev

# App
COPY src /app/src
COPY migrations /app/migrations
COPY config /app/config
COPY scripts /app/scripts

RUN mkdir -p /app/logs /app/.cache/models \
    && useradd -m -u 1000 agent \
    && chown -R agent:agent /app /opt/venv

USER agent

# Default no-op; each container overrides via compose `command`
CMD ["python", "-c", "print('Marked_Path image ready — provide a command')"]

# One-off ops image: migrations, seed, restore drills.
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
      postgresql-client \
      redis-tools \
      curl \
      ca-certificates \
      libsodium23 \
      libpq5 \
      age \
      rclone \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml README.md ./
RUN uv venv /opt/venv && uv sync --no-dev --frozen || uv sync --no-dev

COPY src /app/src
COPY migrations /app/migrations
COPY config /app/config
COPY scripts /app/scripts

CMD ["python", "-m", "src.cli.main", "--help"]

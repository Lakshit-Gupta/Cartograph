# Browser tier: Firefox-based camoufox + Xvfb. ARM64.
# ~400MB heavier than base image — kept as separate image.
FROM python:3.11-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    DISPLAY=:99 \
    MOZ_HEADLESS=

# Firefox + Xvfb + fonts (CF fingerprint defense)
RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb \
      libgtk-3-0 \
      libdbus-glib-1-2 \
      libasound2 \
      libxtst6 \
      libxrandr2 \
      libxcomposite1 \
      libxdamage1 \
      libxfixes3 \
      libxi6 \
      libnss3 \
      libxss1 \
      libsodium23 \
      libpq5 \
      ca-certificates \
      tzdata \
      curl \
      wget \
      fonts-liberation \
      fonts-noto \
      fonts-noto-cjk \
      fonts-dejavu \
      fonts-roboto \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml README.md ./
RUN uv venv /opt/venv && uv sync --no-dev --extra dev --frozen || uv sync --no-dev

# Fetch camoufox firefox binary into image
RUN python -m camoufox fetch || true

COPY src /app/src
COPY config /app/config

RUN useradd -m -u 1000 camoufox \
    && mkdir -p /app/logs /home/camoufox/.cache \
    && chown -R camoufox:camoufox /app /opt/venv /home/camoufox

USER camoufox

# Xvfb wrapper
COPY --chown=camoufox:camoufox docker/camoufox-entrypoint.sh /usr/local/bin/camoufox-entrypoint.sh
RUN chmod +x /usr/local/bin/camoufox-entrypoint.sh || true

ENTRYPOINT ["/usr/local/bin/camoufox-entrypoint.sh"]
CMD ["python", "-m", "src.workers.crawler", "--browser"]

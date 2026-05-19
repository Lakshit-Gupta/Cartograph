"""SOPS-aware env loader.

In production: compose runs as `sops exec-env secrets.yaml 'docker compose up'`,
which exports every top-level YAML key into the process env. This module just
reads `os.environ` and provides typed accessors.

For local dev without SOPS, fall back to `.env`.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote as _urlquote

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Postgres
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_user: str = "marked"
    postgres_password: str = "changeme"
    postgres_db: str = "marked"

    # Redis
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: str = "changeme"

    # OpenRouter / LLM
    openrouter_api_key: str = ""
    openrouter_model_extractor: str = "google/gemini-flash-1.5"
    openrouter_model_reranker: str = "google/gemini-flash-1.5"
    openrouter_model_classifier: str = "google/gemini-flash-1.5"
    openrouter_model_writer: str = "anthropic/claude-3.5-sonnet"

    # Resend
    resend_api_key: str = ""
    resend_from_email: str = ""

    # Discord
    discord_bot_token: str = ""
    discord_guild_id: int = 0

    # Discord channel IDs — kept FLAT because SOPS exec-env only exports
    # top-level keys. See secrets.yaml.example for matching schema.
    discord_channel_daily_digest: int = 0
    discord_channel_priority_push: int = 0
    discord_channel_fulltime: int = 0
    discord_channel_internships: int = 0
    discord_channel_fellowships: int = 0
    discord_channel_freelance: int = 0
    discord_channel_applied: int = 0
    discord_channel_responses: int = 0
    discord_channel_interviews: int = 0
    discord_channel_offers: int = 0
    discord_channel_alerts: int = 0
    discord_channel_costs: int = 0
    discord_channel_source_health: int = 0
    discord_channel_bot_logs: int = 0

    # Telegram
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    # Session stem (no extension; Telethon appends `.session`). Used by the
    # scripts/telegram_auth.py one-shot AND the freelance-telegram-fetcher
    # worker to locate the SQLite session DB.
    telegram_session_name: str = "Cartograph_freelance"
    # Full container-side path to the .session file. The freelance worker
    # mounts ./var/telegram into /var/lib/agent/telegram (read-only). Leave
    # empty for local dev — the worker logs and idles when missing.
    telegram_session_path: str = ""

    # Reddit
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "cartograph/0.1"
    # Optional — when both set, OAuth uses grant_type=password (full scope).
    # When unset, falls back to grant_type=client_credentials (public reads only).
    reddit_username: str = ""
    reddit_password: str = ""

    # Gmail
    gmail_oauth_client_id: str = ""
    gmail_oauth_client_secret: str = ""
    gmail_oauth_refresh_token: str = ""
    gmail_user: str = ""
    gmail_worker_user: str = ""
    gmail_worker_app_password: str = ""

    # R2 backup
    r2_account_id: str = ""
    r2_access_key: str = ""
    r2_secret_key: str = ""
    r2_bucket: str = "agent-jobs-backups"

    # Crypto
    libsodium_master_key_hex: str = ""

    # FlareSolverr
    flaresolverr_url: str = "http://flaresolverr:8191"

    # Cost caps (USD)
    cost_cap_daily_usd: float = 3.0
    cost_cap_daily_kill_usd: float = 10.0
    cost_cap_monthly_soft_usd: float = 30.0
    cost_cap_monthly_hard_usd: float = 100.0

    # Observability
    metrics_bind: str = "0.0.0.0:9090"

    # LaTeX resume subsystem (CLAUDE.md "LaTeX resume subsystem"). When
    # false, send_application uses the legacy JSON-template tailor path.
    # When true, the new LaTeX parser → tailor → sanitize → render →
    # tectonic --untrusted compile flow runs. Staged rollout: ship with
    # flag off, backfill embeddings, drain Streams.APPLY, then flip on.
    mp_resume_latex_enabled: bool = False

    # Phase 2.1 — cold-outreach outbound lane. Daily cap is the HARD ceiling
    # on cold emails sent in a UTC day; warmup_start is the day-0 floor that
    # ramps linearly to daily_cap over warmup_days. When `enabled` is False
    # the worker boots and idles silently — APScheduler trigger fires but
    # the run_daily_cycle handler short-circuits immediately. Same staged-
    # rollout pattern as MP_RESUME_LATEX_ENABLED: ship with flag off,
    # populate target_companies + verify Apollo/Hunter key health, then
    # flip via SOPS edit and restart cold-outreach-worker.
    cold_outreach_enabled: bool = False
    cold_outreach_daily_cap: int = 10
    cold_outreach_warmup_start: int = 5
    cold_outreach_warmup_days: int = 5
    apollo_api_key: str = ""
    hunter_api_key: str = ""

    # Phase 2.3 — follow-up automation. When False the daily 13:00 IST cron
    # short-circuits and never drafts anything; the Send button hard-refuses
    # too. Flip via SOPS edit + restart of jobs-scheduler / applier-worker /
    # notifier-discord. Same staged-rollout pattern as MP_RESUME_LATEX_ENABLED.
    mp_followup_enabled: bool = False
    # Days of silence after sent_at before an application becomes eligible.
    followup_window_days: int = 4
    # Hard cap on follow-ups drafted per cron tick. Oldest-application-first
    # when the eligible set exceeds the cap; overflow is logged via
    # "followup_overflow" so the operator sees it in #🤖-bot-logs.
    followup_daily_cap: int = 30

    # Misc
    obsidian_vault_path: str = "/vault"
    config_root: str = Field(default_factory=lambda: str(Path(__file__).resolve().parents[2] / "config"))

    @property
    def postgres_dsn(self) -> str:
        # URL-encode user/password — strong passwords from `openssl rand -base64 32`
        # contain `+`, `/`, `=`, and sometimes `:` which break asyncpg's DSN parser.
        user = _urlquote(self.postgres_user, safe="")
        pw = _urlquote(self.postgres_password, safe="")
        return f"postgresql://{user}:{pw}@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"

    @property
    def redis_url(self) -> str:
        pw = _urlquote(self.redis_password, safe="")
        return f"redis://:{pw}@{self.redis_host}:{self.redis_port}/0"

    @property
    def libsodium_master_key(self) -> bytes:
        if not self.libsodium_master_key_hex:
            raise RuntimeError("LIBSODIUM_MASTER_KEY_HEX not configured")
        return bytes.fromhex(self.libsodium_master_key_hex)

    # ---- Discord channel helpers ------------------------------------------
    _CHANNEL_NAMES: tuple[str, ...] = (
        "daily_digest",
        "priority_push",
        "fulltime",
        "internships",
        "fellowships",
        "freelance",
        "applied",
        "responses",
        "interviews",
        "offers",
        "alerts",
        "costs",
        "source_health",
        "bot_logs",
    )

    def discord_channel(self, name: str) -> int:
        """Return channel ID for a logical name (e.g. 'daily_digest').

        Raises KeyError on unknown name, returns 0 if unset (caller decides).
        """
        if name not in self._CHANNEL_NAMES:
            raise KeyError(f"unknown discord channel name: {name}")
        return int(getattr(self, f"discord_channel_{name}"))

    def assert_channels_configured(self, required: tuple[str, ...] | None = None) -> None:
        """Fail loud if any required channel id is 0.

        Called on bot startup so a SOPS / .env misconfiguration aborts the
        process instead of silently posting to channel 0.
        """
        required = required or self._CHANNEL_NAMES
        missing = [n for n in required if self.discord_channel(n) == 0]
        if missing:
            raise RuntimeError("discord channel IDs not configured: " + ", ".join(f"discord_channel_{n}" for n in missing))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def get_env(name: str, default: Any = None) -> Any:
    return os.environ.get(name, default)

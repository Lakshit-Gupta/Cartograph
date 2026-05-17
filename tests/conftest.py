"""Pytest fixtures — disabled-by-default so CI never hits live infra."""
from __future__ import annotations

import os

# Ensure tests use safe placeholder env vars
os.environ.setdefault("LIBSODIUM_MASTER_KEY_HEX", "00" * 32)
os.environ.setdefault("OPENROUTER_API_KEY", "test")
os.environ.setdefault("DISCORD_BOT_TOKEN", "test")

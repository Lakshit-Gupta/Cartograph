"""Pytest fixtures — disabled-by-default so CI never hits live infra."""

from __future__ import annotations

import os
import tempfile

# Ensure tests use safe placeholder env vars
os.environ.setdefault("LIBSODIUM_MASTER_KEY_HEX", "00" * 32)
os.environ.setdefault("OPENROUTER_API_KEY", "test")
os.environ.setdefault("DISCORD_BOT_TOKEN", "test")
# Worker modules call `configure_logging(...)` at import time, which tries
# to create `/app/logs` (the container bind-mount path) and fails outside
# Docker. Pin LOG_DIR to a process-scoped temp dir so worker imports are
# hermetic. Lives for the test run; OS cleans up on exit.
os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="cartograph-test-logs-"))

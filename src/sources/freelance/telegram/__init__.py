"""Freelance Telegram channel fetcher package.

Internal subpackage split out of `telegram_fetcher.py` (was 553 LOC). The
public, test-facing module remains `src.sources.freelance.telegram_fetcher`,
which re-exports / hosts the symbols tests touch (so `monkeypatch.setattr`
on `telegram_fetcher` keeps working).

Modules:
    parser   — ParsedMessage, parse_message, build_opportunity, regex helpers
    channels — load_channels_from_prefs, resolve_source_id, _normalise_channel
    handler  — _handle_event + _attach_handler (Telethon event-callback wiring)
    loop     — _run_listener_loop + backoff curve + FloodWaitError handling
"""

from __future__ import annotations

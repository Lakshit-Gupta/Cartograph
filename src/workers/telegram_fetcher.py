"""freelance-telegram-fetcher container entrypoint.

Mirrors `src/workers/gmail_worker.py` shape. Sets up structured logging,
delegates to `src.sources.freelance.telegram_fetcher.run()`, and swallows
KeyboardInterrupt for clean shutdown on SIGTERM / `docker compose down`.
"""

from __future__ import annotations

import asyncio

from src.common.logger import configure_logging, get_logger
from src.sources.freelance.telegram_fetcher import run

configure_logging("telegram_fetcher")
_log = get_logger(__name__)


async def main() -> None:
    try:
        await run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        _log.info("telegram_fetcher_interrupted")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

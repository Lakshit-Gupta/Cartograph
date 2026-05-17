"""notifier-discord container entrypoint."""
from __future__ import annotations

import asyncio
import contextlib
import signal

from src.common.db import close_pool, init_pool
from src.common.logger import configure_logging, get_logger

configure_logging("notifier")
_log = get_logger(__name__)


async def main() -> None:
    await init_pool()
    # Lazy import so missing discord.py at scaffold time doesn't break other workers
    from src.notifiers.discord.bot import Bot

    bot = Bot()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    runner_task = asyncio.create_task(bot.start_default())
    try:
        await stop.wait()
    finally:
        runner_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner_task
        await bot.close()
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())

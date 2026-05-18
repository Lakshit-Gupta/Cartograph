"""Human-ish mouse + scroll behavior for browser sessions.

ghost-cursor-python provides bezier-path cursor moves; for now we use a
simple jitter + scroll to mimic page exploration.
"""

from __future__ import annotations

import asyncio
import random

# Best-effort import — package may be unavailable on the local dev box
try:
    from playwright.async_api import Page as PWPage  # type: ignore[import]
except Exception:
    PWPage = object  # type: ignore[assignment,misc]


async def humanize_page(page: PWPage) -> None:  # type: ignore[valid-type]
    """Issue small interactions to drift signals away from headless defaults."""
    try:
        await asyncio.sleep(random.uniform(0.4, 1.2))
        # Move
        for _ in range(random.randint(1, 3)):
            x = random.randint(50, 1200)
            y = random.randint(50, 600)
            await page.mouse.move(x, y, steps=random.randint(8, 18))
            await asyncio.sleep(random.uniform(0.1, 0.4))
        # Scroll
        for _ in range(random.randint(1, 4)):
            dy = random.randint(150, 600)
            await page.mouse.wheel(0, dy)
            await asyncio.sleep(random.uniform(0.3, 0.9))
        await asyncio.sleep(random.uniform(0.4, 1.0))
    except Exception:
        # Behavioral nudge is best-effort; never abort the fetch on jitter failure.
        pass

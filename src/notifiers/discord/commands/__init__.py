"""Slash command modules. Each exposes `setup(bot)` which the Bot subclass calls
during `on_ready` / `setup_hook` to register `app_commands.Command` objects.

Importing the package imports every submodule so command files self-register
their `setup` function via the `ALL_SETUPS` list below.
"""

from __future__ import annotations

from collections.abc import Callable

from src.notifiers.discord.commands import (
    apply as _apply,
)
from src.notifiers.discord.commands import (
    auto_apply as _auto_apply,
)
from src.notifiers.discord.commands import (
    budget as _budget,
)
from src.notifiers.discord.commands import (
    cost as _cost,
)
from src.notifiers.discord.commands import (
    digest as _digest,
)
from src.notifiers.discord.commands import (
    explain as _explain,
)
from src.notifiers.discord.commands import (
    export as _export,
)
from src.notifiers.discord.commands import (
    followup as _followup,
)
from src.notifiers.discord.commands import (
    identity as _identity,
)
from src.notifiers.discord.commands import (
    jobs_onboard as _jobs_onboard,
)
from src.notifiers.discord.commands import (
    pin as _pin,
)
from src.notifiers.discord.commands import (
    review as _review,
)
from src.notifiers.discord.commands import (
    scrape_status as _scrape_status,
)
from src.notifiers.discord.commands import (
    skip as _skip,
)
from src.notifiers.discord.commands import (
    snooze as _snooze,
)
from src.notifiers.discord.commands import (
    source as _source,
)
from src.notifiers.discord.commands import (
    status as _status,
)

ALL_SETUPS: list[Callable[[object], None]] = [
    _budget.setup,
    _digest.setup,
    _apply.setup,
    _auto_apply.setup,
    _scrape_status.setup,
    _skip.setup,
    _snooze.setup,
    _pin.setup,
    _status.setup,
    _source.setup,
    _identity.setup,
    _jobs_onboard.setup,
    _cost.setup,
    _followup.setup,
    _explain.setup,
    _export.setup,
    _review.setup,
]


def register_all(bot: object) -> None:
    for setup in ALL_SETUPS:
        setup(bot)

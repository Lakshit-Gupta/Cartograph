"""Tests for the lane-split auto-apply slash commands.

`/auto-apply-inter` (internship) and `/auto-apply-job` (fulltime) replace the
single `/auto-apply` group so the two verticals never share a preview/run pool.
Each must register its own group with `run` + `preview` subcommands, and each
subcommand must scope the engine query to its category.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.notifiers.discord.commands.auto_apply as aa_mod


class _Tree:
    def __init__(self) -> None:
        self.cmds: list[Any] = []

    def add_command(self, cmd: Any) -> None:
        self.cmds.append(cmd)


class _Bot:
    def __init__(self) -> None:
        self.tree = _Tree()


def _fake_interaction() -> MagicMock:
    i = MagicMock()
    i.response.defer = AsyncMock()
    i.followup.send = AsyncMock()
    return i


def test_setup_registers_two_lane_groups() -> None:
    bot = _Bot()
    aa_mod.setup(bot)
    names = {g.name for g in bot.tree.cmds}
    assert names == {"auto-apply-inter", "auto-apply-job"}
    for g in bot.tree.cmds:
        sub = {c.name for c in g.commands}
        assert sub == {"run", "preview"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("group_name", "expected_category"), [("auto-apply-inter", "internship"), ("auto-apply-job", "fulltime")])
async def test_preview_scopes_category(monkeypatch: pytest.MonkeyPatch, group_name: str, expected_category: str) -> None:
    seen: dict[str, Any] = {}

    async def _fake_find_eligible(*, user_id: int, limit: int, category: str | None = None):
        seen["category"] = category
        return []

    monkeypatch.setattr(aa_mod, "find_eligible", _fake_find_eligible)
    monkeypatch.setattr(aa_mod, "current_tenant", lambda: 1)

    bot = _Bot()
    aa_mod.setup(bot)
    group = next(g for g in bot.tree.cmds if g.name == group_name)
    preview = next(c for c in group.commands if c.name == "preview")
    await preview.callback(_fake_interaction(), 5)
    assert seen["category"] == expected_category


@pytest.mark.asyncio
@pytest.mark.parametrize(("group_name", "expected_category"), [("auto-apply-inter", "internship"), ("auto-apply-job", "fulltime")])
async def test_run_scopes_category(monkeypatch: pytest.MonkeyPatch, group_name: str, expected_category: str) -> None:
    seen: dict[str, Any] = {}

    # A summary-shaped stub so the command's followup formatting doesn't blow up.
    class _Summary:
        fired_count = 0
        candidates_found = 0
        daily_count_before = 0
        daily_cap = 3
        dry_run = True
        skipped_reasons: dict[str, int] = {}

    async def _dispatch(*, user_id: int, requested_count: int | None = None, source: str = "x", category: str | None = None):
        seen["category"] = category
        return _Summary()

    monkeypatch.setattr(aa_mod, "dispatch", _dispatch)
    monkeypatch.setattr(aa_mod, "current_tenant", lambda: 1)

    bot = _Bot()
    aa_mod.setup(bot)
    group = next(g for g in bot.tree.cmds if g.name == group_name)
    run = next(c for c in group.commands if c.name == "run")
    await run.callback(_fake_interaction(), 2)
    assert seen["category"] == expected_category

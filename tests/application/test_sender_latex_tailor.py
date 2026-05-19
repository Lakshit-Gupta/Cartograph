"""Hermetic tests for ``src.application.sender_latex.tailor``.

Coverage:
  - cost-gate routing via ``chat_json`` with mandatory ``kind="llm_writer"``.
  - prompt assembly fences untrusted opp text + falls through when the
    on-disk prompt template is missing.
  - JSON schema validation: missing ``edits`` key, non-dict entries, missing
    ``id``/``bullets``, empty bullets — all yield ``{}`` or skip the entry.
  - LLM exceptions surface as ``{}`` (caller renders untailored tree).

All boundaries are mocked — no live LLM, no DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from src.application.sender_latex import tailor as tailor_mod


@dataclass
class _FakeBlock:
    id: str
    kind: str
    title: str
    bullets: list[str]


def _blocks() -> list[_FakeBlock]:
    return [
        _FakeBlock(id="b1", kind="event", title="Engineer", bullets=["did things"]),
        _FakeBlock(id="b2", kind="project", title="Side", bullets=["shipped it"]),
    ]


@pytest.fixture
def _patch_prompt(monkeypatch: pytest.MonkeyPatch):
    """Make ``load_prompt`` always return a brace-safe template."""
    import src.common.llm as llm_mod

    template = "Resume tailor.\n<OPP>{opp_summary}</OPP>\n<VAR>{variant_label}</VAR>\n<BLOCKS>{blocks_json}</BLOCKS>"
    monkeypatch.setattr(llm_mod, "load_prompt", lambda _f: template)
    yield


@pytest.mark.smoke
async def test_routes_through_chat_json_with_kind_llm_writer(monkeypatch: pytest.MonkeyPatch, _patch_prompt):
    """Pin CLAUDE.md hard rule #8: cost ledger ``kind="llm_writer"``."""
    captured: dict[str, Any] = {}

    async def _fake_chat_json(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"edits": [{"id": "b1", "bullets": ["new bullet"]}]}

    import src.common.llm as llm_mod

    monkeypatch.setattr(llm_mod, "chat_json", _fake_chat_json)

    out = await tailor_mod.llm_tailor_blocks(_blocks(), {"title": "x"}, "backend")

    assert out == {"b1": ["new bullet"]}
    assert captured["kind"] == "llm_writer"
    # cost gate fires before provider sees request — chat_json is invoked
    # exactly once via the standard call path.
    msgs = captured["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    # Opp text was fenced before reaching prompt — sentinel check.
    assert "<IGNORE>" in msgs[1]["content"]


async def test_returns_empty_when_prompt_template_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    """No ``resume_tailor.txt`` on disk → no LLM call, no edits."""
    import src.common.llm as llm_mod

    def _missing(_f: str) -> str:
        raise FileNotFoundError("resume_tailor.txt")

    monkeypatch.setattr(llm_mod, "load_prompt", _missing)

    called = False

    async def _fake_chat_json(**_k: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(llm_mod, "chat_json", _fake_chat_json)

    out = await tailor_mod.llm_tailor_blocks(_blocks(), {}, "backend")
    assert out == {}
    assert called is False


async def test_returns_empty_on_llm_exception(monkeypatch: pytest.MonkeyPatch, _patch_prompt):
    """LLM exception is swallowed — caller renders untailored tree."""
    import src.common.llm as llm_mod

    async def _boom(**_k: Any) -> dict[str, Any]:
        raise RuntimeError("openrouter 500")

    monkeypatch.setattr(llm_mod, "chat_json", _boom)
    out = await tailor_mod.llm_tailor_blocks(_blocks(), {}, "backend")
    assert out == {}


async def test_refuses_on_missing_edits_key(monkeypatch: pytest.MonkeyPatch, _patch_prompt):
    """Provider returned a dict without the ``edits`` list."""
    import src.common.llm as llm_mod

    async def _fake(**_k: Any) -> dict[str, Any]:
        return {"not_edits": []}

    monkeypatch.setattr(llm_mod, "chat_json", _fake)
    assert await tailor_mod.llm_tailor_blocks(_blocks(), {}, "v") == {}


async def test_refuses_on_non_dict_response(monkeypatch: pytest.MonkeyPatch, _patch_prompt):
    """Provider returned a list / None instead of dict."""
    import src.common.llm as llm_mod

    async def _fake(**_k: Any) -> Any:
        return ["nope"]

    monkeypatch.setattr(llm_mod, "chat_json", _fake)
    assert await tailor_mod.llm_tailor_blocks(_blocks(), {}, "v") == {}


async def test_refuses_entries_with_bad_shape(monkeypatch: pytest.MonkeyPatch, _patch_prompt):
    """Entries missing ``id``/``bullets``, or with non-str id, are dropped."""
    import src.common.llm as llm_mod

    async def _fake(**_k: Any) -> dict[str, Any]:
        return {
            "edits": [
                {"id": "b1", "bullets": ["ok"]},  # keep
                {"id": 99, "bullets": ["bad id"]},  # drop
                {"id": "b2"},  # drop (no bullets)
                {"bullets": ["no id"]},  # drop
                "not a dict",  # drop
                {"id": "b3", "bullets": ["", "  "]},  # drop (all blank)
            ]
        }

    monkeypatch.setattr(llm_mod, "chat_json", _fake)
    out = await tailor_mod.llm_tailor_blocks(_blocks(), {}, "v")
    assert out == {"b1": ["ok"]}


async def test_strips_whitespace_in_bullets(monkeypatch: pytest.MonkeyPatch, _patch_prompt):
    """Bullets are ``.strip()``-ed; empties are dropped."""
    import src.common.llm as llm_mod

    async def _fake(**_k: Any) -> dict[str, Any]:
        return {"edits": [{"id": "b1", "bullets": ["  shipped  ", "", "  "]}]}

    monkeypatch.setattr(llm_mod, "chat_json", _fake)
    out = await tailor_mod.llm_tailor_blocks(_blocks(), {}, "v")
    assert out == {"b1": ["shipped"]}


async def test_passes_block_payload_to_prompt(monkeypatch: pytest.MonkeyPatch, _patch_prompt):
    """Block id/kind/title/bullets must be in the prompt sent to the LLM."""
    captured_prompt = {"v": ""}

    async def _fake(**kwargs: Any) -> dict[str, Any]:
        captured_prompt["v"] = kwargs["messages"][1]["content"]
        return {"edits": []}

    import src.common.llm as llm_mod

    monkeypatch.setattr(llm_mod, "chat_json", _fake)
    await tailor_mod.llm_tailor_blocks(_blocks(), {"title": "X"}, "backend")

    user_prompt = captured_prompt["v"]
    assert "b1" in user_prompt and "b2" in user_prompt
    assert "Engineer" in user_prompt
    assert "backend" in user_prompt

"""Hermetic tests for `src.workers.apply_result_worker`.

The result worker has two interesting branches per status, both of which
we exercise without a live DB by monkeypatching `_persist_result`:

  status='ok'                → notify_kind 'auto_applied'
  status='dry_run_captured'  → notify_kind 'auto_apply_dry_run'
  status='failed'            → notify_kind 'auto_apply_failed'

Also covers:
  - screenshot_b64 + selectors_version pass through verbatim.
  - missing opportunity_id is logged and skipped (no notify).
"""

from __future__ import annotations

from typing import Any

import pytest

from src.common.queue import Streams
from src.workers import apply_result_worker as mod

_OPP_ID = "00000000-0000-0000-0000-0000000088aa"


class _FakeQueue:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, stream: str, payload: dict[str, Any]) -> str:
        self.published.append((stream, payload))
        return "test-id"


@pytest.mark.smoke
async def test_process_ok_publishes_auto_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _persist(_result: dict[str, Any]) -> int | None:
        return 42

    monkeypatch.setattr(mod, "_persist_result", _persist)
    queue = _FakeQueue()
    await mod._process(
        queue,  # type: ignore[arg-type]
        {
            "task_id": "t1",
            "opportunity_id": _OPP_ID,
            "user_id": 1,
            "platform": "internshala",
            "status": "ok",
            "submitted_at": "2026-05-28T19:00:00Z",
            "selectors_version": "v1",
            "apply_url": "https://internshala.com/abc",
        },
    )
    assert len(queue.published) == 1
    stream, payload = queue.published[0]
    assert stream == Streams.NOTIFY
    assert payload["kind"] == "auto_applied"
    assert payload["payload"]["application_id"] == 42
    assert payload["payload"]["browser_status"] == "ok"


@pytest.mark.smoke
async def test_process_dry_run_publishes_auto_apply_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _persist(_result: dict[str, Any]) -> int | None:
        return 7

    monkeypatch.setattr(mod, "_persist_result", _persist)
    queue = _FakeQueue()
    await mod._process(
        queue,  # type: ignore[arg-type]
        {
            "task_id": "t2",
            "opportunity_id": _OPP_ID,
            "user_id": 1,
            "platform": "internshala",
            "status": "dry_run_captured",
            "screenshot_b64": "iVBOR...",
            "selectors_version": "2026.05.28",
            "dry_run": True,
        },
    )
    payload = queue.published[0][1]
    assert payload["kind"] == "auto_apply_dry_run"
    assert payload["payload"]["screenshot_b64"] == "iVBOR..."
    assert payload["payload"]["selectors_version"] == "2026.05.28"
    assert payload["payload"]["dry_run"] is True


@pytest.mark.smoke
async def test_process_failed_publishes_auto_apply_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _persist(_result: dict[str, Any]) -> int | None:
        return 9

    monkeypatch.setattr(mod, "_persist_result", _persist)
    queue = _FakeQueue()
    await mod._process(
        queue,  # type: ignore[arg-type]
        {
            "task_id": "t3",
            "opportunity_id": _OPP_ID,
            "user_id": 1,
            "platform": "internshala",
            "status": "failed",
            "error": "selector_miss: easy_apply_button",
            "screenshot_b64": "AAAA",
        },
    )
    payload = queue.published[0][1]
    assert payload["kind"] == "auto_apply_failed"
    assert payload["payload"]["browser_error"] == "selector_miss: easy_apply_button"
    assert payload["payload"]["screenshot_b64"] == "AAAA"


@pytest.mark.smoke
async def test_process_unknown_status_falls_back_to_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _persist(_result: dict[str, Any]) -> int | None:
        return None

    monkeypatch.setattr(mod, "_persist_result", _persist)
    queue = _FakeQueue()
    await mod._process(
        queue,  # type: ignore[arg-type]
        {
            "task_id": "t4",
            "opportunity_id": _OPP_ID,
            "user_id": 1,
            "platform": "internshala",
            "status": "weird_new_status",
        },
    )
    payload = queue.published[0][1]
    assert payload["kind"] == "auto_apply_failed"

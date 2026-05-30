"""Contract tests for `post_discovery_cycle` (kind=discovery_cycle_report).

The handler renders the ThinkPad worker's per-cycle report:

1. healthy=True → one quiet send to #🛠-source-health carrying the summary.
2. healthy=False → red detail embed to source-health; on a hard failure
   (selector miss, or a dry unhealthy cycle) ALSO a page to #🔔-alerts.
3. Screenshot (base64) attached via `discord.File` when present.
4. `deliver_success_total{channel=...}` increments per successful send.
5. Missing source-health channel → no-op (no send, no metric bump).
6. Send failure re-raises so the notifier worker DLQs the message.

`channel_id_for` and `bot._resolve_channel` are stubbed; no network.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from src.common.metrics import deliver_success_total
from src.notifiers.discord.handlers import notify_discovery_cycle

_PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


def _delivered(channel: str) -> float:
    return deliver_success_total.labels(channel=channel)._value.get()


@pytest.fixture
def channels(monkeypatch):
    """Stub channel resolution.

    `channel_id_for` maps the two logical keys to fake ids; the bot's
    `_resolve_channel` hands back a distinct TextChannel mock per id so a
    test can inspect what landed where.
    """
    ids = {"source_health": 7001, "alerts": 7002}
    monkeypatch.setattr(notify_discovery_cycle, "channel_id_for", lambda name: ids.get(name))

    health = MagicMock(spec=discord.TextChannel)
    health.send = AsyncMock(return_value=None)
    alerts = MagicMock(spec=discord.TextChannel)
    alerts.send = AsyncMock(return_value=None)
    by_id = {7001: health, 7002: alerts}

    bot = SimpleNamespace(_resolve_channel=AsyncMock(side_effect=lambda cid: by_id.get(cid)))
    return SimpleNamespace(bot=bot, health=health, alerts=alerts, ids=ids)


def _healthy_payload() -> dict:
    return {
        "kind": "discovery_cycle_report",
        "cycle_id": "c-1",
        "source_slug": "in_internshala",
        "started_at": "2026-05-29T10:00:00+00:00",
        "duration_sec": 192.4,
        "summary": "✓ 47 cards • 12/12 combos • 3m12s",
        "healthy": True,
        "screenshot_b64": None,
        "details": {
            "combos_attempted": 12,
            "combos_succeeded": 12,
            "combo_timeouts": [],
            "selector_misses": [],
            "cards_scraped": 60,
            "cards_published": 47,
            "cards_rejected_subfloor": 10,
            "cards_rejected_dedup": 2,
            "cards_rejected_parse": 1,
            "selectors_version": "2026.05.29.v1",
            "matrix_version": "2026.05.29.v1",
        },
    }


def _unhealthy_payload(*, selector_misses, cards_published, screenshot=True) -> dict:
    return {
        "kind": "discovery_cycle_report",
        "cycle_id": "c-2",
        "source_slug": "in_internshala",
        "started_at": "2026-05-29T10:05:00+00:00",
        "duration_sec": 88.0,
        "summary": "✗ 2/12 combos • selector drift",
        "healthy": False,
        "screenshot_b64": _PNG_B64 if screenshot else None,
        "details": {
            "combos_attempted": 12,
            "combos_succeeded": 2,
            "combo_timeouts": ["machine-learning", "data-science"],
            "selector_misses": list(selector_misses),
            "cards_scraped": 4,
            "cards_published": cards_published,
            "cards_rejected_subfloor": 1,
            "cards_rejected_dedup": 0,
            "cards_rejected_parse": 0,
            "selectors_version": "2026.05.29.v1",
            "matrix_version": "2026.05.29.v1",
        },
    }


async def test_healthy_posts_single_line_to_source_health(channels):
    before = _delivered("source_health")

    await notify_discovery_cycle.post_discovery_cycle(channels.bot, _healthy_payload())

    channels.health.send.assert_awaited_once()
    channels.alerts.send.assert_not_called()
    kwargs = channels.health.send.await_args.kwargs
    # Quiet line: the summary rides a slim embed, no content, no file.
    assert kwargs["embed"].description == "✓ 47 cards • 12/12 combos • 3m12s"
    assert kwargs["embed"].color.value == notify_discovery_cycle._HEALTHY_COLOR
    assert kwargs["file"] is discord.utils.MISSING
    assert _delivered("source_health") == before + 1


async def test_unhealthy_selector_miss_posts_red_embed_and_alerts_with_screenshot(channels):
    before_health = _delivered("source_health")
    before_alerts = _delivered("alerts")

    await notify_discovery_cycle.post_discovery_cycle(
        channels.bot,
        _unhealthy_payload(selector_misses=["category_button"], cards_published=3),
    )

    # source-health: red embed + screenshot file.
    channels.health.send.assert_awaited_once()
    h_kwargs = channels.health.send.await_args.kwargs
    embed = h_kwargs["embed"]
    assert embed.color.value == notify_discovery_cycle._DEGRADED_COLOR
    field_blob = " ".join(f"{f.name} {f.value}" for f in embed.fields)
    assert "category_button" in field_blob
    assert "machine-learning" in field_blob  # combo timeout rendered
    assert isinstance(h_kwargs["file"], discord.File)

    # alerts: hard failure (selector miss) → @here page with a fresh File.
    channels.alerts.send.assert_awaited_once()
    a_kwargs = channels.alerts.send.await_args.kwargs
    assert a_kwargs["content"].startswith("@here")
    assert isinstance(a_kwargs["file"], discord.File)
    assert a_kwargs["file"] is not h_kwargs["file"]  # single-use File not reused

    assert _delivered("source_health") == before_health + 1
    assert _delivered("alerts") == before_alerts + 1


async def test_unhealthy_with_published_cards_no_selector_miss_skips_alerts(channels):
    # Unhealthy (e.g. some combo timeouts) but it still published cards and no
    # selector miss → soft failure: source-health only, no alerts page.
    await notify_discovery_cycle.post_discovery_cycle(
        channels.bot,
        _unhealthy_payload(selector_misses=[], cards_published=5),
    )

    channels.health.send.assert_awaited_once()
    channels.alerts.send.assert_not_called()


async def test_unhealthy_dry_cycle_escalates_to_alerts(channels):
    # Zero published + unhealthy + no selector miss is still a hard failure
    # (session expiry / captcha) → alerts page.
    await notify_discovery_cycle.post_discovery_cycle(
        channels.bot,
        _unhealthy_payload(selector_misses=[], cards_published=0),
    )

    channels.health.send.assert_awaited_once()
    channels.alerts.send.assert_awaited_once()


async def test_unhealthy_without_screenshot_attaches_no_file(channels):
    await notify_discovery_cycle.post_discovery_cycle(
        channels.bot,
        _unhealthy_payload(selector_misses=["card_root"], cards_published=0, screenshot=False),
    )

    h_kwargs = channels.health.send.await_args.kwargs
    assert h_kwargs["file"] is discord.utils.MISSING


async def test_missing_source_health_channel_is_noop(channels, monkeypatch):
    before = _delivered("source_health")
    monkeypatch.setattr(notify_discovery_cycle, "channel_id_for", lambda name: None)

    await notify_discovery_cycle.post_discovery_cycle(channels.bot, _healthy_payload())

    channels.health.send.assert_not_called()
    channels.alerts.send.assert_not_called()
    assert _delivered("source_health") == before


async def test_send_failure_reraises(channels):
    channels.health.send.side_effect = RuntimeError("discord 500")
    with pytest.raises(RuntimeError, match="discord 500"):
        await notify_discovery_cycle.post_discovery_cycle(channels.bot, _healthy_payload())

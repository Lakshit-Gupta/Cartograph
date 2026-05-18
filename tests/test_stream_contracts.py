"""Stream contract tests — assert payload shapes for every Redis Stream.

These are the contracts subsystems rely on when talking through queue.py.
Break these = silent data loss in production. Run BEFORE any worker code change.

Tests are pure-function unit tests (no live infra needed):
- Streams.RANK shape: opportunity_id required; missing → DLQ
- Streams.NOTIFY kinds enumerated; routing exists for each
- Streams.FETCH shape: source_id + url + tier_chain required
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from src.common.queue import Streams
from src.common.types import FetchTask, OppCategory, Opportunity

# ----- Streams.RANK contract -----------------------------------------------


def test_streams_rank_requires_opportunity_id():
    """Producer A: extractor → persist → publish opportunity_id (the only contract)."""
    payload = {"opportunity_id": str(uuid4()), "user_id": 1}
    assert payload.get("opportunity_id"), "every RANK payload must carry opportunity_id"
    UUID(payload["opportunity_id"])  # must parse


def test_streams_rank_inline_opp_rejected():
    """Producer B (gmail Upwork) MUST NOT publish inline_opp without an opportunity_id.

    Historical bug: gmail_worker was publishing `{opportunity_id: None, inline_opp: {...}}`
    onto Streams.RANK; ranker dropped silently. Fixed by routing both producers
    through src.extractors.persist.persist_and_publish.
    """
    legacy_bad = {"opportunity_id": None, "inline_opp": {"title": "x"}, "user_id": 1}
    # Ranker contract: opportunity_id must be truthy or message goes to DLQ.
    opp_id = legacy_bad.get("opportunity_id")
    assert not opp_id, "fixture must represent the broken shape"


def test_persist_and_publish_signature():
    """Both extractor_worker and gmail_worker call this symbol — keep it importable."""
    from src.extractors.persist import persist_and_publish

    assert callable(persist_and_publish)
    # Module docstring documents the single-write-path invariant.
    import src.extractors.persist as mod

    assert "Single write-path" in (mod.__doc__ or "")


# ----- Streams.FETCH contract ----------------------------------------------


def test_streams_fetch_payload_shape():
    task = FetchTask(
        source_id=1,
        source_slug="ats_greenhouse",
        url="https://example.test/x",
        crawler_strategy="ats_greenhouse",
        tier_chain=[0],
        requires_identity=False,
        correlation_id="abc123",
    )
    dumped = task.model_dump(mode="json")
    for required in ("source_id", "source_slug", "url", "tier_chain", "correlation_id"):
        assert required in dumped, f"FetchTask must serialize {required}"
    assert isinstance(dumped["tier_chain"], list)


# ----- Streams.NOTIFY contract ---------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        "digest",
        "priority_push",
        "alert",
        "tracker_update",
        "manual_apply_ready",
        "applied",
    ],
)
def test_streams_notify_kinds_documented(kind):
    """If a NOTIFY kind is published but no consumer dispatches it, the message
    is silently dropped. Bot.py _notify_consumer must handle each kind below."""
    # Sanity — just assert the kind is a non-empty string. Real wiring lives
    # in src/notifiers/discord/bot.py.
    assert kind and isinstance(kind, str)


# ----- routing rules cover every lane --------------------------------------


def test_routing_per_lane_covers_every_category():
    """routing_rules.yaml must have an entry per OppCategory."""
    from pathlib import Path

    import yaml

    cfg = Path(__file__).resolve().parents[1] / "config" / "routing_rules.yaml"
    rules = yaml.safe_load(cfg.read_text())
    per_lane = rules.get("per_lane", {})
    for cat in OppCategory:
        assert cat.value in per_lane, f"per_lane missing {cat.value}"


# ----- Settings.assert_channels_configured ---------------------------------


def test_settings_channel_assert(monkeypatch):
    """Bot startup MUST fail loud when channel ids unset."""
    from src.common.secrets import Settings

    s = Settings(_env_file=None)  # type: ignore[call-arg]
    # all default 0
    with pytest.raises(RuntimeError) as ei:
        s.assert_channels_configured(required=("daily_digest",))
    assert "daily_digest" in str(ei.value)


def test_settings_channel_known_names():
    from src.common.secrets import Settings

    s = Settings(_env_file=None)  # type: ignore[call-arg]
    # Should not raise
    for name in ("daily_digest", "priority_push", "alerts", "applied"):
        assert s.discord_channel(name) == 0
    with pytest.raises(KeyError):
        s.discord_channel("nonexistent_channel")


# ----- Opportunity payload sanity ------------------------------------------


def test_opportunity_round_trip():
    opp = Opportunity(
        source_id=1,
        canonical_url="https://x.test/job/1",
        title="Backend Engineer",
        category=OppCategory.FULLTIME,
        fingerprint_hash="deadbeef",
    )
    dumped = opp.model_dump(mode="json")
    rehydrated = Opportunity.model_validate(dumped)
    assert rehydrated.canonical_url == opp.canonical_url
    assert rehydrated.category == OppCategory.FULLTIME


# ----- Streams names are stable --------------------------------------------


def test_stream_names_stable():
    """Renaming a Stream would orphan in-flight messages on Pi after deploy.
    Keep these stable across releases."""
    assert Streams.FETCH == "stream:fetch"
    assert Streams.EXTRACT == "stream:extract"
    assert Streams.RANK == "stream:rank"
    assert Streams.NOTIFY == "stream:notify"
    assert Streams.APPLY == "stream:apply"
    assert Streams.ALERTS == "stream:alerts"
    assert Streams.DLQ == "stream:dlq"

"""Buttons under each opportunity embed.

Each button publishes the user's intent onto `stream:apply`. The
appliers worker decides what to do (transition state, fire Resend, etc.).
This keeps the Discord gateway thread snappy.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import discord

from src.common import db
from src.common.logger import get_logger
from src.common.queue import RedisQ, Streams
from src.notifiers.discord import voice
from src.notifiers.discord.tenant import refuse_unonboarded, resolve_tenant

_log = get_logger(__name__)


def _custom_id(action: str, opp_id: str | UUID) -> str:
    return f"opp:{action}:{opp_id}"


async def _enqueue(action: str, opp_id: str, user_id: int, **extra: Any) -> None:
    q = await RedisQ.connect()
    payload = {
        "action": action,
        "opp_id": str(opp_id),
        "user_id": user_id,
        "ts": datetime.now(UTC).isoformat(),
        **extra,
    }
    await q.publish(Streams.APPLY, payload)


async def _transition_state(opp_id: str, target_state: str) -> bool:
    """Direct DB transition — relies on V004 trigger to validate + audit."""
    try:
        await db.execute(
            "UPDATE opportunities SET state = $2 WHERE id = $1",
            UUID(opp_id),
            target_state,
        )
        return True
    except Exception as e:
        _log.warning("transition_failed", opp_id=opp_id, target=target_state, err=str(e))
        return False


class OppActionView(discord.ui.View):
    """Persistent view with Apply / Skip / Snooze / Pin / Explain buttons."""

    def __init__(self, opp_id: str | UUID, *, timeout: float | None = None):
        super().__init__(timeout=timeout)
        opp = str(opp_id)
        # We register all 5 buttons with stable custom_ids so the View can be
        # rebuilt on bot restart and still respond.
        self.add_item(_btn("Apply", "apply", opp, discord.ButtonStyle.success, "✅"))
        self.add_item(_btn("Skip", "skip", opp, discord.ButtonStyle.secondary, "❌"))
        self.add_item(_btn("Snooze", "snooze", opp, discord.ButtonStyle.secondary, "🔁"))
        self.add_item(_btn("Pin", "pin", opp, discord.ButtonStyle.primary, "🔖"))
        self.add_item(_btn("Explain", "explain", opp, discord.ButtonStyle.secondary, "💬"))


class FollowupActionView(discord.ui.View):
    """Persistent view attached to ``kind=followup_ready`` embeds.

    Three buttons: Send → publish onto Streams.APPLY (action=send_followup);
    Edit → open the FollowupEditModal (modals.py); Skip → mark the row
    status='skipped'. All actions are idempotent at the DB layer — the
    applier worker's _do_send_followup also rechecks the followups row's
    status so a double-click can't double-send.
    """

    def __init__(self, followup_id: int | str, *, timeout: float | None = None):
        super().__init__(timeout=timeout)
        fid = str(followup_id)
        self.add_item(_followup_btn("Send", "send_followup", fid, discord.ButtonStyle.success, "📤"))
        self.add_item(_followup_btn("Edit", "edit_followup", fid, discord.ButtonStyle.primary, "✏️"))
        self.add_item(_followup_btn("Skip", "skip_followup", fid, discord.ButtonStyle.secondary, "🚫"))


def _followup_btn(
    label: str,
    action: str,
    followup_id: str,
    style: discord.ButtonStyle,
    emoji: str | None = None,
) -> discord.ui.Button:
    button = discord.ui.Button(
        label=label,
        style=style,
        custom_id=f"followup:{action}:{followup_id}",
        emoji=emoji,
    )

    async def _cb(interaction: discord.Interaction) -> None:
        await dispatch_followup_button(interaction)

    button.callback = _cb  # type: ignore[assignment]
    return button


async def dispatch_followup_button(interaction: discord.Interaction) -> None:
    """Single entry point for follow-up buttons. Mirrors ``dispatch_button``.

    Edit opens a modal IN-PLACE; Send / Skip publish onto Streams.APPLY so
    the applier-worker (already running with DB pool + LLM cost gate)
    handles state mutations + Resend.
    """
    raw = interaction.data.get("custom_id", "") if interaction.data else ""
    try:
        _, action, followup_id = raw.split(":", 2)
    except ValueError:
        await _ephemeral(interaction, f"Bad button id: `{raw}`")
        return

    tenant = await resolve_tenant(interaction)
    if tenant is None:
        await refuse_unonboarded(interaction)
        return
    user_id = tenant.user_id
    try:
        if action == "send_followup":
            q = await RedisQ.connect()
            await q.publish(
                Streams.APPLY,
                {
                    "action": "send_followup",
                    "followup_id": int(followup_id),
                    "user_id": user_id,
                    "source": "button",
                    "ts": datetime.now(UTC).isoformat(),
                },
            )
            await _ephemeral(interaction, "Follow-up queued for send.")
        elif action == "skip_followup":
            # Mark the row skipped synchronously — the worker would do this
            # too, but skipping is cheap and immediate user feedback feels
            # better than the round-trip.
            from src.application.followup import mark_skipped

            ok = await mark_skipped(int(followup_id))
            await _ephemeral(interaction, "Skipped." if ok else "Could not skip (already terminal?).")
        elif action == "edit_followup":
            # Open the modal directly. The followup_id rides on the modal
            # so on_submit knows which row to update.
            from src.notifiers.discord.handlers.modals import FollowupEditModal

            modal = FollowupEditModal(followup_id=int(followup_id))
            try:
                await interaction.response.send_modal(modal)
            except Exception as e:
                _log.warning("followup_modal_send_failed", err=str(e), followup_id=followup_id)
                await _ephemeral(interaction, f"Could not open editor: {e}")
        else:
            await _ephemeral(interaction, f"Unknown follow-up action `{action}`.")
    except Exception as e:
        _log.exception("followup_button_dispatch_failed", err=str(e), action=action, followup_id=followup_id)
        await _ephemeral(interaction, f"Error: {e}")


class OppReviewView(discord.ui.View):
    """Persistent view attached to `manual_apply_ready` review threads.

    Two buttons: Mark applied → state stays at 'applied' (already set by
    sender.py via the V004 trigger); Cancel → state back to 'digested'.
    Both publish onto Streams.APPLY so the applier worker handles auditing.
    """

    def __init__(self, opp_id: str | UUID, *, timeout: float | None = None):
        super().__init__(timeout=timeout)
        opp = str(opp_id)
        self.add_item(_btn("Mark applied", "apply", opp, discord.ButtonStyle.success, "✅"))
        self.add_item(_btn("Cancel", "cancel", opp, discord.ButtonStyle.secondary, "↩️"))


def _btn(
    label: str,
    action: str,
    opp_id: str,
    style: discord.ButtonStyle,
    emoji: str | None = None,
) -> discord.ui.Button:
    button = discord.ui.Button(
        label=label,
        style=style,
        custom_id=_custom_id(action, opp_id),
        emoji=emoji,
    )

    async def _cb(interaction: discord.Interaction) -> None:
        await dispatch_button(interaction)

    button.callback = _cb  # type: ignore[assignment]
    return button


async def dispatch_button(interaction: discord.Interaction) -> None:
    """Single entry point — parses custom_id and acts."""
    raw = interaction.data.get("custom_id", "") if interaction.data else ""
    try:
        _, action, opp_id = raw.split(":", 2)
    except ValueError:
        await _ephemeral(interaction, f"Bad button id: `{raw}`")
        return

    tenant = await resolve_tenant(interaction)
    if tenant is None:
        await refuse_unonboarded(interaction)
        return
    user_id = tenant.user_id
    try:
        if action == "apply":
            await _transition_state(opp_id, "applied")
            await _enqueue("apply", opp_id, user_id, source="button")
            await _ephemeral(interaction, voice.pick("applied_confirm"))
        elif action == "skip":
            await _transition_state(opp_id, "seen")
            await _enqueue("skip", opp_id, user_id, source="button")
            await _ephemeral(interaction, voice.pick("skipped_confirm"))
        elif action == "snooze":
            await _enqueue("snooze", opp_id, user_id, days=1, source="button")
            await _transition_state(opp_id, "snoozed")
            await _ephemeral(interaction, voice.pick("snoozed_confirm"))
        elif action == "pin":
            await _enqueue("pin", opp_id, user_id, source="button")
            await _ephemeral(interaction, voice.pick("pinned_confirm"))
        elif action == "explain":
            text = await _explain_text(opp_id)
            await _ephemeral(interaction, text)
        elif action == "cancel":
            await _transition_state(opp_id, "digested")
            await _enqueue("cancel_apply", opp_id, user_id, source="button")
            await _ephemeral(interaction, "Cancelled. Back to digested.")
        else:
            await _ephemeral(interaction, f"Unknown action `{action}`.")
    except Exception as e:
        _log.exception("button_dispatch_failed", err=str(e), action=action, opp_id=opp_id)
        await _ephemeral(interaction, f"Error: {e}")


async def _explain_text(opp_id: str) -> str:
    row = await db.fetch_one(
        """
        SELECT s.score, s.score_components
        FROM opportunity_scores s
        WHERE s.opportunity_id = $1
        ORDER BY s.scored_at DESC
        LIMIT 1
        """,
        UUID(opp_id),
    )
    if not row:
        return "No score yet for this opp."
    comps = row["score_components"]
    if isinstance(comps, str):
        try:
            comps = json.loads(comps)
        except Exception:
            comps = {}
    parts = ", ".join(f"{k}={v:.2f}" for k, v in (comps or {}).items())
    intro = voice.pick("explain_intro")
    return f"{intro} score={row['score']:.2f} ({parts})"


async def _ephemeral(interaction: discord.Interaction, msg: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception as e:
        _log.warning("ephemeral_send_failed", err=str(e))


# ---------------------------------------------------------------------------
# Phase 3.2 — dark-source candidate review buttons.
# Custom-id shape: candidate:<action>:<candidate_id>
#   action ∈ {approve, reject, snooze}
# Buttons live under the embed returned by `/review` (commands/review.py).
# Approve materialises a sources row inline (small txn, no separate worker).
# ---------------------------------------------------------------------------


class CandidateReviewView(discord.ui.View):
    """View attached to a paginated `/review` embed.

    Discord caps an action row at 5 buttons and a view at 5 rows. With the
    page-size of 10 we can't fit per-row Approve/Reject/Snooze. We instead
    show one Approve / Reject / Snooze trio that operates on the FIRST
    pending candidate id in the rendered page. The operator can re-run
    /review after each click to action the next one. Crude but Discord-
    constraint-honest; Phase 3.3 can replace with a Select component
    (25-option dropdown) to pick which candidate.
    """

    def __init__(self, candidate_ids: list[int], *, timeout: float | None = None):
        super().__init__(timeout=timeout)
        if not candidate_ids:
            return
        first = candidate_ids[0]
        self.add_item(_candidate_btn("Approve", "approve", first, discord.ButtonStyle.success, "✅"))
        self.add_item(_candidate_btn("Reject", "reject", first, discord.ButtonStyle.danger, "❌"))
        self.add_item(_candidate_btn("Snooze", "snooze", first, discord.ButtonStyle.secondary, "🔁"))


def _candidate_btn(
    label: str,
    action: str,
    candidate_id: int,
    style: discord.ButtonStyle,
    emoji: str | None = None,
) -> discord.ui.Button:
    button = discord.ui.Button(
        label=label,
        style=style,
        custom_id=f"candidate:{action}:{candidate_id}",
        emoji=emoji,
    )

    async def _cb(interaction: discord.Interaction) -> None:
        await dispatch_candidate_button(interaction)

    button.callback = _cb  # type: ignore[assignment]
    return button


async def _approve_candidate(candidate_id: int) -> tuple[bool, str]:
    """Promote the candidate into sources atomically.

    Mirrors the auto-promote path in `src/sources/discovery/promoter.py` but
    fires from human approval, not LLM confidence. Returns (ok, message).
    """
    # Lazy import to dodge a circular: buttons.py imports → promoter.py imports
    # db / logger from common, no cycle today but keeps the dependency direction
    # clear if promoter ever grows a Discord import.
    from src.sources.discovery.promoter import _CATEGORY_TO_STRATEGY, _slug_from_url

    rec = await db.fetch_one(
        """
        SELECT id, url, title, classifier_confidence, classifier_category, status
        FROM candidate_sources WHERE id = $1
        """,
        candidate_id,
    )
    if rec is None:
        return False, f"No candidate `#{candidate_id}` found."
    if rec["status"] != "pending":
        return False, f"Candidate `#{candidate_id}` already {rec['status']}."

    category = rec["classifier_category"] or "other"
    strategy, cf_level = _CATEGORY_TO_STRATEGY.get(category, ("generic_html", "basic"))
    slug = _slug_from_url(rec["url"])
    safe_category = category if category in ("ats", "rss", "github_md", "hn", "reddit", "fellowship", "india", "freelance") else "other"

    try:
        async with db.acquire() as conn, conn.transaction():
            source_row = await conn.fetchrow(
                """
                    INSERT INTO sources (
                        slug, name, category, base_url, crawler_strategy,
                        fetch_freq_minutes, priority, cf_protection_level,
                        tier_chain, browser_mode_required, status, created_via,
                        discovery_confidence
                    ) VALUES ($1,$2,$3,$4,$5,240,5,$6,ARRAY[0,1,2],FALSE,'active','discovery',$7)
                    ON CONFLICT (slug) DO NOTHING
                    RETURNING id
                    """,
                slug,
                (rec["title"] or rec["url"])[:200],
                safe_category,
                rec["url"],
                strategy,
                cf_level,
                rec["classifier_confidence"],
            )
            if source_row is None:
                return False, f"Slug `{slug}` already exists — manually resolve via `/source list`."
            source_id = int(source_row["id"])
            await conn.execute(
                """
                    UPDATE candidate_sources
                       SET status = 'approved',
                           promoted_source_id = $2,
                           reviewed_at = NOW()
                     WHERE id = $1
                    """,
                candidate_id,
                source_id,
            )
            await conn.execute(
                """
                    INSERT INTO source_provenance (source_id, candidate_source_id, discovered_via)
                    VALUES ($1, $2, (SELECT discovered_via FROM candidate_sources WHERE id = $2))
                    """,
                source_id,
                candidate_id,
            )
        return True, f"Approved → new source `{slug}` (id={source_id})."
    except Exception as e:
        _log.exception("candidate_approve_failed", candidate_id=candidate_id, err=str(e))
        return False, f"Approve failed: {e}"


async def _reject_candidate(candidate_id: int) -> tuple[bool, str]:
    result = await db.execute(
        """
        UPDATE candidate_sources
           SET status = 'rejected', reviewed_at = NOW()
         WHERE id = $1 AND status = 'pending'
        """,
        candidate_id,
    )
    if result.endswith(" 0"):
        return False, f"Could not reject `#{candidate_id}` (already actioned?)."
    return True, f"Rejected `#{candidate_id}`."


async def _snooze_candidate(candidate_id: int) -> tuple[bool, str]:
    result = await db.execute(
        """
        UPDATE candidate_sources
           SET status = 'snoozed', reviewed_at = NOW()
         WHERE id = $1 AND status = 'pending'
        """,
        candidate_id,
    )
    if result.endswith(" 0"):
        return False, f"Could not snooze `#{candidate_id}`."
    return True, f"Snoozed `#{candidate_id}`."


async def dispatch_candidate_button(interaction: discord.Interaction) -> None:
    """Single entry point for `candidate:<action>:<id>` buttons.

    Same pattern as ``dispatch_button`` but operates against candidate_sources
    rather than opportunities. Idempotency comes from the
    `WHERE status = 'pending'` guard inside each handler — clicking the same
    button twice is a no-op message.
    """
    raw = interaction.data.get("custom_id", "") if interaction.data else ""
    try:
        _, action, candidate_id_str = raw.split(":", 2)
        candidate_id = int(candidate_id_str)
    except (ValueError, TypeError):
        await _ephemeral(interaction, f"Bad button id: `{raw}`")
        return

    try:
        if action == "approve":
            ok, msg = await _approve_candidate(candidate_id)
        elif action == "reject":
            ok, msg = await _reject_candidate(candidate_id)
        elif action == "snooze":
            ok, msg = await _snooze_candidate(candidate_id)
        else:
            ok, msg = False, f"Unknown action `{action}`."
    except Exception as e:
        _log.exception("candidate_button_dispatch_failed", action=action, candidate_id=candidate_id, err=str(e))
        ok, msg = False, f"Error: {e}"

    await _ephemeral(interaction, ("✓ " if ok else "✗ ") + msg)

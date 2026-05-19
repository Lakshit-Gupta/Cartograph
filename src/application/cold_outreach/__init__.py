"""Phase 2.1 — cold-email outbound funnel.

Flow:
    target_companies (DB)
      → OutboundProvider.find_contacts(domain)       (apollo / hunter)
      → cap.allow_send(user_id, recipient)            (daily cap + warmup ramp)
      → drafter.draft_intro(profile, company, contact) (LLM, 90 words max)
      → sanitizer.scrub(subject + body)               (strip HTML / control)
      → notifiers.email.send_email(no attachments)
      → INSERT INTO outbound_messages

Gmail watcher classifies replies and updates outbound_messages.response_status
via the `In-Reply-To` / `References` header chain (same machinery already used
for `applications`).

All modules in this package stay under 300 lines per CLAUDE.md structure
rule. base.py owns the Protocol surface so apollo/hunter/null are swappable.
"""

from src.application.cold_outreach.base import Contact, OutboundProvider

__all__ = ["Contact", "OutboundProvider"]

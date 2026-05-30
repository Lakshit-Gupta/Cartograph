"""Shared Pydantic types crossing subsystem boundaries via Redis Streams."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ApplyMethod(StrEnum):
    EMAIL = "email"
    ATS_FORM = "ats_form"
    EXTERNAL = "external"
    IN_PLATFORM = "in_platform"
    EMBEDDED_FORM = "embedded_form"


class OppState(StrEnum):
    NEW = "new"
    QUEUED = "queued"
    RANKED = "ranked"
    DIGESTED = "digested"
    SEEN = "seen"
    SNOOZED = "snoozed"
    APPLIED = "applied"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    EXPIRED = "expired"


class RemoteType(StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNSPECIFIED = "unspecified"


class OppCategory(StrEnum):
    FULLTIME = "fulltime"
    INTERNSHIP = "internship"
    FELLOWSHIP = "fellowship"
    FREELANCE = "freelance"
    CONTRACT = "contract"
    UNKNOWN = "unknown"


class IdentityBanStatus(StrEnum):
    HEALTHY = "healthy"
    SUSPECT = "suspect"
    QUARANTINED = "quarantined"
    BANNED = "banned"


class FetchTask(BaseModel):
    """Task pushed onto `stream:fetch` for crawler workers."""

    model_config = ConfigDict(extra="forbid")

    source_id: int
    source_slug: str
    url: str
    crawler_strategy: str
    tier_chain: list[int] = Field(default_factory=lambda: [0])
    requires_identity: bool = False
    correlation_id: str
    queued_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class FetchResult(BaseModel):
    """Output of a fetcher — pushed onto `stream:extract`."""

    model_config = ConfigDict(extra="forbid")

    source_id: int
    source_slug: str
    url: str
    http_status: int
    content: str
    content_type: str | None = None
    tier_used: int
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    correlation_id: str
    error: str | None = None


class Opportunity(BaseModel):
    """Canonical extracted opportunity — pushed onto `stream:rank`."""

    model_config = ConfigDict(extra="ignore")

    id: UUID | None = None
    source_id: int
    canonical_url: str
    title: str
    company: str | None = None
    description: str | None = None
    comp_min: float | None = None
    comp_max: float | None = None
    comp_currency: str | None = None
    comp_period: str | None = None
    location: str | None = None
    remote_type: RemoteType = RemoteType.UNSPECIFIED
    category: OppCategory = OppCategory.UNKNOWN
    posted_at: datetime | None = None
    expires_at: datetime | None = None
    years_experience_min: int | None = None
    apply_url: str | None = None
    apply_method: ApplyMethod | None = None
    raw_payload_s3_key: str | None = None
    fingerprint_hash: str
    extraction_tier: int = 0
    extraction_confidence: float = 0.0


class RankedOpportunity(BaseModel):
    """Pushed onto `stream:notify` after ranking."""

    model_config = ConfigDict(extra="ignore")

    opportunity_id: UUID
    user_id: int
    score: float
    score_components: dict[str, float] = Field(default_factory=dict)
    ranker_version: str = "v1"


class NotificationTask(BaseModel):
    """Notifier worker payload."""

    model_config = ConfigDict(extra="ignore")

    kind: str  # 'digest' | 'priority_push' | 'alert' | 'tracker_update'
    user_id: int
    payload: dict[str, Any] = Field(default_factory=dict)


class IdentityLease(BaseModel):
    """Returned by identity_vault.checkout."""

    model_config = ConfigDict(extra="forbid")

    identity_id: int
    platform: str
    cookies: dict[str, str] = Field(default_factory=dict)
    ua_string: str | None = None
    lease_id: int
    expires_at: datetime

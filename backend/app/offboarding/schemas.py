"""Pydantic v2 request + response models (§7 validation, §8 API contract).

Request models validate and normalize input at the edge (enum membership, sane
lengths) so services receive clean data. Response models are the stable
frontend contract: Offboarding Register, Case Detail, Workflow Timeline,
Blockers, Audit Trail, and Completion Readiness.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .constants import ExitType, RevocationMode


# ════════════════════════════════════════════════════════════════════════════
# REQUEST MODELS
# ════════════════════════════════════════════════════════════════════════════
class CaseCreate(BaseModel):
    worker_id: Optional[int] = None
    engagement_id: str = Field(min_length=1, max_length=80)
    exit_type: str = Field(default=ExitType.VOLUNTARY.value)
    jurisdiction: Optional[str] = Field(default=None, max_length=40)
    worker_type: Optional[str] = Field(default=None, max_length=40)
    risk_level: str = Field(default="standard", max_length=20)
    department: Optional[str] = Field(default=None, max_length=100)
    data_classification: str = Field(default="internal", max_length=40)
    revocation_mode: str = Field(default=RevocationMode.SCHEDULED.value)
    final_access_at: Optional[datetime] = None
    termination_reason: Optional[str] = Field(default=None, max_length=4000)
    notes: Optional[str] = Field(default=None, max_length=4000)

    @field_validator("exit_type")
    @classmethod
    def _valid_exit(cls, v: str) -> str:
        if v not in {e.value for e in ExitType}:
            raise ValueError(f"invalid exit_type '{v}'")
        return v

    @field_validator("revocation_mode")
    @classmethod
    def _valid_mode(cls, v: str) -> str:
        if v not in {m.value for m in RevocationMode}:
            raise ValueError(f"invalid revocation_mode '{v}'")
        return v


class CaseUpdate(BaseModel):
    """PATCH — only mutable while the case is in Draft. All fields optional."""
    exit_type: Optional[str] = None
    jurisdiction: Optional[str] = Field(default=None, max_length=40)
    worker_type: Optional[str] = Field(default=None, max_length=40)
    risk_level: Optional[str] = Field(default=None, max_length=20)
    department: Optional[str] = Field(default=None, max_length=100)
    data_classification: Optional[str] = Field(default=None, max_length=40)
    revocation_mode: Optional[str] = None
    final_access_at: Optional[datetime] = None
    termination_reason: Optional[str] = Field(default=None, max_length=4000)
    notes: Optional[str] = Field(default=None, max_length=4000)


class SubmitRequest(BaseModel):
    note: Optional[str] = Field(default=None, max_length=2000)


class ApproveRequest(BaseModel):
    note: Optional[str] = Field(default=None, max_length=2000)
    schedule: bool = True               # SCHEDULED vs immediate IN_PROGRESS
    final_access_at: Optional[datetime] = None


class CancelRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class ReverseRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class LegalHoldApply(BaseModel):
    matter_ref: Optional[str] = Field(default=None, max_length=120)
    reason: Optional[str] = Field(default=None, max_length=4000)
    notes: Optional[str] = Field(default=None, max_length=4000)
    review_due_at: Optional[datetime] = None


class TaskComplete(BaseModel):
    result: Dict[str, Any] = Field(default_factory=dict)
    note: Optional[str] = Field(default=None, max_length=2000)


class BlockerCreate(BaseModel):
    blocker_type: str = Field(min_length=1, max_length=40)
    reason: Optional[str] = Field(default=None, max_length=2000)
    severity: str = Field(default="high", max_length=20)
    task_id: Optional[str] = None


class WaiverCreate(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
    justification: Optional[str] = Field(default=None, max_length=4000)
    task_id: Optional[str] = None
    blocker_id: Optional[str] = None


# ── Security API (§7) ─────────────────────────────────────────────────────────
class RevokeAccessRequest(BaseModel):
    """At least one target must be supplied. case_id is preferred (full sweep);
    worker_id alone triggers break-glass direct revocation."""
    worker_id: Optional[int] = None
    case_id: Optional[str] = Field(default=None, max_length=40)
    engagement_id: Optional[str] = Field(default=None, max_length=80)
    reason: str = Field(default="security_revoke", max_length=2000)

    @field_validator("reason")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        return v or "security_revoke"


class RetryRevocationRequest(BaseModel):
    case_id: str = Field(min_length=1, max_length=40)


# ════════════════════════════════════════════════════════════════════════════
# RESPONSE MODELS  (§8 — frontend contract)
# ════════════════════════════════════════════════════════════════════════════
class CaseSummary(BaseModel):
    """One row in the Offboarding Register."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    engagement_id: str
    worker_id: Optional[int]
    status: str
    exit_type: Optional[str]
    department: Optional[str]
    legal_hold_status: str
    risk_level: Optional[str]
    final_access_at: Optional[datetime]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]


class RegisterResponse(BaseModel):
    items: List[CaseSummary]
    total: int
    limit: int
    offset: int


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_type: str
    status: str
    required: bool
    sequence: int
    assignee_role: Optional[str]
    sla_due_at: Optional[datetime]
    sla_breached: bool
    escalated: bool
    completed_at: Optional[datetime]


class RevocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    component: str
    status: str
    attempts: int
    revoked_at: Optional[datetime]
    last_error: Optional[str]


class BlockerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    blocker_type: str
    severity: str
    reason: Optional[str]
    status: str
    task_id: Optional[str]
    resolved_at: Optional[datetime]
    created_at: Optional[datetime]


class TimelineItem(BaseModel):
    """One entry in the Workflow Timeline — derived from domain events."""
    event_type: str
    occurred_at: Optional[datetime]
    state_from: Optional[str]
    state_to: Optional[str]
    correlation_id: Optional[str]
    payload: Dict[str, Any] = Field(default_factory=dict)


class AuditTrailItem(BaseModel):
    id: str
    action: str
    entity: str
    actor_id: Optional[int]
    actor_role: Optional[str]
    created_at: Optional[datetime]
    current_hash: str
    payload: Dict[str, Any] = Field(default_factory=dict)


class CompletionReadiness(BaseModel):
    """Why a case can or cannot complete — drives the 'Complete' button state."""
    ready: bool
    case_status: str
    access_fully_revoked: bool
    tracking_stopped: bool
    open_blockers: int
    required_tasks_total: int
    required_tasks_done: int
    legal_hold_active: bool
    payroll_closed: bool
    billing_closed: bool
    retention_assigned: bool
    certificate_issued: bool
    blocking_reasons: List[str] = Field(default_factory=list)


class CaseDetail(BaseModel):
    """Full Case Detail view."""
    id: str
    tenant_id: str
    engagement_id: str
    worker_id: Optional[int]
    status: str
    blocked_from_state: Optional[str]
    exit_type: Optional[str]
    jurisdiction: Optional[str]
    worker_type: Optional[str]
    risk_level: Optional[str]
    department: Optional[str]
    data_classification: Optional[str]
    legal_hold_status: str
    revocation_mode: str
    final_access_at: Optional[datetime]
    termination_reason: Optional[str] = None   # redacted unless permitted
    notes: Optional[str]
    correlation_id: Optional[str]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]

    tasks: List[TaskOut] = Field(default_factory=list)
    revocations: List[RevocationOut] = Field(default_factory=list)
    blockers: List[BlockerOut] = Field(default_factory=list)
    readiness: Optional[CompletionReadiness] = None
    allowed_actions: List[str] = Field(default_factory=list)

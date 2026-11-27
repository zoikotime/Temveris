"""Offboarding & Former-Worker Lifecycle — SQLAlchemy models.

These tables are additive. They sit alongside the legacy single-tenant tables
(which use ``organization_id="default"``) and the commercial multi-tenant tables.
Every row carries a ``tenant_id`` and every read path filters on it, so the same
schema serves the current single-tenant deployment ("default") and real
multi-tenant orgs without change (§1).

Cross-cutting guarantees baked into the schema
  • tenant_id on every table, indexed, with tenant isolation enforced in the
    repository layer (never trust a caller-supplied tenant).
  • UUID/opaque string primary keys (see ids.py) — never enumerable integers.
  • created_at / updated_at on mutable tables; audit/event tables are
    append-only (created_at only, no updated_at, no update/delete methods).
  • Indexed: case_id, worker_id, engagement_id, status, tenant_id,
    final_access_at, legal_hold_status (the §1 index list).
  • One active case per engagement, enforced by a NULL-collapsing unique key
    (``active_engagement_key``) — see OffboardingCase below.
  • audit_events is hash-chained (prev_hash → current_hash) for tamper-evidence,
    mirroring the existing WorkEvidenceChain pattern.

All tables register against the shared ``database.database.Base`` metadata, so
``bootstrap.ensure_tables()`` and Alembic both pick them up.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey,
    Text, JSON, Index, UniqueConstraint,
)

from database.database import Base


# Default tenant for the current single-tenant deployment. Real orgs mint their
# own ids; this keeps legacy rows isolated under one stable key.
DEFAULT_TENANT = "default"


# ════════════════════════════════════════════════════════════════════════════
# 1. OFFBOARDING CASE  — the lifecycle aggregate root
# ════════════════════════════════════════════════════════════════════════════
class OffboardingCase(Base):
    __tablename__ = "offboarding_cases"

    id              = Column(String(40), primary_key=True)            # "case_<uuid>"
    tenant_id       = Column(String(80), nullable=False, default=DEFAULT_TENANT, index=True)

    worker_id       = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"),
                             nullable=True, index=True)
    engagement_id   = Column(String(80), nullable=False, index=True)

    status          = Column(String(40), nullable=False, default="draft", index=True)
    # The state a BLOCKED case must return to when its blockers clear.
    blocked_from_state = Column(String(40), nullable=True)

    # ── ABAC attributes (§4) ────────────────────────────────────────────────
    jurisdiction        = Column(String(40), nullable=True, index=True)
    worker_type         = Column(String(40), nullable=True)   # full_time | contractor | ...
    exit_type           = Column(String(40), nullable=True)   # ExitType
    risk_level          = Column(String(20), nullable=True, default="standard")
    department          = Column(String(100), nullable=True, index=True)
    data_classification = Column(String(40), nullable=True, default="internal")

    # Legal hold rolls up here so the register can filter without a join (§1).
    legal_hold_status   = Column(String(40), nullable=False, default="none", index=True)  # none|active|released

    revocation_mode     = Column(String(20), nullable=False, default="scheduled")  # RevocationMode

    # ── Sensitive, field-level protected (§4) ───────────────────────────────
    termination_reason  = Column(Text, nullable=True)

    # ── Timeline ─────────────────────────────────────────────────────────────
    final_access_at = Column(DateTime(timezone=True), nullable=True, index=True)
    scheduled_at    = Column(DateTime(timezone=True), nullable=True)
    submitted_at    = Column(DateTime(timezone=True), nullable=True)
    approved_at     = Column(DateTime(timezone=True), nullable=True)
    completed_at    = Column(DateTime(timezone=True), nullable=True)
    reversed_at     = Column(DateTime(timezone=True), nullable=True)
    cancelled_at    = Column(DateTime(timezone=True), nullable=True)

    created_by_id   = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_by_id  = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    correlation_id  = Column(String(40), nullable=True, index=True)
    notes           = Column(Text, nullable=True)
    meta            = Column(JSON, nullable=False, default=dict)

    # Set to engagement_id while the case is OPEN, NULL once it reaches a closed
    # state (completed/cancelled/reversed). Because NULLs never collide in a
    # unique index (Postgres + SQLite), the UniqueConstraint below allows many
    # closed cases per engagement but at most ONE open one — the §1
    # "unique active offboarding case per engagement" rule, enforced in the DB.
    active_engagement_key = Column(String(80), nullable=True)

    created_at      = Column(DateTime(timezone=True), default=datetime.utcnow, index=True)
    updated_at      = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("tenant_id", "active_engagement_key",
                         name="uq_offboarding_active_case_per_engagement"),
        Index("ix_offb_case_tenant_status", "tenant_id", "status"),
        Index("ix_offb_case_tenant_worker", "tenant_id", "worker_id"),
        Index("ix_offb_case_tenant_legalhold", "tenant_id", "legal_hold_status"),
    )


# ════════════════════════════════════════════════════════════════════════════
# 2. OFFBOARDING TASK  — workflow work item
# ════════════════════════════════════════════════════════════════════════════
class OffboardingTask(Base):
    __tablename__ = "offboarding_tasks"

    id          = Column(String(40), primary_key=True)        # "task_<uuid>"
    tenant_id   = Column(String(80), nullable=False, default=DEFAULT_TENANT, index=True)
    case_id     = Column(String(40), ForeignKey("offboarding_cases.id", ondelete="CASCADE"),
                         nullable=False, index=True)

    task_type   = Column(String(40), nullable=False, index=True)   # TaskType
    status      = Column(String(20), nullable=False, default="pending", index=True)  # TaskStatus
    required    = Column(Boolean, nullable=False, default=True)
    sequence    = Column(Integer, nullable=False, default=0)

    assignee_id     = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    assignee_role   = Column(String(40), nullable=True)

    # SLA / escalation tracking (§5).
    sla_due_at      = Column(DateTime(timezone=True), nullable=True, index=True)
    sla_breached    = Column(Boolean, nullable=False, default=False)
    escalated       = Column(Boolean, nullable=False, default=False)
    escalated_at    = Column(DateTime(timezone=True), nullable=True)

    started_at      = Column(DateTime(timezone=True), nullable=True)
    completed_at    = Column(DateTime(timezone=True), nullable=True)
    completed_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    waiver_id       = Column(String(40), nullable=True)

    result      = Column(JSON, nullable=True, default=dict)
    notes       = Column(Text, nullable=True)

    created_at  = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at  = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("case_id", "task_type", name="uq_offb_task_case_type"),
        Index("ix_offb_task_case_status", "case_id", "status"),
    )


# ════════════════════════════════════════════════════════════════════════════
# 3. ACCESS REVOCATION RECORD  — one row per revoked surface, per case
# ════════════════════════════════════════════════════════════════════════════
class AccessRevocationRecord(Base):
    __tablename__ = "access_revocation_records"

    id          = Column(String(40), primary_key=True)        # "rev_<uuid>"
    tenant_id   = Column(String(80), nullable=False, default=DEFAULT_TENANT, index=True)
    case_id     = Column(String(40), ForeignKey("offboarding_cases.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    worker_id   = Column(Integer, nullable=True, index=True)

    component   = Column(String(40), nullable=False, index=True)   # RevocationComponent
    status      = Column(String(20), nullable=False, default="pending", index=True)  # RevocationStatus
    attempts    = Column(Integer, nullable=False, default=0)
    last_error  = Column(Text, nullable=True)

    revoked_at  = Column(DateTime(timezone=True), nullable=True)
    reversed_at = Column(DateTime(timezone=True), nullable=True)
    evidence    = Column(JSON, nullable=True, default=dict)        # counts / ids revoked
    correlation_id = Column(String(40), nullable=True, index=True)

    created_at  = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at  = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("case_id", "component", name="uq_revocation_case_component"),
        Index("ix_revocation_tenant_status", "tenant_id", "status"),
    )


# ════════════════════════════════════════════════════════════════════════════
# 4. FORMER WORKER ACCESS  — isolated, restricted portal grant
# ════════════════════════════════════════════════════════════════════════════
class FormerWorkerAccess(Base):
    __tablename__ = "former_worker_access"

    id          = Column(String(40), primary_key=True)        # "fwa_<uuid>"
    tenant_id   = Column(String(80), nullable=False, default=DEFAULT_TENANT, index=True)
    case_id     = Column(String(40), ForeignKey("offboarding_cases.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    worker_id   = Column(Integer, nullable=True, index=True)

    status      = Column(String(20), nullable=False, default="active", index=True)  # FormerAccessStatus
    # Allowlisted document scopes ONLY (constants.ALLOWED_FORMER_WORKER_SCOPES).
    scopes      = Column(JSON, nullable=False, default=list)

    granted_at  = Column(DateTime(timezone=True), default=datetime.utcnow)
    expires_at  = Column(DateTime(timezone=True), nullable=True, index=True)
    revoked_at  = Column(DateTime(timezone=True), nullable=True)
    granted_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # Portal credential is a separate, isolated secret — the former worker never
    # re-uses their old session/refresh tokens (those are revoked at exit).
    portal_token_hash = Column(String(64), nullable=True, index=True)

    created_at  = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at  = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_fwa_tenant_status", "tenant_id", "status"),
    )


# ════════════════════════════════════════════════════════════════════════════
# 5. RETENTION ASSIGNMENT  — retention governance per case
# ════════════════════════════════════════════════════════════════════════════
class RetentionAssignment(Base):
    __tablename__ = "retention_assignments"

    id          = Column(String(40), primary_key=True)        # "ret_<uuid>"
    tenant_id   = Column(String(80), nullable=False, default=DEFAULT_TENANT, index=True)
    case_id     = Column(String(40), ForeignKey("offboarding_cases.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    worker_id   = Column(Integer, nullable=True, index=True)

    policy_code         = Column(String(60), nullable=False, default="default_retention")
    jurisdiction        = Column(String(40), nullable=True)
    data_classification = Column(String(40), nullable=True)
    retention_until     = Column(DateTime(timezone=True), nullable=True, index=True)
    deletion_eligibility = Column(String(40), nullable=False, default="retention_active")  # DeletionEligibility
    locked      = Column(Boolean, nullable=False, default=False)  # frozen once case completes

    assigned_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    created_at  = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at  = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("case_id", name="uq_retention_case"),
        Index("ix_retention_tenant_eligibility", "tenant_id", "deletion_eligibility"),
    )


# ════════════════════════════════════════════════════════════════════════════
# 6. LEGAL HOLD  — hard block on deletion / anonymisation
# ════════════════════════════════════════════════════════════════════════════
class LegalHold(Base):
    __tablename__ = "legal_holds"

    id          = Column(String(40), primary_key=True)        # "hold_<uuid>"
    tenant_id   = Column(String(80), nullable=False, default=DEFAULT_TENANT, index=True)
    # Holds can be case-scoped or worker-scoped (org-wide matter). case_id may
    # be NULL for a standing hold attached only to a worker.
    case_id     = Column(String(40), ForeignKey("offboarding_cases.id", ondelete="SET NULL"),
                         nullable=True, index=True)
    worker_id   = Column(Integer, nullable=True, index=True)

    status      = Column(String(20), nullable=False, default="active", index=True)  # active|released
    matter_ref  = Column(String(120), nullable=True)

    # ── Sensitive, field-level protected (§4) ───────────────────────────────
    reason      = Column(Text, nullable=True)
    notes       = Column(Text, nullable=True)

    applied_by_id  = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    released_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    applied_at  = Column(DateTime(timezone=True), default=datetime.utcnow)
    released_at = Column(DateTime(timezone=True), nullable=True)
    review_due_at = Column(DateTime(timezone=True), nullable=True)

    created_at  = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at  = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_legalhold_tenant_status", "tenant_id", "status"),
    )


# ════════════════════════════════════════════════════════════════════════════
# 7. COMPLETION CERTIFICATE  — one signed certificate per completed case
# ════════════════════════════════════════════════════════════════════════════
class CompletionCertificate(Base):
    __tablename__ = "completion_certificates"

    id          = Column(String(40), primary_key=True)        # "cert_<uuid>"
    tenant_id   = Column(String(80), nullable=False, default=DEFAULT_TENANT, index=True)
    case_id     = Column(String(40), ForeignKey("offboarding_cases.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    worker_id   = Column(Integer, nullable=True, index=True)

    certificate_number = Column(String(60), nullable=False, unique=True, index=True)
    content_hash       = Column(String(64), nullable=False)   # SHA-256 of the readiness snapshot
    evidence_package_id = Column(String(40), nullable=True)

    issued_at   = Column(DateTime(timezone=True), default=datetime.utcnow, index=True)
    issued_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    payload     = Column(JSON, nullable=False, default=dict)   # readiness snapshot at issuance

    created_at  = Column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("case_id", name="uq_certificate_case"),
    )


# ════════════════════════════════════════════════════════════════════════════
# 8. EVIDENCE PACKAGE  — bundled evidence (access closure, payroll, billing…)
# ════════════════════════════════════════════════════════════════════════════
class EvidencePackage(Base):
    __tablename__ = "evidence_packages"

    id          = Column(String(40), primary_key=True)        # "evd_<uuid>"
    tenant_id   = Column(String(80), nullable=False, default=DEFAULT_TENANT, index=True)
    case_id     = Column(String(40), ForeignKey("offboarding_cases.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    worker_id   = Column(Integer, nullable=True, index=True)

    package_type = Column(String(40), nullable=False, index=True)  # EvidencePackageType
    content_hash = Column(String(64), nullable=False)
    storage_ref  = Column(String(255), nullable=True)              # bucket key / URI
    manifest     = Column(JSON, nullable=False, default=dict)      # items + per-item hashes

    generated_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at   = Column(DateTime(timezone=True), default=datetime.utcnow, index=True)


# ════════════════════════════════════════════════════════════════════════════
# 9. AUDIT EVENT  — append-only, hash-chained immutable audit trail
# No updated_at and no update/delete methods anywhere. Each row's current_hash
# = SHA-256(prev_hash + canonical row body), chaining per (tenant, case) so any
# tampering with a historical row breaks every subsequent hash.
# ════════════════════════════════════════════════════════════════════════════
class AuditEvent(Base):
    __tablename__ = "offboarding_audit_events"

    id          = Column(String(40), primary_key=True)        # "aud_<uuid>"
    tenant_id   = Column(String(80), nullable=False, default=DEFAULT_TENANT, index=True)
    case_id     = Column(String(40), nullable=True, index=True)
    worker_id   = Column(Integer, nullable=True, index=True)
    engagement_id = Column(String(80), nullable=True, index=True)

    # Monotonic position within this (tenant, case) hash chain. Assigned at
    # append time = previous seq + 1. Drives deterministic chain ordering and
    # is folded into the hash, so it survives a tz-lossy SQLite round-trip the
    # way a timestamp would not.
    seq         = Column(Integer, nullable=False, default=0, index=True)

    actor_id    = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    actor_role  = Column(String(40), nullable=True)
    action      = Column(String(80), nullable=False, index=True)
    entity      = Column(String(60), nullable=False)
    payload     = Column(JSON, nullable=True, default=dict)

    prev_hash   = Column(String(64), nullable=True)
    current_hash = Column(String(64), nullable=False)

    correlation_id = Column(String(40), nullable=True, index=True)
    causation_id   = Column(String(40), nullable=True)
    ip_address  = Column(String(45), nullable=True)
    source_service = Column(String(40), nullable=False, default="offboarding")

    created_at  = Column(DateTime(timezone=True), default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("ix_offb_audit_tenant_case", "tenant_id", "case_id"),
        Index("ix_offb_audit_action_created", "action", "created_at"),
    )


# ════════════════════════════════════════════════════════════════════════════
# 10. DOMAIN EVENT  — append-only, idempotent, replay-safe event log
# Doubles as a transactional outbox: rows are written in the same transaction
# as the state change, then a dispatcher publishes pending rows.
# ════════════════════════════════════════════════════════════════════════════
class DomainEvent(Base):
    __tablename__ = "offboarding_domain_events"

    id          = Column(String(40), primary_key=True)        # "evt_<uuid>"
    tenant_id   = Column(String(80), nullable=False, default=DEFAULT_TENANT, index=True)
    case_id     = Column(String(40), nullable=True, index=True)
    aggregate_id = Column(String(40), nullable=True, index=True)

    event_type  = Column(String(80), nullable=False, index=True)  # EventType
    payload     = Column(JSON, nullable=False, default=dict)

    correlation_id = Column(String(40), nullable=True, index=True)
    causation_id   = Column(String(40), nullable=True)
    # Unique → replays of the same logical event are dropped (§6 idempotency).
    idempotency_key = Column(String(64), nullable=False, unique=True, index=True)

    state_from  = Column(String(40), nullable=True)
    state_to    = Column(String(40), nullable=True)

    status      = Column(String(20), nullable=False, default="pending", index=True)  # pending|published|failed
    attempts    = Column(Integer, nullable=False, default=0)
    error       = Column(Text, nullable=True)

    occurred_at = Column(DateTime(timezone=True), default=datetime.utcnow, index=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    created_at  = Column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        Index("ix_offb_event_corr_type", "correlation_id", "event_type"),
        Index("ix_offb_event_tenant_status", "tenant_id", "status"),
    )


# ════════════════════════════════════════════════════════════════════════════
# 11. WORKFLOW BLOCKER  — a condition stopping a task/case from progressing
# ════════════════════════════════════════════════════════════════════════════
class WorkflowBlocker(Base):
    __tablename__ = "offboarding_workflow_blockers"

    id          = Column(String(40), primary_key=True)        # "blk_<uuid>"
    tenant_id   = Column(String(80), nullable=False, default=DEFAULT_TENANT, index=True)
    case_id     = Column(String(40), ForeignKey("offboarding_cases.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    task_id     = Column(String(40), nullable=True, index=True)

    blocker_type = Column(String(40), nullable=False)         # access_revocation_failed | legal_hold | payroll_open | ...
    severity    = Column(String(20), nullable=False, default="high")
    reason      = Column(Text, nullable=True)
    status      = Column(String(20), nullable=False, default="open", index=True)  # open|resolved|waived

    raised_by_id   = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    resolved_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    waiver_id   = Column(String(40), nullable=True)

    created_at  = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at  = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_blocker_case_status", "case_id", "status"),
    )


# ════════════════════════════════════════════════════════════════════════════
# 12. WORKFLOW WAIVER  — an audited, approved exception to a blocker/task
# ════════════════════════════════════════════════════════════════════════════
class WorkflowWaiver(Base):
    __tablename__ = "offboarding_workflow_waivers"

    id          = Column(String(40), primary_key=True)        # "wvr_<uuid>"
    tenant_id   = Column(String(80), nullable=False, default=DEFAULT_TENANT, index=True)
    case_id     = Column(String(40), ForeignKey("offboarding_cases.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    task_id     = Column(String(40), nullable=True)
    blocker_id  = Column(String(40), nullable=True)

    reason        = Column(Text, nullable=False)
    justification = Column(Text, nullable=True)
    status        = Column(String(20), nullable=False, default="active", index=True)  # active|revoked

    waived_by_id   = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    created_at  = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at  = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_waiver_case_status", "case_id", "status"),
    )


# ════════════════════════════════════════════════════════════════════════════
# 13. INTEGRATION RECONCILIATION QUEUE  — retry/dead-letter for external revokes
# When an external identity surface (SSO/SCIM/CRM) can't be revoked synchronously
# we enqueue it here; an async job reconciles with bounded retries.
# ════════════════════════════════════════════════════════════════════════════
class IntegrationReconciliationItem(Base):
    __tablename__ = "integration_reconciliation_queue"

    id          = Column(String(40), primary_key=True)        # "rcn_<uuid>"
    tenant_id   = Column(String(80), nullable=False, default=DEFAULT_TENANT, index=True)
    case_id     = Column(String(40), nullable=False, index=True)
    worker_id   = Column(Integer, nullable=True, index=True)

    integration = Column(String(40), nullable=False, index=True)  # sso | scim | crm | billing | ...
    component   = Column(String(40), nullable=True)               # RevocationComponent it satisfies
    operation   = Column(String(20), nullable=False, default="revoke")  # revoke | restore

    status      = Column(String(20), nullable=False, default="pending", index=True)  # pending|in_progress|succeeded|failed|dead_letter
    attempts    = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=5)
    last_error  = Column(Text, nullable=True)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True, index=True)

    payload     = Column(JSON, nullable=True, default=dict)
    correlation_id = Column(String(40), nullable=True, index=True)

    created_at  = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at  = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_recon_tenant_status", "tenant_id", "status"),
    )


# ════════════════════════════════════════════════════════════════════════════
# 14. SECURITY INCIDENT  — graded, escalatable failure record (§6)
# Opened by the enforcement layer whenever an invariant is violated or a control
# fails: a revoked worker is detected still active, telemetry arrives post-exit,
# a reconciliation conflict surfaces, a cross-tenant access is attempted, etc.
# Severity drives escalation; `reference` is the human-quotable incident id.
# `diagnostics` is sanitised — it never holds tokens, passwords or PII beyond
# the ids already present elsewhere in the audit trail (§6 safe diagnostics).
# ════════════════════════════════════════════════════════════════════════════
class SecurityIncident(Base):
    __tablename__ = "offboarding_security_incidents"

    id          = Column(String(40), primary_key=True)        # "inc_<uuid>"
    tenant_id   = Column(String(80), nullable=False, default=DEFAULT_TENANT, index=True)
    reference   = Column(String(40), nullable=False, unique=True, index=True)  # "INC-..."

    case_id     = Column(String(40), nullable=True, index=True)
    worker_id   = Column(Integer, nullable=True, index=True)

    incident_type = Column(String(60), nullable=False, index=True)   # AlertType / conflict type
    severity    = Column(String(20), nullable=False, default="high", index=True)  # IncidentSeverity
    status      = Column(String(20), nullable=False, default="open", index=True)  # IncidentStatus

    title       = Column(String(200), nullable=False)
    diagnostics = Column(JSON, nullable=False, default=dict)     # SAFE only — no secrets/PII

    occurrences = Column(Integer, nullable=False, default=1)     # dedupe counter
    retry_count = Column(Integer, nullable=False, default=0)

    escalated_at = Column(DateTime(timezone=True), nullable=True)
    resolved_at  = Column(DateTime(timezone=True), nullable=True)
    resolved_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    resolution_note = Column(Text, nullable=True)

    correlation_id = Column(String(40), nullable=True, index=True)
    last_seen_at   = Column(DateTime(timezone=True), default=datetime.utcnow, index=True)

    created_at  = Column(DateTime(timezone=True), default=datetime.utcnow, index=True)
    updated_at  = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_incident_tenant_status", "tenant_id", "status"),
        Index("ix_incident_tenant_severity", "tenant_id", "severity"),
        # One OPEN incident per (tenant, type, worker) at a time — re-detecting
        # the same condition bumps occurrences instead of spamming new rows.
        Index("ix_incident_dedupe", "tenant_id", "incident_type", "worker_id", "status"),
    )


# ════════════════════════════════════════════════════════════════════════════
# 15. TELEMETRY QUARANTINE RECORD  — rejected post-exit ingest (§2)
# When the enforcement gate denies a telemetry write (screenshot / activity /
# heartbeat / idle / url / productivity / late offline upload) for an offboarded
# worker, the payload metadata is parked here and NEVER written to the live
# activities / screenshots tables. Append-only evidence that the stop was
# enforced server-side, regardless of what the client did.
# ════════════════════════════════════════════════════════════════════════════
class TelemetryQuarantineRecord(Base):
    __tablename__ = "offboarding_telemetry_quarantine"

    id          = Column(String(40), primary_key=True)        # "tqr_<uuid>"
    tenant_id   = Column(String(80), nullable=False, default=DEFAULT_TENANT, index=True)
    worker_id   = Column(Integer, nullable=True, index=True)
    case_id     = Column(String(40), nullable=True, index=True)

    telemetry_kind = Column(String(40), nullable=False, index=True)  # TelemetryKind
    reason      = Column(String(60), nullable=False)                 # QuarantineReason
    status      = Column(String(20), nullable=False, default="quarantined", index=True)  # QuarantineStatus

    # Sanitised metadata only — app/window names, counts, client timestamp,
    # source IP. NEVER the screenshot bytes or any credential.
    payload_meta = Column(JSON, nullable=False, default=dict)
    client_timestamp = Column(DateTime(timezone=True), nullable=True)
    source_ip   = Column(String(45), nullable=True)
    incident_id = Column(String(40), nullable=True, index=True)

    created_at  = Column(DateTime(timezone=True), default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("ix_quarantine_tenant_worker", "tenant_id", "worker_id"),
        Index("ix_quarantine_tenant_kind", "tenant_id", "telemetry_kind"),
    )


# ════════════════════════════════════════════════════════════════════════════
# 16. FORMER-WORKER PORTAL SESSION  — isolated bearer for the §1 portal
# Distinct from active-worker sessions/refresh-tokens (which are revoked at
# exit). Lives a short TTL (PORTAL_SESSION_TTL_SECONDS) and requires MFA to
# move from PENDING_MFA → ACTIVE. ``token_hash`` is SHA-256 of the raw bearer
# the portal issues — the raw value never leaves the issuing response.
# ════════════════════════════════════════════════════════════════════════════
class FormerWorkerPortalSession(Base):
    __tablename__ = "offboarding_portal_sessions"

    id          = Column(String(40), primary_key=True)        # "psn_<uuid>"
    tenant_id   = Column(String(80), nullable=False, default=DEFAULT_TENANT, index=True)
    fwa_id      = Column(String(40), ForeignKey("former_worker_access.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    worker_id   = Column(Integer, nullable=True, index=True)

    token_hash  = Column(String(64), nullable=False, unique=True, index=True)
    status      = Column(String(20), nullable=False, default="pending_mfa", index=True)  # PortalSessionStatus

    # OTP challenge fields (used while status == pending_mfa). otp_hash is
    # SHA-256 of the dispatched code; never the plaintext.
    otp_hash       = Column(String(64), nullable=True)
    otp_expires_at = Column(DateTime(timezone=True), nullable=True)
    mfa_attempts   = Column(Integer, nullable=False, default=0)
    mfa_passed     = Column(Boolean, nullable=False, default=False)

    issued_at     = Column(DateTime(timezone=True), default=datetime.utcnow, index=True)
    expires_at    = Column(DateTime(timezone=True), nullable=False, index=True)
    last_active_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    revoked_at    = Column(DateTime(timezone=True), nullable=True)
    revoked_reason = Column(String(80), nullable=True)

    ip_address  = Column(String(45), nullable=True)
    user_agent  = Column(String(512), nullable=True)

    created_at  = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at  = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_portal_session_tenant_status", "tenant_id", "status"),
        Index("ix_portal_session_fwa_status", "fwa_id", "status"),
    )


# ════════════════════════════════════════════════════════════════════════════
# 17. FORMER-WORKER MFA  — alternate verified email + enrollment state (§2)
# One row per FormerWorkerAccess. The alternate email is the channel the OTP
# is dispatched to; it must be re-verified before MFA is considered ENROLLED.
# ════════════════════════════════════════════════════════════════════════════
class FormerWorkerMFA(Base):
    __tablename__ = "offboarding_portal_mfa"

    id          = Column(String(40), primary_key=True)        # "mfa_<uuid>"
    tenant_id   = Column(String(80), nullable=False, default=DEFAULT_TENANT, index=True)
    fwa_id      = Column(String(40), ForeignKey("former_worker_access.id", ondelete="CASCADE"),
                         nullable=False, unique=True, index=True)
    worker_id   = Column(Integer, nullable=True, index=True)

    method      = Column(String(20), nullable=False, default="email_otp")  # MFAMethod
    status      = Column(String(20), nullable=False, default="not_enrolled", index=True)  # MFAStatus

    alternate_email          = Column(String(255), nullable=True)
    alternate_email_verified = Column(Boolean, nullable=False, default=False)

    # SHA-256 of a one-time recovery code, surfaced once to the worker at
    # enrollment time. Never persisted in plaintext.
    recovery_code_hash = Column(String(64), nullable=True)

    last_verified_at = Column(DateTime(timezone=True), nullable=True)

    # Transient enrollment-OTP fields (used while status == pending_verify).
    enrollment_otp_hash       = Column(String(64), nullable=True)
    enrollment_otp_expires_at = Column(DateTime(timezone=True), nullable=True)
    enrollment_attempts       = Column(Integer, nullable=False, default=0)

    created_at  = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at  = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)


# ════════════════════════════════════════════════════════════════════════════
# 18. PORTAL ACCESS LOG  — append-only portal-side audit (§1, §9)
# Every portal action (login attempt, MFA, list, download, support request,
# DSAR, abnormal-activity trip) lands here, scoped to the FormerWorkerAccess
# grant. The hash-chained ``offboarding_audit_events`` table records the
# governance-side mirror; this table is the worker-facing access log shown in
# the §1 "expiry visibility" view.
# ════════════════════════════════════════════════════════════════════════════
class PortalAccessLog(Base):
    __tablename__ = "offboarding_portal_access_log"

    id          = Column(String(40), primary_key=True)        # "pal_<uuid>"
    tenant_id   = Column(String(80), nullable=False, default=DEFAULT_TENANT, index=True)
    fwa_id      = Column(String(40), ForeignKey("former_worker_access.id", ondelete="CASCADE"),
                         nullable=True, index=True)
    worker_id   = Column(Integer, nullable=True, index=True)
    session_id  = Column(String(40), nullable=True, index=True)

    action      = Column(String(40), nullable=False, index=True)   # PortalAction
    outcome     = Column(String(20), nullable=False, default="ok")  # ok | denied | throttled | failed

    scope       = Column(String(80), nullable=True)
    document_ref = Column(String(120), nullable=True)
    detail      = Column(JSON, nullable=False, default=dict)

    ip_address  = Column(String(45), nullable=True)
    user_agent  = Column(String(512), nullable=True)

    occurred_at = Column(DateTime(timezone=True), default=datetime.utcnow, index=True)
    # No updated_at: append-only.

    __table_args__ = (
        Index("ix_portal_log_tenant_action", "tenant_id", "action"),
        Index("ix_portal_log_fwa_action", "fwa_id", "action"),
    )


# ════════════════════════════════════════════════════════════════════════════
# 19. RETENTION POLICY  — versioned, jurisdiction-aware policy definition (§5)
# RetentionAssignment (table 5) references a policy_code; this table is the
# library of those codes, versioned so a policy update never silently moves the
# deletion clock on already-assigned cases. Activating a new version supersedes
# the prior one (``superseded_by``); RetentionAssignment keeps its frozen copy.
# ════════════════════════════════════════════════════════════════════════════
class RetentionPolicy(Base):
    __tablename__ = "offboarding_retention_policies"

    id          = Column(String(40), primary_key=True)        # "pol_<uuid>"
    tenant_id   = Column(String(80), nullable=False, default=DEFAULT_TENANT, index=True)

    code         = Column(String(60), nullable=False, index=True)   # RetentionPolicyCode
    version      = Column(Integer, nullable=False, default=1)
    jurisdiction = Column(String(40), nullable=True, index=True)
    data_classification = Column(String(40), nullable=True)

    retention_days = Column(Integer, nullable=False)
    active       = Column(Boolean, nullable=False, default=True, index=True)
    description  = Column(Text, nullable=True)

    superseded_by = Column(String(40), nullable=True)
    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    created_at  = Column(DateTime(timezone=True), default=datetime.utcnow, index=True)
    updated_at  = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("tenant_id", "code", "version", name="uq_retention_policy_code_version"),
        Index("ix_retention_policy_tenant_active", "tenant_id", "active"),
    )


# ════════════════════════════════════════════════════════════════════════════
# 20. DELETION ELIGIBILITY REVIEW  — dual approval before any data deletion (§4)
# Captures the gating decision required by the spec: deletion / anonymisation
# may only proceed once BOTH a Legal approver AND a Compliance approver sign
# off, AND no legal hold / open dispute / active retention is in the way. The
# computed blockers are snapshotted at review time so the approval cannot
# silently outlive the conditions it was made under.
# ════════════════════════════════════════════════════════════════════════════
class DeletionEligibilityReview(Base):
    __tablename__ = "offboarding_deletion_reviews"

    id          = Column(String(40), primary_key=True)        # "der_<uuid>"
    tenant_id   = Column(String(80), nullable=False, default=DEFAULT_TENANT, index=True)
    case_id     = Column(String(40), ForeignKey("offboarding_cases.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    worker_id   = Column(Integer, nullable=True, index=True)

    status      = Column(String(20), nullable=False, default="pending", index=True)  # DeletionReviewStatus
    scope       = Column(String(60), nullable=False, default="full_record")  # what to delete/anonymise

    requested_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    request_notes   = Column(Text, nullable=True)

    legal_approver_id      = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    legal_approved_at      = Column(DateTime(timezone=True), nullable=True)
    legal_notes            = Column(Text, nullable=True)

    compliance_approver_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    compliance_approved_at = Column(DateTime(timezone=True), nullable=True)
    compliance_notes       = Column(Text, nullable=True)

    denied_by_id  = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    denied_at     = Column(DateTime(timezone=True), nullable=True)
    denial_reason = Column(Text, nullable=True)

    blockers_snapshot = Column(JSON, nullable=False, default=list)   # [{type, detail}, ...]
    decided_at  = Column(DateTime(timezone=True), nullable=True)
    executed_at = Column(DateTime(timezone=True), nullable=True)

    correlation_id = Column(String(40), nullable=True, index=True)

    created_at  = Column(DateTime(timezone=True), default=datetime.utcnow, index=True)
    updated_at  = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_deletion_review_tenant_status", "tenant_id", "status"),
    )


# ════════════════════════════════════════════════════════════════════════════
# 21. SUPPORT TICKET  — former-worker-opened support request (§1 allowed list)
# Strictly portal-bounded: a former worker may OPEN a ticket; reading/resolving
# happens through the existing support tooling. Body is text only; no
# attachments at this layer.
# ════════════════════════════════════════════════════════════════════════════
class FormerWorkerSupportTicket(Base):
    __tablename__ = "offboarding_support_tickets"

    id          = Column(String(40), primary_key=True)        # "tic_<uuid>"
    tenant_id   = Column(String(80), nullable=False, default=DEFAULT_TENANT, index=True)
    fwa_id      = Column(String(40), ForeignKey("former_worker_access.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    worker_id   = Column(Integer, nullable=True, index=True)

    subject     = Column(String(255), nullable=False)
    body        = Column(Text, nullable=False)
    category    = Column(String(40), nullable=True)
    status      = Column(String(20), nullable=False, default="open", index=True)  # SupportTicketStatus

    resolved_by_id  = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    resolution_note = Column(Text, nullable=True)

    created_at  = Column(DateTime(timezone=True), default=datetime.utcnow, index=True)
    updated_at  = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)


# ════════════════════════════════════════════════════════════════════════════
# 22. DATA-RIGHTS REQUEST  — GDPR-style access / erasure / portability (§1)
# Filed from the portal; gated by legal-hold + retention before fulfilment.
# Status moves through under_review → fulfilled / denied; reasons captured.
# ════════════════════════════════════════════════════════════════════════════
class DataRightsRequest(Base):
    __tablename__ = "offboarding_data_rights_requests"

    id          = Column(String(40), primary_key=True)        # "drq_<uuid>"
    tenant_id   = Column(String(80), nullable=False, default=DEFAULT_TENANT, index=True)
    fwa_id      = Column(String(40), ForeignKey("former_worker_access.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    worker_id   = Column(Integer, nullable=True, index=True)
    case_id     = Column(String(40), nullable=True, index=True)

    request_type = Column(String(20), nullable=False, index=True)   # DataRightsRequestType
    status       = Column(String(20), nullable=False, default="open", index=True)  # DataRightsRequestStatus
    details      = Column(Text, nullable=True)

    sla_due_at   = Column(DateTime(timezone=True), nullable=True, index=True)
    fulfilled_at = Column(DateTime(timezone=True), nullable=True)
    denied_at    = Column(DateTime(timezone=True), nullable=True)
    denial_reason = Column(Text, nullable=True)
    handled_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    created_at  = Column(DateTime(timezone=True), default=datetime.utcnow, index=True)
    updated_at  = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)


# Convenience: the full ordered list of model classes, consumed by
# bootstrap.ensure_tables() and the test schema fixture.
ALL_MODELS = [
    OffboardingCase, OffboardingTask, AccessRevocationRecord, FormerWorkerAccess,
    RetentionAssignment, LegalHold, CompletionCertificate, EvidencePackage,
    AuditEvent, DomainEvent, WorkflowBlocker, WorkflowWaiver,
    IntegrationReconciliationItem,
    SecurityIncident, TelemetryQuarantineRecord,
    FormerWorkerPortalSession, FormerWorkerMFA, PortalAccessLog,
    RetentionPolicy, DeletionEligibilityReview,
    FormerWorkerSupportTicket, DataRightsRequest,
]

# The subset added by the runtime-enforcement layer (§1-§8). Referenced by the
# dedicated alembic migration so it can create exactly these without re-touching
# the original 13-table lifecycle migration.
SECURITY_MODELS = [SecurityIncident, TelemetryQuarantineRecord]

# The subset added by the former-worker lifecycle governance layer (§1-§9 of
# the governance spec). Its own alembic migration creates exactly these.
GOVERNANCE_MODELS = [
    FormerWorkerPortalSession, FormerWorkerMFA, PortalAccessLog,
    RetentionPolicy, DeletionEligibilityReview,
    FormerWorkerSupportTicket, DataRightsRequest,
]

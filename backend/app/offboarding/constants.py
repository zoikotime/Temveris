"""
Offboarding domain constants — the single source of truth for every enum
the lifecycle uses. Strings (not native PG enums) so we can evolve the
vocabulary without an ALTER TYPE migration on Supabase, matching the
String-status convention used elsewhere in the codebase (LeaveStatus,
BillingSubscription.subscription_status, etc.).
"""
from __future__ import annotations

import enum


# ──────────────────────────────────────────────────────────────────────────────
# CASE STATE MACHINE
# The exact 15 states from the spec. Order here is roughly the happy-path
# progression; the authoritative transition rules live in state_machine.py.
# ──────────────────────────────────────────────────────────────────────────────
class CaseState(str, enum.Enum):
    DRAFT                   = "draft"
    PENDING_APPROVAL        = "pending_approval"
    APPROVED                = "approved"
    SCHEDULED               = "scheduled"
    IN_PROGRESS             = "in_progress"
    ACCESS_CLOSURE_PENDING  = "access_closure_pending"
    ACCESS_REVOCATION_FAILED = "access_revocation_failed"
    FINAL_REVIEW_PENDING    = "final_review_pending"
    PAYROLL_CLOSURE_PENDING = "payroll_closure_pending"
    BILLING_CLOSURE_PENDING = "billing_closure_pending"
    LEGAL_HOLD_REVIEW_PENDING = "legal_hold_review_pending"
    FORMER_ACCESS_PENDING   = "former_access_pending"
    BLOCKED                 = "blocked"
    COMPLETED               = "completed"
    REVERSED                = "reversed"
    CANCELLED               = "cancelled"


# Terminal states — no outbound transitions except where explicitly allowed.
TERMINAL_STATES = frozenset({CaseState.COMPLETED, CaseState.CANCELLED})


class ExitType(str, enum.Enum):
    VOLUNTARY        = "voluntary"          # resignation
    INVOLUNTARY      = "involuntary"        # termination for cause
    REDUNDANCY       = "redundancy"         # layoff / role elimination
    END_OF_CONTRACT  = "end_of_contract"    # contractor / fixed-term
    RETIREMENT       = "retirement"
    DECEASED         = "deceased"


class RevocationMode(str, enum.Enum):
    IMMEDIATE = "immediate"     # revoke the moment the case is approved
    SCHEDULED = "scheduled"     # revoke at final_access_time
    GRACEFUL  = "graceful"      # revoke at final_access_time + grace window


# ──────────────────────────────────────────────────────────────────────────────
# ACCESS REVOCATION COMPONENTS
# Each is an independently-tracked target so a partial failure surfaces exactly
# which surface is still open instead of a single opaque "revocation failed".
# ──────────────────────────────────────────────────────────────────────────────
class RevocationComponent(str, enum.Enum):
    LOGIN_ACCESS        = "login_access"
    ACTIVE_SESSIONS     = "active_sessions"
    REFRESH_TOKENS      = "refresh_tokens"
    API_TOKENS          = "api_tokens"
    DESKTOP_AUTH        = "desktop_authorization"
    MOBILE_AUTH         = "mobile_authorization"
    MFA_TRUSTED_DEVICES = "mfa_trusted_devices"
    SSO_SESSIONS        = "sso_sessions"
    SCIM_STATE          = "scim_state"
    CRM_ACCESS          = "crm_access"
    BILLING_ADMIN       = "billing_admin_access"
    TEAM_MEMBERSHIPS    = "team_memberships"
    MANAGER_ADMIN_SCOPE = "manager_admin_scopes"
    INTEGRATION_TOKENS  = "integration_tokens"


class RevocationStatus(str, enum.Enum):
    PENDING   = "pending"
    REVOKED   = "revoked"
    FAILED    = "failed"
    NOT_APPLICABLE = "not_applicable"   # surface user never had (e.g. no SCIM)
    REVERSED  = "reversed"              # restored on case reversal


# ──────────────────────────────────────────────────────────────────────────────
# WORKFLOW TASKS
# ──────────────────────────────────────────────────────────────────────────────
class TaskType(str, enum.Enum):
    FINAL_WORK_REVIEW       = "final_work_review"
    PAYROLL_CLOSURE         = "payroll_closure"
    BILLING_CLOSURE         = "billing_closure"
    RETENTION_ASSIGNMENT    = "retention_assignment"
    LEGAL_HOLD_REVIEW       = "legal_hold_review"
    FORMER_WORKER_SETUP     = "former_worker_setup"
    ACCESS_REVOCATION       = "access_revocation"
    TRACKING_STOP_VERIFY    = "tracking_stop_verification"
    CERTIFICATE_GENERATION  = "certificate_generation"


class TaskStatus(str, enum.Enum):
    PENDING    = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED    = "blocked"
    WAIVED     = "waived"
    COMPLETED  = "completed"
    FAILED     = "failed"


# ──────────────────────────────────────────────────────────────────────────────
# DOMAIN EVENTS  (event-driven lifecycle)
# ──────────────────────────────────────────────────────────────────────────────
class EventType(str, enum.Enum):
    CASE_CREATED              = "OffboardingCaseCreated"
    CASE_SUBMITTED            = "OffboardingCaseSubmitted"
    CASE_APPROVED             = "OffboardingCaseApproved"
    CASE_SCHEDULED            = "OffboardingCaseScheduled"
    FINAL_ACCESS_TIME_REACHED = "FinalAccessTimeReached"
    ACCESS_REVOCATION_STARTED = "AccessRevocationStarted"
    ACCESS_REVOCATION_COMPLETED = "AccessRevocationCompleted"
    ACCESS_REVOCATION_FAILED  = "AccessRevocationFailed"
    TRACKING_STOPPED          = "TrackingStopped"
    POST_EXIT_TRACKING_DETECTED = "PostExitTrackingDetected"
    POST_EXIT_TELEMETRY_QUARANTINED = "PostExitTelemetryQuarantined"
    POST_EXIT_ACCESS_BLOCKED  = "PostExitAccessBlocked"
    LEGAL_HOLD_APPLIED        = "LegalHoldApplied"
    LEGAL_HOLD_RELEASED       = "LegalHoldReleased"
    RETENTION_POLICY_ASSIGNED = "RetentionPolicyAssigned"
    PAYROLL_CLOSED            = "PayrollClosureCompleted"
    BILLING_CLOSED            = "BillingClosureCompleted"
    COMPLETION_CERTIFICATE_GENERATED = "CompletionCertificateGenerated"
    CASE_COMPLETED            = "OffboardingCaseCompleted"
    CASE_REVERSED             = "OffboardingCaseReversed"
    CASE_CANCELLED            = "OffboardingCaseCancelled"
    FORMER_WORKER_ACCESS_GRANTED = "FormerWorkerAccessGranted"
    FORMER_WORKER_ACCESS_EXPIRED = "FormerWorkerAccessExpired"
    FORMER_WORKER_ACCESS_REVOKED = "FormerWorkerAccessRevoked"
    # ── Enforcement layer (server-side runtime) ─────────────────────────────
    REVOCATION_RETRIED        = "AccessRevocationRetried"
    RECONCILIATION_ENQUEUED   = "ReconciliationEnqueued"
    RECONCILIATION_SUCCEEDED  = "ReconciliationSucceeded"
    RECONCILIATION_DEAD_LETTER = "ReconciliationDeadLettered"
    IDENTITY_CONFLICT_DETECTED = "IdentityConflictDetected"
    SECURITY_INCIDENT_OPENED  = "SecurityIncidentOpened"
    SECURITY_INCIDENT_ESCALATED = "SecurityIncidentEscalated"
    SECURITY_INCIDENT_RESOLVED = "SecurityIncidentResolved"


# ──────────────────────────────────────────────────────────────────────────────
# RUNTIME ENFORCEMENT  (server-side access + telemetry gate)
# ──────────────────────────────────────────────────────────────────────────────
# Case states in which the worker's *active* access and tracking MUST already be
# closed. The gate denies any login / API call / telemetry ingest for a worker
# whose case sits in one of these states — the "zero post-exit access / zero
# post-exit tracking" doctrine, enforced from server-side lifecycle state and
# never from client logout. Reversed/cancelled cases are deliberately absent:
# reversal restores access.
ACCESS_CLOSED_STATES = frozenset({
    CaseState.ACCESS_CLOSURE_PENDING.value,
    CaseState.ACCESS_REVOCATION_FAILED.value,
    CaseState.FINAL_REVIEW_PENDING.value,
    CaseState.PAYROLL_CLOSURE_PENDING.value,
    CaseState.BILLING_CLOSURE_PENDING.value,
    CaseState.LEGAL_HOLD_REVIEW_PENDING.value,
    CaseState.FORMER_ACCESS_PENDING.value,
    CaseState.COMPLETED.value,
})

# States in which the worker is still pre-cutover: access remains open UNTIL the
# final_access_at instant passes, at which point the gate flips to deny even
# though access-closure tasks may not have run yet (a crashed/stale agent must
# not keep tracking past the cutover just because the closure job is late).
PENDING_CUTOVER_STATES = frozenset({
    CaseState.APPROVED.value,
    CaseState.SCHEDULED.value,
    CaseState.IN_PROGRESS.value,
})


class TelemetryKind(str, enum.Enum):
    """The ingest surface a blocked telemetry event arrived on (for evidence)."""
    SCREENSHOT          = "screenshot"
    ACTIVITY            = "activity"
    HEARTBEAT           = "heartbeat"
    IDLE                = "idle"
    URL                 = "url"
    PRODUCTIVITY        = "productivity"
    OFFLINE_UPLOAD      = "offline_upload"


class QuarantineReason(str, enum.Enum):
    POST_EXIT_ACCESS_CLOSED = "post_exit_access_closed"   # case past access closure
    PAST_FINAL_ACCESS       = "past_final_access_at"       # cutover instant elapsed
    OFFLINE_BACKFILL        = "offline_backfill_after_exit"  # late offline upload


class QuarantineStatus(str, enum.Enum):
    QUARANTINED = "quarantined"   # held, never written to the live tracking tables
    REVIEWED    = "reviewed"
    PURGED      = "purged"


# ──────────────────────────────────────────────────────────────────────────────
# SSO / SCIM RECONCILIATION
# ──────────────────────────────────────────────────────────────────────────────
class ReconciliationStatus(str, enum.Enum):
    PENDING     = "pending"
    IN_PROGRESS = "in_progress"
    SUCCEEDED   = "succeeded"
    FAILED      = "failed"
    DEAD_LETTER = "dead_letter"   # exhausted retries — needs human intervention


class IdentityConflictType(str, enum.Enum):
    IDP_ACTIVE_ZOIKO_REVOKED = "idp_active_zoiko_revoked"   # IdP still grants, Temveris revoked
    SCIM_FAILURE             = "scim_deprovision_failure"
    STALE_SSO_STATE          = "stale_sso_state"             # SSO not torn down post-exit
    MULTIPLE_IDENTITIES      = "multiple_identities"         # duplicate accounts for a worker
    REHIRE_CONFLICT          = "rehire_conflict"             # reactivation while exit closed


# ──────────────────────────────────────────────────────────────────────────────
# ALERTS  (§18 monitoring)
# Defined here, above INCIDENT_SEVERITY / CONFLICT_SEVERITY, so those severity
# maps can reference AlertType without a forward-reference NameError at import.
# ──────────────────────────────────────────────────────────────────────────────
class AlertType(str, enum.Enum):
    TRACKING_AFTER_OFFBOARDING = "tracking_after_offboarding"
    TOKEN_STILL_ACTIVE         = "token_still_active"
    SESSION_REVOCATION_FAILURE = "session_revocation_failure"
    AUDIT_FAILURE              = "audit_failure"
    LEGAL_HOLD_DELETION_ATTEMPT = "legal_hold_deletion_attempt"
    RECONCILIATION_CONFLICT    = "reconciliation_conflict"
    CERTIFICATE_GENERATION_FAILURE = "certificate_generation_failure"
    CROSS_TENANT_ACCESS_ATTEMPT = "cross_tenant_access_attempt"
    FORMER_WORKER_ABUSE        = "former_worker_abuse"


# ──────────────────────────────────────────────────────────────────────────────
# INCIDENT & FAILURE MANAGEMENT
# ──────────────────────────────────────────────────────────────────────────────
class IncidentSeverity(str, enum.Enum):
    CRITICAL = "critical"
    HIGH     = "high"
    MEDIUM   = "medium"
    LOW      = "low"


class IncidentStatus(str, enum.Enum):
    OPEN       = "open"
    ESCALATED  = "escalated"
    RESOLVED   = "resolved"
    SUPPRESSED = "suppressed"   # acknowledged dedupe of a known-open incident


# Default severity for each alert/incident type. Critical = a Tier-0 invariant is
# actively being violated (a former worker is getting in, or telemetry is being
# accepted, or tenants are leaking). Those page immediately; lower severities
# queue for retry / review.
INCIDENT_SEVERITY: dict = {
    AlertType.TRACKING_AFTER_OFFBOARDING.value:  IncidentSeverity.CRITICAL.value,
    AlertType.TOKEN_STILL_ACTIVE.value:          IncidentSeverity.CRITICAL.value,
    AlertType.CROSS_TENANT_ACCESS_ATTEMPT.value: IncidentSeverity.CRITICAL.value,
    AlertType.FORMER_WORKER_ABUSE.value:         IncidentSeverity.CRITICAL.value,
    AlertType.SESSION_REVOCATION_FAILURE.value:  IncidentSeverity.HIGH.value,
    AlertType.RECONCILIATION_CONFLICT.value:     IncidentSeverity.HIGH.value,
    AlertType.CERTIFICATE_GENERATION_FAILURE.value: IncidentSeverity.MEDIUM.value,
    AlertType.AUDIT_FAILURE.value:               IncidentSeverity.HIGH.value,
    AlertType.LEGAL_HOLD_DELETION_ATTEMPT.value: IncidentSeverity.CRITICAL.value,
}

# Identity-conflict scenario → severity.
CONFLICT_SEVERITY: dict = {
    IdentityConflictType.IDP_ACTIVE_ZOIKO_REVOKED.value: IncidentSeverity.CRITICAL.value,
    IdentityConflictType.SCIM_FAILURE.value:             IncidentSeverity.HIGH.value,
    IdentityConflictType.STALE_SSO_STATE.value:          IncidentSeverity.HIGH.value,
    IdentityConflictType.MULTIPLE_IDENTITIES.value:      IncidentSeverity.MEDIUM.value,
    IdentityConflictType.REHIRE_CONFLICT.value:          IncidentSeverity.MEDIUM.value,
}

# Retry/backoff policy for the reconciliation queue (§5 background jobs).
RECONCILIATION_MAX_ATTEMPTS = 5
RECONCILIATION_BACKOFF_BASE_SECONDS = 60        # 1m, 2m, 4m, 8m, 16m
RECONCILIATION_BACKOFF_CAP_SECONDS = 3600       # never wait more than an hour


# ════════════════════════════════════════════════════════════════════════════
# FORMER-WORKER LIFECYCLE GOVERNANCE  (§1-§9 of the governance spec)
# ════════════════════════════════════════════════════════════════════════════

# ── Portal sessions (§2 MFA + Security) ─────────────────────────────────────
class PortalSessionStatus(str, enum.Enum):
    PENDING_MFA = "pending_mfa"   # token issued, awaiting OTP verification
    ACTIVE      = "active"        # MFA passed, bearer good until expiry
    EXPIRED     = "expired"
    REVOKED     = "revoked"


# Former-worker portal sessions deliberately expire FAR sooner than active-
# worker sessions: a former worker should re-prove identity on every visit.
PORTAL_SESSION_TTL_SECONDS         = 30 * 60      # 30 minutes once MFA passed
PORTAL_MFA_CHALLENGE_TTL_SECONDS   = 5 * 60       # 5 minutes to enter the OTP
PORTAL_OTP_LENGTH                  = 6
PORTAL_MAX_MFA_ATTEMPTS            = 5            # then session is killed

# Rate-limit + download-throttle defaults. In-memory; replace with a shared
# limiter (Redis) for multi-instance deployments.
PORTAL_LOGIN_WINDOW_SECONDS        = 15 * 60
PORTAL_LOGIN_MAX_PER_WINDOW        = 8
PORTAL_DOWNLOAD_WINDOW_SECONDS     = 60
PORTAL_DOWNLOAD_MAX_PER_WINDOW     = 10           # ten downloads/minute, hard cap
PORTAL_DAILY_DOWNLOAD_CAP          = 200          # twenty-four-hour cap per worker

# Abnormal-activity heuristics. Trips a HIGH SecurityIncident; the session is
# also revoked when the IP-rotation threshold fires (likely token sharing).
PORTAL_DISTINCT_IPS_PER_HOUR_LIMIT = 4
PORTAL_FAILED_MFA_BURST_LIMIT      = 5            # in PORTAL_LOGIN_WINDOW_SECONDS


class MFAMethod(str, enum.Enum):
    EMAIL_OTP = "email_otp"      # default — sent to alternate verified email
    TOTP      = "totp"           # authenticator-app code (future)


class MFAStatus(str, enum.Enum):
    NOT_ENROLLED   = "not_enrolled"
    PENDING_VERIFY = "pending_verify"   # alternate email captured, awaiting verify
    ENROLLED       = "enrolled"


# ── Document scope rules (§3) ───────────────────────────────────────────────
# Categories the former worker may NEVER reach, regardless of scope. These
# strings never appear in ALLOWED_FORMER_WORKER_SCOPES — but listing them here
# makes the engine's blocklist explicit and testable.
FORBIDDEN_FORMER_WORKER_SCOPES = frozenset({
    "dashboards", "analytics", "crm", "billing_admin", "admin_settings",
    "tracking_data", "audit_logs", "legal_hold_records",
    "internal_notes", "billing_evidence",
})


# ── Portal action audit (§1, §9) ────────────────────────────────────────────
class PortalAction(str, enum.Enum):
    LOGIN_ATTEMPT      = "login_attempt"
    LOGIN_SUCCEEDED    = "login_succeeded"
    MFA_SENT           = "mfa_sent"
    MFA_VERIFIED       = "mfa_verified"
    MFA_FAILED         = "mfa_failed"
    LOGOUT             = "logout"
    LIST_DOCUMENTS     = "list_documents"
    DOWNLOAD_DOCUMENT  = "download_document"
    DOWNLOAD_THROTTLED = "download_throttled"
    SUPPORT_REQUEST    = "support_request_opened"
    DSAR_REQUEST       = "data_rights_request_opened"
    SESSION_EXPIRED    = "session_expired"
    ABNORMAL_ACTIVITY  = "abnormal_activity"
    FORBIDDEN_SCOPE_ATTEMPT = "forbidden_scope_attempt"


# ── Retention engine (§5) ───────────────────────────────────────────────────
class RetentionPolicyCode(str, enum.Enum):
    DEFAULT          = "default_retention"
    GDPR_EU          = "gdpr_eu_retention"
    PAYROLL_TAX_US   = "payroll_tax_us_retention"
    PAYROLL_TAX_UK   = "payroll_tax_uk_retention"
    PAYROLL_TAX_IN   = "payroll_tax_in_retention"
    LITIGATION_HOLD  = "litigation_hold_retention"


# Jurisdiction → recommended default policy code. Used by the retention engine
# when a case has a jurisdiction but no explicitly-selected policy.
JURISDICTION_POLICY_DEFAULTS: dict = {
    "EU": RetentionPolicyCode.GDPR_EU.value,
    "DE": RetentionPolicyCode.GDPR_EU.value,
    "FR": RetentionPolicyCode.GDPR_EU.value,
    "US": RetentionPolicyCode.PAYROLL_TAX_US.value,
    "UK": RetentionPolicyCode.PAYROLL_TAX_UK.value,
    "GB": RetentionPolicyCode.PAYROLL_TAX_UK.value,
    "IN": RetentionPolicyCode.PAYROLL_TAX_IN.value,
}

# Seeded retention durations (days). The active policy version's
# retention_days is authoritative once seeded; this map is the bootstrap.
RETENTION_DAYS_DEFAULTS: dict = {
    RetentionPolicyCode.DEFAULT.value:         7 * 365,    # 7 years
    RetentionPolicyCode.GDPR_EU.value:         6 * 365,    # 6 years (GDPR floor)
    RetentionPolicyCode.PAYROLL_TAX_US.value:  7 * 365,    # IRS 7-year tax
    RetentionPolicyCode.PAYROLL_TAX_UK.value:  6 * 365,    # HMRC 6-year tax
    RetentionPolicyCode.PAYROLL_TAX_IN.value:  8 * 365,    # I-T Act 8-year
    RetentionPolicyCode.LITIGATION_HOLD.value: 99 * 365,   # effectively indefinite
}


# ── Deletion eligibility review (§4) ────────────────────────────────────────
class DeletionReviewStatus(str, enum.Enum):
    PENDING     = "pending"
    APPROVED    = "approved"   # both Legal + Compliance signed off
    DENIED      = "denied"
    EXECUTED    = "executed"   # the approved deletion/anonymisation has run
    CANCELLED   = "cancelled"


# Approvers required before a deletion / anonymisation may proceed. Both must
# be satisfied (legal + compliance) — separation of duties.
DELETION_REQUIRED_APPROVERS = ("legal", "compliance")


# ── Support tickets + data-rights requests (DSAR / erasure) (§1) ────────────
class SupportTicketStatus(str, enum.Enum):
    OPEN     = "open"
    RESOLVED = "resolved"
    CLOSED   = "closed"


class DataRightsRequestType(str, enum.Enum):
    ACCESS       = "access"          # GDPR Art 15 / DSAR
    ERASURE      = "erasure"         # GDPR Art 17 / right to be forgotten
    RECTIFICATION = "rectification"  # GDPR Art 16
    PORTABILITY  = "portability"     # GDPR Art 20
    OBJECTION    = "objection"       # GDPR Art 21


class DataRightsRequestStatus(str, enum.Enum):
    OPEN       = "open"
    UNDER_REVIEW = "under_review"
    FULFILLED  = "fulfilled"
    DENIED     = "denied"            # e.g. blocked by legal hold


# Window in days within which a data-rights request must be acknowledged
# (used to set the SLA clock on the ticket). 30 days mirrors GDPR Art 12.
DATA_RIGHTS_SLA_DAYS = 30


# ──────────────────────────────────────────────────────────────────────────────
# FORMER-WORKER PORTAL  — strictly scoped document categories
# ──────────────────────────────────────────────────────────────────────────────
class FormerWorkerDocScope(str, enum.Enum):
    FINAL_TIMESHEETS   = "final_timesheets"
    APPROVED_PAYSLIPS  = "approved_payslips"
    TAX_DOCUMENTS      = "approved_tax_documents"
    ENGAGEMENT_DOCS    = "approved_engagement_docs"


# Anything NOT in FormerWorkerDocScope is forbidden for former workers. The
# portal service hard-fails on any scope outside this allowlist.
ALLOWED_FORMER_WORKER_SCOPES = frozenset(s.value for s in FormerWorkerDocScope)


class FormerAccessStatus(str, enum.Enum):
    ACTIVE   = "active"
    EXPIRED  = "expired"
    REVOKED  = "revoked"


# ──────────────────────────────────────────────────────────────────────────────
# RETENTION / LEGAL HOLD
# ──────────────────────────────────────────────────────────────────────────────
class DeletionEligibility(str, enum.Enum):
    BLOCKED_LEGAL_HOLD = "blocked_legal_hold"   # hard block
    RETENTION_ACTIVE   = "retention_active"     # countdown not elapsed
    ELIGIBLE           = "eligible"             # may be deleted/anonymised
    ANONYMISE_ELIGIBLE = "anonymise_eligible"


# ──────────────────────────────────────────────────────────────────────────────
# EVIDENCE PACKAGES
# ──────────────────────────────────────────────────────────────────────────────
class EvidencePackageType(str, enum.Enum):
    ACCESS_CLOSURE   = "access_closure"
    COMPLETION       = "completion_certificate"
    PAYROLL          = "payroll_evidence"
    BILLING          = "billing_evidence"
    LEGAL            = "legal_evidence"
    AUDIT            = "audit_evidence"


# Roles permitted to operate offboarding. `super_admin` and `admin`/`hr` map to
# the existing string roles on User.role. We treat these as the offboarding
# operator set; field-level gating narrows further inside the services.
OFFBOARDING_OPERATOR_ROLES = frozenset({"admin", "hr", "super_admin"})
OFFBOARDING_APPROVER_ROLES  = frozenset({"admin", "super_admin"})

# Source service tag stamped on every audit row so a multi-service deployment
# can prove which component wrote the evidence.
SOURCE_SERVICE = "offboarding"

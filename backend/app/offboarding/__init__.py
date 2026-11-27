"""
Temveris — Offboarding & Former Worker Lifecycle System
========================================================

A governed workforce-exit platform layered on top of the existing
Temveris stack (FastAPI + SQLAlchemy + Supabase Postgres). It does NOT
replace the simple `User.is_active` deactivate path — it wraps it in an
auditable, gated, evidence-producing lifecycle:

    Draft → Approval → Scheduled → Access closure → Tracking stop →
    Final review → Payroll/Billing closure → Legal-hold & retention →
    Former-worker access → Completion certificate

Product doctrine (enforced server-side, never frontend-only):
  • Active access is provably closed (sessions/tokens/devices revoked).
  • Active tracking is provably stopped (post-exit telemetry rejected).
  • Historical + payroll/billing evidence is preserved (zero silent deletion).
  • Legal hold and retention govern any deletion/anonymisation.
  • Former-worker access is isolated (separate scope, never inherits perms).
  • Every critical action is append-only, hash-chained audited.
  • Completion is BLOCKED if any critical control fails.

The module reuses existing infrastructure rather than duplicating it:
  • app.auth.tokens.revoke_all_for_user  → refresh-token revocation
  • database.models.UserSession           → device/session revocation
  • database.models.User.is_active        → login + refresh gate
  • database.models.OutboxEvent           → transactional outbox
  • app.workspace.audit_governance        → org-level legal holds / retention
"""

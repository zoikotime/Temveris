# Offboarding & Former-Worker Lifecycle

A governed workforce-exit closure system layered on the Temveris stack
(FastAPI + SQLAlchemy + Supabase Postgres). **Not** a delete-user feature — an
auditable, gated, evidence-producing lifecycle:

```
Draft → Pending Approval → Approved → Scheduled → In Progress →
Access Closure Pending → (Access Revocation Failed) → Final Review Pending →
Payroll Closure Pending → Billing Closure Pending → Legal Hold Review Pending →
Former Access Pending → Completed         (plus Blocked / Reversed / Cancelled)
```

## Doctrine (enforced server-side, never frontend-only)

- Access is **provably closed** (sessions / refresh tokens / login gate revoked).
- Tracking is **provably stopped** (open clock sessions closed; post-exit ingest rejected).
- History + payroll/billing evidence is **preserved** (zero silent deletion).
- **Legal hold + retention** govern any deletion / anonymisation.
- Former-worker access is **isolated** (separate credential + scope allowlist; never inherits old perms).
- Every critical action is **append-only, hash-chained audited**.
- Completion is **BLOCKED** if any critical control fails (the readiness gate).

## Folder / repository structure

```
backend/app/offboarding/
├── __init__.py        module doctrine + reuse map
├── constants.py       all enums (states, exit types, components, tasks, events…)
├── state_machine.py   the 16-state lifecycle graph + transition guards
├── ids.py             opaque, prefixed UUID identifiers (case_…, task_…, …)
├── models.py          15 SQLAlchemy tables (tenant-isolated, UUID PKs, indexed)
├── events.py          domain-event outbox (idempotent) + hash-chained audit
├── rbac.py            13 roles, permission matrix, ABAC policy, field redaction
├── repository.py      tenant-scoped data access (isolation enforced here)
├── schemas.py         Pydantic v2 request models + §8 response contract
├── workflow.py        task graph, blockers, waivers, SLA/escalation, readiness
├── services.py        10 dependency-injected, event-driven domain services
├── router.py          FastAPI endpoints (validation, rate limit, correlation id)
├── enforcement.py     server-side access + telemetry gate (Tier-0 invariants)
├── incidents.py       graded security incidents + safe diagnostics
├── reconciliation.py  SSO/SCIM queue + identity-conflict detection
├── security_service.py orchestrator for the §7 security API
├── security_router.py  POST /revoke-access, /retry-revocation; GET /reconciliation, /security-events
├── jobs.py            five §5 background processors + run_forever scheduler
├── bootstrap.py       ensure_tables() + install_router()  (wired in main.py)
└── README.md          this file

backend/alembic/versions/b7d2e5f10a44_offboarding_lifecycle.py             lifecycle migration
backend/alembic/versions/c3a1f7e92b15_offboarding_security_enforcement.py  enforcement migration
backend/tests/test_offboarding_*.py                                         tests
```

## Runtime enforcement layer (§1–§8)

A second tier of files makes the doctrine real *at request time*, not just
within the lifecycle:

### Access + telemetry gate — `enforcement.py`
The single authority that decides, from server-side lifecycle state alone, whether
a worker may hold access or push telemetry right now. Reused by the auth gate,
the desktop/mobile ingest endpoints, and the background sweep — never by the
client. A blocked telemetry payload's METADATA (never the bytes, never a
credential) is parked in `offboarding_telemetry_quarantine` and a CRITICAL
incident is opened — append-only proof the stop was enforced server-side.

### Incidents — `incidents.py`
Graded (Critical / High / Medium / Low) failure records with safe diagnostics
(credential-shaped keys stripped), deduplication (re-detection bumps a counter,
never spams), auto-escalation for Critical, and full audit-chain coverage.

### Reconciliation — `reconciliation.py`
Drives the `integration_reconciliation_queue` for SSO/SCIM/CRM/billing with
bounded retries + exponential backoff + dead-lettering, and detects the five
identity-drift scenarios (IdP-active-Temveris-revoked / SCIM failure / stale SSO
state / multiple identities / rehire conflict). Conflicts raise a case blocker
so completion stays gated until cleared.

### Security API — `security_router.py`
| Method | Path                                | Permission     |
|-------:|-------------------------------------|----------------|
| POST   | `/api/offboarding/revoke-access`    | `access.revoke`|
| POST   | `/api/offboarding/retry-revocation` | `access.revoke`|
| GET    | `/api/offboarding/reconciliation`   | `audit.view`   |
| GET    | `/api/offboarding/security-events`  | `audit.view`   |

### Background jobs — `jobs.py`
Five processors driven by `run_forever` from the FastAPI lifespan (5-minute
cadence):
1. `process_scheduled_final_access`     — SCHEDULED → access closure when cutover passes.
2. `process_revocation_retries`         — retry stuck revocations; escalate after the cap.
3. `process_scim_reconciliation`        — drain the queue (retry + dead-letter).
4. `enforce_tracking_stop`              — safety net: close drifted clock sessions, flag leaked telemetry.
5. `detect_identity_conflicts`          — sweep for the five drift scenarios.

## Service layer (§3)

All services are constructed from one `OffboardingContext` (db + actor authz +
correlation id). They are dependency-injected, event-driven, stateless
(thread/async-safe), and unit-testable against in-memory SQLite.

| Service | Responsibility |
|---|---|
| `OffboardingService` | lifecycle orchestrator — owns every state transition + all gates |
| `WorkerLifecycleService` | former-worker access grant / revoke (isolated credential) |
| `IdentityService` | access revocation across every surface (reuses refresh-token + session revocation) |
| `TrackingService` | tracking stop enforcement + post-exit rejection |
| `AuditService` | append-only hash-chained audit + domain events |
| `DocumentService` | former-worker document **scope allowlist** enforcement |
| `RetentionService` | retention assignment + deletion eligibility |
| `LegalHoldService` | legal hold apply / release (hard deletion block) |
| `NotificationService` | event-driven notification dispatch |
| `EvidencePackageService` | evidence bundle + content hashing |

## RBAC + ABAC (§4)

13 roles (`rbac.Role`): Super Admin, HR Admin, HR Lead, Manager, IT/Security
Admin, Payroll Admin, Payroll Lead, Finance Admin, Finance Lead, Legal Admin,
Compliance Admin, Support Admin, Former Worker. Each maps to an explicit
permission set (`rbac.PERMISSIONS`); separation of duties is intentional.

ABAC attributes evaluated on every action: **tenant** (cross-tenant always
denied), **department** (manager scope), **legal_hold_status** (blocks
destructive steps), plus jurisdiction / worker_type / exit_type / risk_level /
case_status / data_classification carried on the case.

Field-level protection (`rbac.redact_case`) redacts the sensitive columns
unless the actor holds the matching field permission:
`termination_reason`, `legal_hold_notes`, `payroll_notes`, `billing_notes`.

## Event contracts (§6)

Every domain event is an append-only `offboarding_domain_events` row with:

- `correlation_id` — ties one user action's whole fan-out together
- `causation_id` — the event that directly caused this one
- `idempotency_key` — **UNIQUE**; re-emitting the same logical event is a no-op (replay-safe)
- `state_from` / `state_to` — for transition events
- `status` — `pending → published` (transactional outbox; `events.dispatch_pending`)

Emitted event types (`constants.EventType`):

```
OffboardingCaseCreated      OffboardingCaseSubmitted     OffboardingCaseApproved
OffboardingCaseScheduled    FinalAccessTimeReached       AccessRevocationStarted
AccessRevocationCompleted   AccessRevocationFailed       TrackingStopped
PostExitTrackingDetected    LegalHoldApplied             LegalHoldReleased
RetentionPolicyAssigned     PayrollClosureCompleted      BillingClosureCompleted
CompletionCertificateGenerated   OffboardingCaseCompleted    OffboardingCaseReversed
OffboardingCaseCancelled    FormerWorkerAccessGranted    FormerWorkerAccessExpired
FormerWorkerAccessRevoked
```

## API (§7) — mounted at `/api/offboarding`

```
POST   /cases                              create (Draft)
GET    /cases                              register (filter by status/worker/engagement/legal_hold)
GET    /cases/{case_id}                    case detail (sensitive fields redacted per role)
PATCH  /cases/{case_id}                    edit (Draft only)
POST   /cases/{case_id}/submit             Draft → Pending Approval
POST   /cases/{case_id}/approve            Pending Approval → Approved → Scheduled/In Progress
POST   /cases/{case_id}/cancel             → Cancelled
POST   /cases/{case_id}/reverse            → Reversed (restores access, revokes former access)
POST   /cases/{case_id}/begin              Scheduled → In Progress
POST   /cases/{case_id}/close-access       revoke access + stop tracking → Final Review / Failed
POST   /cases/{case_id}/retry-access       retry a failed revocation
POST   /cases/{case_id}/final-review       → Payroll Closure Pending
POST   /cases/{case_id}/close-payroll      → Billing Closure Pending
POST   /cases/{case_id}/close-billing      → Legal Hold Review Pending
POST   /cases/{case_id}/review-legal-hold  assign retention → Former Access Pending
POST   /cases/{case_id}/setup-former-access  grant scoped former-worker access
POST   /cases/{case_id}/generate-certificate completion certificate + evidence
POST   /cases/{case_id}/complete           Former Access Pending → Completed (readiness-gated)
POST   /cases/{case_id}/legal-hold         apply a legal hold
POST   /cases/{case_id}/blockers           raise a blocker (→ Blocked)
POST   /cases/{case_id}/waivers            grant an audited waiver
POST   /cases/{case_id}/unblock            Blocked → prior state (no open blockers)
GET    /cases/{case_id}/readiness          completion readiness (the gate)
GET    /cases/{case_id}/timeline           workflow timeline (from domain events)
GET    /cases/{case_id}/audit              append-only audit trail (with hashes)
GET    /cases/{case_id}/blockers           blockers list
```

Cross-tenant ids return **404** (never a 403 that would confirm existence).
Invalid state transitions return **409**. Missing permission returns **403**.
Completion blocked by unmet controls returns **409** with the blocking reasons.

## Migrations

- Runtime: `bootstrap.ensure_tables()` (idempotent, runs on every boot from `main.py` lifespan).
- Versioned: `alembic upgrade head` applies `b7d2e5f10a44_offboarding_lifecycle`.

## Tests

```
pytest backend/tests/test_offboarding_state.py        # state machine transitions
pytest backend/tests/test_offboarding_rbac.py         # RBAC + ABAC + field redaction
pytest backend/tests/test_offboarding_tenant.py       # tenant isolation
pytest backend/tests/test_offboarding_service.py      # full lifecycle + audit/events
pytest backend/tests/test_offboarding_enforcement.py  # security-critical enforcement + P0 blockers
```

Release gates (assert in tests): tenant isolation holds, invalid transitions are
rejected, and audit events are present for every critical action. The §8 P0
blockers — revoked-user login working, tracking continuing post-exit, API token
still valid after revocation, cross-tenant leakage — are each negated by an
explicit test in `test_offboarding_enforcement.py::TestP0Blockers`.

# Temveris — Enterprise Platform Master Design Specification

**Status:** Target architecture (the "north star"). Not an audit, not implementation.
**Horizon:** The platform Temveris should be after ~2 years of disciplined development.
**Source of truth:** [ORG_SETTINGS_AUDIT.md](ORG_SETTINGS_AUDIT.md) · [ORG_SETTINGS_ROADMAP.md](ORG_SETTINGS_ROADMAP.md) · [PLATFORM_ARCHITECTURE_AUDIT.md](PLATFORM_ARCHITECTURE_AUDIT.md).
**Rule:** Every future implementation should conform to this document. Where it conflicts with current code, this document wins and the code is migrated toward it (never the reverse).

---

## 1. Executive Summary

Temveris's target state is a **multi-tenant, modular-monolith SaaS platform** for enterprise workforce analytics, time tracking, and productivity intelligence — competitive with Time Doctor, Hubstaff, Insightful, ActivTrak, and the HR-adjacent capabilities of Rippling/BambooHR. The platform serves **10,000+ organizations and 1,000,000+ employees**, ingesting **millions of activities and screenshots per day**, with **99.9% availability** and horizontal scale.

Architecturally, the target consolidates today's fragmentation into **one canonical identity spine** (`Organization → Department → Team → User`, Integer PKs, real FKs, RLS-enforced), **one configuration engine** (typed + dynamic, 5-level cascade, cached), **one permission engine** (persisted RBAC + resource-scoped ABAC), **one audit/event backbone**, and **one commercial subsystem** (extractable to a service). It adds the infra the current single-container monolith lacks: **Redis** (cache + realtime backplane + job broker), a **dedicated worker fleet**, **object storage** for all binary blobs, and an **outbox-driven event bus**.

The platform is delivered across **five product versions (V1–V5)**, each aligned to the implementation roadmap. It is **compliance-ready** (SOC 2 Type II, GDPR, ISO 27001, HIPAA-capable), **developer-extensible** (public REST API, webhooks, OAuth apps, SDKs, API keys), and **AI-native** (the KAIROS assistant matured into a RAG-backed knowledge and insights layer).

**Target Enterprise Readiness Score: 90 / 100** (from today's 41). This spec defines what "90" means and how each subsystem contributes.

---

## 2. Product Vision (Section 1)

| Dimension | Target |
|---|---|
| **Customers** | 10,000+ organizations, from 5-seat SMBs to 50,000-seat enterprises; agencies managing multiple client orgs |
| **Users** | 1,000,000+ employees; 100,000+ concurrent trackers; thousands of concurrent admins |
| **Scale** | Millions of activities/day, millions of screenshots/day, billions of audit records over time |
| **Industries** | BPO/call centers, IT services, agencies, remote-first companies, finance/insurance, healthcare (HIPAA mode), government (data-residency mode) |
| **Integrations** | HRIS (BambooHR, Workday), payroll (Gusto, ADP), SSO/identity (Okta, Azure AD, Google), comms (Slack, Teams), PM (Jira, Asana), calendars, accounting (QuickBooks, Xero), plus a public API + webhooks for custom |
| **Deployments** | Multi-tenant SaaS (default); single-tenant/dedicated for enterprise; regional data residency (EU/US/APAC); optional self-hosted/private-cloud for regulated buyers |
| **Compliance** | SOC 2 Type II, GDPR/CCPA (DSAR, retention, consent), ISO 27001, HIPAA-capable, audit-grade tamper-evident evidence (WEC) |

**Positioning:** the tracking-tool depth of Time Doctor/Insightful + the org-hierarchy, RBAC, and compliance rigor of the HR platforms + a genuine developer platform none of the pure-monitoring tools offer.

---

## 3. Enterprise Architecture (Section 2)

**Style: modular monolith + satellite services.** One primary deployable (the API monolith with enforced module boundaries) surrounded by a small number of independently-scaled services where the workload genuinely differs (workers, realtime, AI). This avoids premature microservice sprawl while removing the current single-process ceiling.

```
                          ┌───────────────── CDN / Edge ─────────────────┐
                          │   SPA (React) · desktop update artifacts      │
                          └───────────────┬───────────────────────────────┘
                                          │ HTTPS
   Desktop Tracker (Electron) ─┐          │          ┌─ Browser Extension (MV3)
   Web SPA ────────────────────┤   ┌──────▼───────┐  │
   Public API clients / SDKs ──┴──►│  API Gateway  │◄─┘
                                    │ (rate limit,  │
                                    │  authn, CORS) │
                                    └──────┬────────┘
             ┌─────────────────────────────┼───────────────────────────────┐
             ▼                              ▼                               ▼
   ┌───────────────────┐        ┌────────────────────┐           ┌──────────────────┐
   │  API Monolith      │        │  Realtime Gateway  │           │   AI Service      │
   │  (modular)         │        │  (WebSocket,       │           │  (RAG, insights,  │
   │  router→service→   │        │   Redis pub/sub)   │           │   assistant)      │
   │  repository        │        └─────────┬──────────┘           └────────┬─────────┘
   │  domains (S3):     │                  │                                │
   │  identity·org·cfg· │                  │                                │
   │  tracking·leave·   │        ┌─────────▼──────────┐                     │
   │  payroll·billing·  │◄──────►│      Redis          │◄────────────────────┘
   │  audit·notif·intg  │        │  cache · pub/sub ·  │
   └───┬──────────┬─────┘        │  Celery broker      │
       │          │              └─────────┬───────────┘
       │          │                        │
       ▼          ▼              ┌──────────▼───────────┐
 ┌──────────┐ ┌─────────────┐    │   Worker Fleet        │
 │ Postgres │ │ Object Store│    │  (Celery beat +       │
 │ + RLS    │ │ (S3-compat) │    │   workers): schedules,│
 │ read     │ │ screenshots,│    │   screenshot pipeline,│
 │ replicas │ │ exports,logo│    │   billing, exports,   │
 └──────────┘ └─────────────┘    │   webhooks, events    │
       ▲                          └──────────┬───────────┘
       │        Outbox → Event Bus ──────────┘
       └── Observability: metrics (Prometheus) · logs · traces (OTel)
```

| Component | Target design |
|---|---|
| **Frontend** | React + TypeScript, feature-folder structure, single API client + React Query, shared design-system package, route + tab code-splitting |
| **Backend** | FastAPI modular monolith; every domain `router→service→repository`; `common/` for deps/errors/pagination; Alembic-only schema |
| **Desktop Tracker** | Electron shell; pulls config from a versioned `/v1/settings/effective`; uploads to an async screenshot pipeline; OS-keychain refresh tokens (keep the current strong model) |
| **Browser Extension** | MV3; OAuth device-flow auth (replace the copy-pasted token); posts to `/v1/browser/*` |
| **AI Services** | Separate service: assistant, RAG over org knowledge, insights, risk/burnout — consumes the event bus + read replicas |
| **Worker Services** | Celery (broker=Redis) + beat; all scheduled + event-driven work; screenshot processing off the request path |
| **Notification Services** | Event-driven; channel adapters (email/Slack/Teams/webhook/in-app); templated + localized |
| **Billing/Commercial** | The layered `commercial` subsystem, reconciled to Integer org identity; extractable to a standalone service |
| **Reports/Analytics** | Read-replica-backed; materialized rollups for hot dashboards; async export pipeline |
| **Integrations / Developer Platform** | Connector framework + public API + webhooks + OAuth apps (Sections 8) |
| **Storage / Object Storage** | Postgres for relational; S3-compatible object storage for all blobs (screenshots, exports, logos, attachments) |
| **Caching / Queues / Realtime** | Redis for cache, Celery broker, and WebSocket pub/sub backplane |
| **Authentication** | JWT access + rotating refresh (keep current design) + MFA + SSO/SAML + SCIM |

---

## 4. Domain Design (Section 3) — Bounded Contexts

Each context owns its tables, exposes a service interface (never its DB to other contexts), and communicates cross-context via the event bus for anything asynchronous.

| Context | Responsibilities | Owns (DB) | API surface | Emits events | Depends on |
|---|---|---|---|---|---|
| **Identity** | Auth, sessions, MFA, SSO/SCIM, users | `users`, `user_sessions`, `refresh_tokens`, `mfa_factors`, `sso_configs`, `scim_*` | `/v1/auth/*`, `/v1/users/*` | `user.created/deactivated`, `session.revoked` | — |
| **Organizations** | Canonical org record, profile, branding, residency | `organizations` (rich), `commercial_profile` (1:1) | `/v1/org/*` | `org.updated`, `org.owner_changed` | Identity |
| **Departments** | Department hierarchy | `departments` | `/v1/departments/*` | `dept.*` | Organizations |
| **Teams** | Teams under departments, membership | `teams`, `team_members` (FK) | `/v1/teams/*` | `team.*` | Departments |
| **Employees** | Roster, employment profile, live status | `employee_profiles`, `live_status` | `/v1/employees/*` | `employee.*` | Identity, Teams |
| **Attendance** | Clock sessions, breaks, policies, alerts | `clock_sessions`, `break_sessions`, `attendance_policies` | `/v1/attendance/*` | `clock.*`, `break.*`, `attendance.violation` | Settings, Employees |
| **Tracking** | Ingest of activity/app/browser telemetry | `activities`(metadata), `app_sessions`, `browser_activities` | `/v1/tracking/*`, `/v1/browser/*` | `activity.ingested` | Settings |
| **Activity/Integrity** | Active-time engine, integrity scoring, WEC | `activity_*_scores`, `integrity_*`, `work_evidence_chain` | `/v1/activity/*`, `/v1/integrity/*` | `integrity.flagged` | Tracking |
| **Screenshots** | Async capture pipeline, storage refs | `screenshots` (reference table) | `/v1/screenshots/*` | `screenshot.stored` | Tracking, Storage |
| **Reports/Analytics** | Aggregations, rollups, exports | materialized rollups, `report_exports` | `/v1/reports/*` | `export.ready` | most (read replicas) |
| **Leave** | Requests, approvals, balances, policies | `leave_*`, `holidays` | `/v1/leave/*`, `/v1/holidays/*` | `leave.requested/approved` | Settings, Employees |
| **Payroll** | Configs, runs, items | `payroll_*` | `/v1/payroll/*` | `payroll.run_completed` | Attendance, Leave |
| **Billing/Commercial** | Plans, subscriptions, invoices, seats, trials, dunning | `commercial_*` (reconciled) | `/v1/billing/*`, `/v1/commercial/*` | `subscription.*`, `invoice.*` | Organizations |
| **Settings/Config** | The configuration engine (cascade + typed) | `app_settings`, typed policy tables, `config_templates` | `/v1/settings/*` | `config.changed` | Organizations, Departments, Teams |
| **Permissions** | RBAC + ABAC, roles, grants, delegation | `roles`, `permissions`, `role_permissions`, `user_roles`, `permission_grants` | `/v1/roles/*`, `/v1/permissions/*` | `role.assigned`, `grant.expired` | Organizations |
| **Integrations** | Connector framework, OAuth connections, sync | `integration_connections`, `integration_syncs` | `/v1/integrations/*` | `integration.synced` | Developer, event bus |
| **Developer** | API keys, OAuth apps, webhooks | `api_keys`, `oauth_apps`, `webhook_subscriptions`, `webhook_deliveries` | `/v1/developer/*` | `webhook.delivered/failed` | Permissions |
| **Audit/Compliance** | Unified audit, retention, legal hold, DSAR, evidence | `audit_events` (unified), `retention_rules`, `legal_holds`, `data_requests` | `/v1/audit/*`, `/v1/compliance/*` | `audit.recorded` | event bus (consumer) |
| **Notifications** | Channel delivery, templates, preferences | `notification_settings`, `notification_log`, `templates` | `/v1/notifications/*` | `notification.sent` | event bus (consumer) |
| **AI** | Assistant, RAG, insights, risk narratives | `ai_conversations`, `ai_documents`, vector store | `/v1/ai/*` | `ai.insight_generated` | Analytics, event bus |

**Context-map rule:** synchronous cross-context reads go through the target context's **service interface**; all fan-out (audit, notifications, integrations, webhooks, AI) is **event-driven** via the outbox → event bus. No context reads another's tables directly.

---

## 5. Database Design (Section 4)

### 5.1 Identity spine (canonical)
```
organizations (INT PK) ── rich: name, slug, legal_entity, domain, region,
  │   timezone, currency, locale, logo_url, brand_colors, plan_ref, status, owner_user_id
  ├─1:1─ commercial_profile (organization_id FK) ── plan, seats, quotas, stripe_ref, trial_*
  ├─1:N─ departments (id, organization_id FK, name, parent_department_id NULLABLE, head_user_id)
  │         └─1:N─ teams (id, organization_id FK, department_id FK, name, head_user_id)
  │                   └─N:M─ team_members (team_id FK, user_id FK)  [replaces User.team string]
  └─1:N─ users (id, organization_id FK NOT NULL, department_id FK, primary_team_id FK,
              employment_type, status)  [role via user_roles, not a string column]
```
All tenant tables: `organization_id INT FK NOT NULL`, indexed, **RLS-enforced**, `ON DELETE` via soft-delete + purge job (not `SET NULL`).

### 5.2 Domain tables (target)
| Group | Tables (canonical) |
|---|---|
| Permissions | `roles`, `permissions`, `role_permissions`, `user_roles`, `permission_grants` (temporary/delegated, with `expires_at`) |
| Settings/Config | `app_settings` (dynamic key/value cascade), typed: `security_policies`, `integration_connections`, `notification_settings`, `attendance_policies`, `config_templates`, `config_key_registry` |
| Attendance/Tracking | `clock_sessions`, `break_sessions`, `activities` (metadata only), `app_sessions`, `browser_activities`, `screenshots` (ref table: url, taken_at, blur, integrity flags) |
| Activity/Integrity | `activity_windows`, `activity_minute_scores`, `activity_daily_scores`, `integrity_scores`, `integrity_events`, `work_evidence_chain` (hash-chained) |
| Leave/Holiday/Payroll | `leave_policies`, `leave_requests`, `employee_leave_balances`, `leave_balance_adjustments`, `holidays`, `payroll_configs`, `payroll_runs`, `payroll_items` |
| Billing/Commercial | `commercial_subscriptions`, `commercial_invoices` (cents), `billing_ledger`, `entitlements`, `checkout_sessions`, `price_quotes`, `provider_webhook_receipts` (legacy `billing_*` "default" tables **removed**) |
| Audit/Compliance | `audit_events` (unified, org-scoped, correlation_id, optional hash chain), `outbox_events` (single), `retention_rules`, `legal_holds`, `data_requests` (DSAR), `evidence_packages` |
| Notifications | `notification_settings` (org + user), `notification_templates`, `notification_log` |
| **New platform tables** | `feature_flags` (org-scoped + global, JSON value + rollout), `api_keys` (hashed, scoped, rotatable), `oauth_apps`, `webhook_subscriptions`, `webhook_deliveries`, `integration_connections`, `automation_rules`, `usage_events` (metering) |
| Analytics | materialized `daily_org_rollups`, `daily_user_rollups`, `report_exports` |

### 5.3 Database principles
- **One PK regime:** Integer (bigint) surrogate PKs everywhere; external references (Stripe, provider ids) are attributes, not identities.
- **RLS on every tenant table** — defense in depth over app-level filtering.
- **Blobs never in-row** — object storage refs only (screenshots, avatars, exports).
- **Partitioning/archival** for high-volume tables (`activities`, `audit_events`, `screenshots`) by time; cold data to cheaper storage.
- **Read replicas** for analytics/reporting; writes to primary.
- **One schema source:** Alembic; CI asserts model == head; no runtime `create_all`.

---

## 6. Configuration Engine (Section 5)

**One engine, hybrid storage, 5-level inheritance, cached.**

- **Cascade:** `System default → Organization → Department → Team → Employee`. Resolved by FK (not the `team_key` string). Effective value = deepest override wins.
- **Storage:** dynamic/extensible keys in `app_settings` (key/value); structured policy in typed tables (`security_policies`, etc.). Both resolve through **one resolver**.
- **Key registry:** every config key declared once in `config_key_registry` — type, validation rules, default, which scopes may override, UI control metadata. The admin UI and the write-validator share this single definition.
- **Overrides & reset:** per-key override at any tier; reset-to-inherited deletes the tier row; the UI shows override/inherited/system-default badges + the inherited value (mature today — keep it, extend to Department).
- **Templates:** `config_templates` — a named preset config map appliable to a scope ("Call-center defaults", "EU privacy profile"). First-class reuse of the engine.
- **Feature flags:** layered evaluation — plan entitlement → org flag → rollout %. Evaluated by a `feature_flags` service with a typed API (`is_enabled(org, flag)`), cached.
- **Caching:** effective config cached in Redis keyed by `(org, dept, team, user, key-set)`, invalidated on write (WebSocket push already exists for live UI refresh).
- **Validation:** server-side against the registry; typed values; no more "true"/"false" text in a bag for security posture.

---

## 7. Permission Engine (Section 6)

**Persisted RBAC + resource-scoped ABAC, one enforcement point.**

- **Tables:** `roles` (system + custom, org-scoped), `permissions` (fine-grained verbs), `role_permissions`, `user_roles`, `permission_grants` (temporary/delegated).
- **Custom roles:** orgs define roles from the permission catalog (promote `offboarding/rbac.py`'s matrix as the platform baseline: ~13 roles, granular `Permission` set).
- **Scoped permissions (ABAC):** every permission carries a scope — `own | team | department | organization | platform`. A "Team Admin" gets `employees.manage@team`; a "Department Admin" gets it `@department`.
- **Delegated admin:** org owner delegates a permission subset to a department/team admin, bounded to that scope. Separation of duties: you cannot grant a role/permission you don't hold.
- **Temporary permissions:** `permission_grants` with `expires_at`; a worker expires them and emits `grant.expired` to audit.
- **Enforcement:** a single `require_permission(permission, scope_resolver)` dependency in `common/deps` replaces every ad-hoc `role in (...)` check. Deny-by-default.
- **Audit:** every grant/revoke/role change → `audit_events` (closes today's silent role-change gap).

---

## 8. Commercial Platform (Section 7)

- **Subscriptions/Plans:** `commercial_subscriptions` per org (Integer FK); plans in a governed catalog with `feature_flags_json` feeding the entitlements service. Billing cycles, proration, upgrade/downgrade with effective dates.
- **Licensing/Entitlements:** `entitlements` computed from plan + overrides, cached; every feature gate reads the entitlements service (not scattered plan checks).
- **Trials:** trial lifecycle worker (exists in `commercial/`) — Day 0..N emails, conversion, expiry.
- **Invoices/Payments:** `commercial_invoices` (integer cents — the correct unit), Stripe (and pluggable providers) via the ledger + provider-webhook idempotency (exists). One invoice model (legacy dollars-Float `BillingInvoice` removed).
- **Usage/Metering:** `usage_events` — seats, tracked hours, storage, API calls; drives usage-based add-ons and quota enforcement.
- **Seat management & quotas:** seats tracked on `commercial_profile`; enforcement at invite/activation; quota checks (storage, API, orgs-per-agency) via entitlements.
- **Marketplace (V4+):** integration marketplace + paid add-ons (advanced AI, premium connectors), billed through the same ledger.
- **Isolation:** the whole subsystem is a dependency island — **extractable to a standalone billing service** once identity is reconciled (ADR-011).

---

## 9. Developer Platform (Section 8)

- **REST API:** the public, versioned `/v1` surface — the same contracts the SPA uses, documented via OpenAPI. Consistent pagination/filtering/sorting envelope.
- **GraphQL:** *optional, deferred.* Offered only if partners demand flexible querying; the REST + webhooks surface covers the majority. (ADR: not V1–V3.)
- **SDKs:** generated from OpenAPI (TypeScript, Python) — no hand-maintained clients.
- **Webhooks:** `webhook_subscriptions` (event types, signing secret, retries with backoff, dead-letter); `webhook_deliveries` log; driven by the event bus.
- **API keys:** `api_keys` — hashed at rest, scoped to permissions, rotatable, per-key rate limits, org-scoped, auditable.
- **OAuth apps:** third-party apps request scoped access via OAuth 2.0 authorization-code; `oauth_apps` + consent screen.
- **Rate limits:** per-key/per-app/per-org tiers (Redis token bucket); today's slowapi-on-auth-only becomes platform-wide, entitlement-driven.
- **Developer portal:** self-serve — key management, app registration, webhook config, usage/quota dashboards, docs, API playground.

---

## 10. Infrastructure Design (Section 9)

| Concern | Target |
|---|---|
| **Cloud** | Cloud Run (or GKE at scale) + managed Postgres (Supabase/Cloud SQL) + managed Redis (Memorystore) + S3-compatible object storage (GCS/S3) |
| **Containers** | Separate images/services: `api`, `worker`, `realtime`, `ai`; horizontally scalable; health/readiness probes |
| **Redis** | Cache, Celery broker/result backend, WebSocket pub/sub backplane, rate-limit buckets, feature-flag cache |
| **Workers** | Celery + beat; queues by priority (ingest, billing, exports, webhooks, notifications); autoscaled |
| **Queues** | Redis-backed Celery; heavy/async: screenshot processing, exports, webhook delivery, payroll runs, dunning |
| **Storage** | Postgres (relational, RLS, read replicas, time-partitioning) |
| **Object storage** | Screenshots, exports, logos, attachments; lifecycle rules (hot→cold→delete per retention); signed URLs |
| **CDN** | SPA + static + desktop update artifacts + signed screenshot URLs |
| **Monitoring** | Prometheus metrics + Grafana; SLO dashboards (latency, error rate, queue depth, ingest lag) |
| **Logging** | Structured JSON logs, centralized (Loki/Cloud Logging), correlation-id per request |
| **Tracing** | OpenTelemetry across api→worker→ai; trace ingest→score→report |
| **Metrics** | Business (active trackers, screenshots/day, MRR) + system (p95, saturation) |
| **Backup** | Automated Postgres PITR; object-storage versioning; tested restores |
| **Disaster Recovery** | Multi-AZ primary; cross-region replica; documented RTO ≤ 1h, RPO ≤ 5m; runbooks (extend the existing `runbooks/`) |

**Migration from today:** the single `app` container splits into `api` + `worker` + `realtime`; Redis and object storage become first-class; the empty `workers/`/`infrastructure/` stubs are filled with real IaC (Terraform) or deleted.

---

## 11. Security Design (Section 10)

| Control | Target |
|---|---|
| **Authentication** | JWT access (short-lived) + rotating, reuse-detecting refresh tokens (keep — it's already enterprise-grade); device-bound tokens for tracker/extension |
| **MFA** | TOTP + WebAuthn/passkeys; enforced per org policy; recovery codes; break-glass; **real `mfa_factors` table + login enforcement** (today it's non-functional) |
| **SSO** | OIDC (Google, Azure AD, Okta) + generic OIDC; enforce-for-admins/all toggles that are actually enforced |
| **SAML** | SAML 2.0 SP (enterprise IdPs) |
| **SCIM** | SCIM 2.0 provisioning/deprovisioning from the IdP |
| **RBAC/ABAC** | The persisted permission engine (Section 7); deny-by-default; one enforcement dependency |
| **Encryption** | TLS in transit; at-rest via cloud KMS; **field-level encryption** for PII/payroll/bank; screenshots access-controlled + signed URLs |
| **Secrets** | Secret manager (GCP Secret Manager/Vault); **never in git**; rotation; the current committed `.env` purged (P0) |
| **Audit** | Unified tamper-evident `audit_events`; every security-relevant mutation logged; correlation ids |
| **Compliance** | SOC 2 controls mapped; GDPR DSAR + retention + consent; ISO 27001; HIPAA mode (BAA, PHI handling); data residency by region |
| **Tenant isolation** | RLS + centralized org-scoped access; regression-tested (the existing `test_tenant_isolation.py` expanded) |
| **Enforced policies** | Session idle/absolute timeout, IP allow-list, password policy — **actually enforced by middleware** (today stored-but-ignored) |

---

## 12. AI Platform Design (Section 11)

Matures KAIROS from a chatbot into an AI layer, running as a **separate service** consuming the event bus + read replicas (never blocking core requests).

| Capability | Target |
|---|---|
| **AI Assistant** | Conversational assistant over org data, RBAC-scoped (only answers within the caller's permission scope) |
| **Knowledge Base / RAG** | Vector store (pgvector or managed) over org docs, policies, help content; retrieval-augmented answers with citations |
| **Document Intelligence / OCR** | Parse uploaded docs (contracts, receipts, timesheets); extract structured data |
| **Meeting Intelligence** | (V4+) transcription + summarization + action items, if meetings are in scope |
| **Employee Insights** | Burnout, focus, workflow analytics matured from the existing `intelligence/` engines; narrative summaries |
| **Risk Detection** | Fraud/fake-activity (build on `integrity/` + `proof_of_work/`); anomaly detection on activity streams |
| **Automation** | `automation_rules` — event-triggered actions ("if attendance violation → notify manager + create task"); AI-suggested rules |

**Principles:** AI is advisory, auditable, and permission-scoped; no AI action mutates data without an explicit rule/approval; model calls are provider-abstracted and cost-metered.

---

## 13. Scalability (Section 12)

| Target | How the architecture meets it |
|---|---|
| **10,000 organizations** | Canonical Integer identity + RLS + indexed `organization_id`; entitlements cached; no five-headed org model |
| **1,000,000 employees** | Read replicas for analytics; materialized rollups for dashboards; cached config cascade (no per-request re-query on the poll path) |
| **Millions of activities/day** | Time-partitioned `activities`; metadata-only rows (blobs in object storage); async ingest |
| **Millions of screenshots/day** | Async screenshot pipeline (off the request path, via Celery); object storage + lifecycle rules; `screenshots` reference table |
| **High availability (99.9%)** | Stateless api/worker/realtime (state in Postgres/Redis/object store); multi-AZ; health probes; graceful degradation |
| **Horizontal scaling** | Every tier scales out: api (stateless), worker (queue-driven), realtime (Redis pub/sub backplane — the current in-process WS ceiling removed), schedulers (Celery beat, not per-process loops) |

**The three ceilings removed vs today:** in-process WebSocket rooms → Redis pub/sub; per-Gunicorn-worker duplicated schedulers → Celery beat; synchronous screenshot processing → async pipeline.

---

## 14. Coding Standards (Section 15)

**Backend (Python/FastAPI)**
- Layering mandatory: `router` (HTTP/validation only) → `service` (business logic) → `repository` (all DB access). **No `db.query()` in routers.**
- Type hints everywhere; Pydantic for all I/O schemas; no business logic in `main.py`.
- One bounded context per package; cross-context calls via service interfaces; async fan-out via events.
- Errors: typed exceptions → central handlers; generic 500 handler; correlation id on every request/log.
- `common/` for deps, errors, pagination, responses.

**Frontend (React/TypeScript)**
- TypeScript; feature-folder structure (`features/<domain>/{pages,components,hooks,api}`).
- One API client + React Query for all server state (no manual `useState`/`useEffect` fetch, no raw `fetch`).
- Shared design-system package (`components/ui`) — no per-feature `Modal`/`Button`/`Toggle` reinvention.
- All theming via `useTheme()`; ARIA-complete primitives; components under ~400 lines.

**Database**
- Integer surrogate PKs; snake_case tables/columns; `organization_id` FK + RLS on tenant tables; no blobs in-row.
- Alembic-only; one migration per logical change; forward-and-back tested; CI model==head check.

**API**
- Versioned `/v1`; REST resource naming; consistent pagination/filter/sort envelope; OpenAPI as the contract; deprecations announced with sunset headers.

**Testing**
- Unit (services/engines, pure logic), integration (router→DB), contract (OpenAPI), E2E (critical flows), tenant-isolation regression suite. Target ≥ 70% on core domains; 100% on billing/permissions/security paths.

**Naming / Folders / Docs**
- Consistent kebab/snake/Pascal per layer; one `docs/` for ADRs + specs; every module a README; ADRs for every significant decision (this doc's Section 14 is the seed).

---

## 15. Product Roadmap (Section 13) — Versions

| Version | Theme | Contents (maps to roadmap phases) |
|---|---|---|
| **V1.0 — Stabilize & Secure** | Trustworthy foundation | Critical bugs (Phase 1): secret rotation, `team_id` fix, billing de-`default`, org-filter fixes, holidays→API. Security foundation (Phase 2A): persisted RBAC + `require_permission`, tenant-isolation centralization, unified audit, session/policy enforcement, rate limiting |
| **V2.0 — Enterprise Identity** | Sellable to enterprise IT | Phase 2B: MFA (real), Google/MS SSO, SAML2, SCIM, password policies, trusted devices |
| **V3.0 — Platform Foundation** | The base everything builds on | Phase 3: canonical org spine + Department + team FK, 5-level config engine + cache, schema single-source, `common/` + service/repo refactor, Redis + worker fleet + object storage, org templates |
| **V4.0 — Enterprise & Developer Platform** | Extensibility & depth | Phase 4: feature flags, API keys, webhooks, integrations/connectors, developer portal, automation, reports/analytics, branding, notifications engine |
| **V5.0 — Commercial, AI & Scale** | Full SaaS + intelligence | Phase 5–7: full billing/subscription/seats/quotas/usage, marketplace, org cloning, import/export, backup/restore; AI platform (RAG, insights, automation); performance/HA + frontend redesign + TypeScript |

**Gate between every version:** the prior version's exit criteria (esp. V3's platform-foundation criteria) must be met before the next begins. No V4 feature ships on the pre-V3 foundation.

---

## 16. Architecture Decision Records (Section 14)

Carrying forward ADR-001…010 from [PLATFORM_ARCHITECTURE_AUDIT.md](PLATFORM_ARCHITECTURE_AUDIT.md) §16 (modular monolith; canonical Integer org spine; delete dead scaffold; Alembic-only schema; Redis + workers; blobs to object storage; persisted RBAC+ABAC; unified config engine; unified audit/event store; frontend design-system + React Query). Additional target-state ADRs:

**ADR-011 — Commercial is extractable, not extracted (yet).** *Chosen:* keep `commercial` as an in-monolith island now; extract to a service only post-identity-reconciliation if scale/compliance demands. *Rejected:* immediate microservice — would export the String/Integer identity mismatch across a network boundary.

**ADR-012 — Event-driven fan-out via transactional outbox.** *Chosen:* `outbox_events` → event bus for audit/notifications/integrations/webhooks/AI. *Rejected:* synchronous in-request fan-out (today's model — couples latency to side effects) and dual-write to a broker (loses atomicity).

**ADR-013 — RLS + app-level scoping (defense in depth).** *Chosen:* Postgres RLS *and* a centralized org-scoped query dependency. *Rejected:* app-level-only (today — one forgotten filter = leak); RLS-only (harder to reason about in app code).

**ADR-014 — Hybrid config storage (typed + dynamic).** *Chosen:* typed tables for structured policy, `app_settings` key/value for dynamic keys, one resolver. *Rejected:* all-EAV (today's Text bag — unqueryable, untyped) and all-typed (schema change per new key — too rigid for tracker tunables).

**ADR-015 — REST-first developer platform; GraphQL deferred.** *Chosen:* versioned REST + webhooks + SDKs. *Rejected:* GraphQL in the base platform — added complexity without demonstrated partner demand; revisit at V4+.

**ADR-016 — Object storage for all blobs; async screenshot pipeline.** *Chosen:* S3-compatible storage + Celery pipeline. *Rejected:* base64-in-row (today — bloats hottest tables) and synchronous in-request processing (caps screenshots/day).

**ADR-017 — Integer surrogate PKs platform-wide.** *Chosen:* reconcile commercial/offboarding onto Integer identity; external refs are attributes. *Rejected:* String-UUID identities (today's commercial/offboarding — three incompatible regimes, broken joins).

**ADR-018 — Separate realtime, worker, and AI services; monolith for the rest.** *Chosen:* split only where the workload profile genuinely differs. *Rejected:* full microservices (premature) and pure single-process monolith (today's ceiling).

---

## 17. Success Metrics (Section 16)

| Dimension | Measurable target |
|---|---|
| **Performance** | API p95 < 300 ms; dashboard load < 2 s; screenshot ingest lag < 30 s; config resolve < 10 ms (cached) |
| **Scalability** | 10k orgs / 1M employees / 100k concurrent trackers with linear cost; queue depth drained < 1 min at peak |
| **Availability** | 99.9% monthly uptime; RTO ≤ 1h, RPO ≤ 5m; zero-downtime deploys |
| **Security** | SOC 2 Type II + ISO 27001 certified; 0 criticals in pen tests; 100% security-mutations audited; MFA/SSO adoption tracked |
| **Maintainability** | 0 `db.query()` in routers; no file > 800 lines; ≥ 70% test coverage (100% on billing/permissions/security); one schema source (CI-enforced) |
| **Developer experience** | New domain module scaffolded in < 1 day; public API onboarding < 30 min; generated SDKs; < 15 min local setup |
| **Customer experience** | Self-serve onboarding < 10 min; settings discoverable (search + IA); admin tasks < 3 clicks; WCAG 2.1 AA; mobile-responsive admin |

---

## 18. Enterprise Readiness Score

| State | Score |
|---|---|
| Today (from Enterprise Audit) | 41 |
| After V1 (stabilize + security foundation) | ~60 |
| After V2 (enterprise identity) | ~66 |
| After V3 (platform foundation) | **~72 — credible enterprise baseline** |
| After V4 (enterprise + developer platform) | ~82 |
| After V5 (commercial + AI + scale/HA) | **~90 — mature enterprise SaaS (target)** |

**What "90" means:** one canonical multi-tenant identity with RLS; one config engine; one permission engine; one audit/event backbone; Redis-backed realtime/jobs; object-storage blobs; enforced MFA/SSO/SAML/SCIM; a public developer platform; a matured AI layer; SOC 2/ISO/GDPR compliance; and a modular, tested, single-schema codebase where every future feature slots into a bounded context without fighting the foundation.

**The through-line of the entire trilogy + this spec:** *fix the foundation before building on it.* This document is the destination; the roadmap is the route; the audits are why. Build in version order, gate each version on the last's exit criteria, and Temveris compounds toward this target instead of accumulating the rework that a five-headed org model, triple schema management, and a single-process ceiling would otherwise guarantee.

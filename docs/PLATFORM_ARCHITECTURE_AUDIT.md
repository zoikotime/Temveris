# Temveris — Enterprise Platform Architecture Audit & Blueprint

**Date:** 2026-07-16
**Purpose:** The master architectural blueprint for Phase 3. Determines whether the platform is maintainable, scalable, modular, and enterprise-grade — and defines the target architecture to build toward.
**Companion docs:** [ORG_SETTINGS_AUDIT.md](ORG_SETTINGS_AUDIT.md), [ORG_SETTINGS_ROADMAP.md](ORG_SETTINGS_ROADMAP.md).
**Nature:** Read-only. No code written, no migrations generated, no refactoring performed.

---

## 1. Executive Summary

Temveris is **two codebases wearing one repo**. A newer, properly layered set of modules (`commercial`, `offboarding`, `intelligence`, `active_time`, `proof_of_work`, `kairos`) demonstrates the team *knows* how to build clean, service-oriented, self-contained domains. An older router-monolith set (`employees` at 4,044 lines, `leave` at 2,508, `reports` at 1,890, `org`, `workspace`, `super_admin`, plus `main.py` at 2,466) does the opposite: HTTP handlers own the database directly — **489 `db.query()` calls across 28 router files** — with no service or repository boundary.

The platform runs as a **single monolithic container** with **no Redis, no message queue, no cache, and no object storage of its own**. Background jobs are in-process `asyncio` loops that duplicate under multi-worker Gunicorn; WebSocket rooms are in-memory with no fan-out backplane. Both are **hard ceilings on horizontal scale** — the system cannot safely run more than one process/instance for real-time features or scheduled work without a redesign. The advertised `workers/`, `infrastructure/`, and root `database/` directories are **empty `.gitkeep` stubs** — the folder structure implies an architecture that does not exist.

The data model is the deepest problem: **~112 tables across 6 model files in three incompatible PK/tenancy regimes** (Integer-PK/Integer-org-FK core; String-UUID-PK/String-org commercial; String-UUID/String-`tenant_id`/no-org offboarding). There are **five "organization" concepts** that barely link, **six parallel audit logs**, **two subscription systems**, two invoice tables (dollars vs cents), and **no tables at all** for feature flags, API keys, webhooks, integrations, roles/permissions, or departments. Screenshots aren't even a table — they're base64 blobs in `Activity.thumbnail`.

**Is it maintainable?** Partially — the layered half is; the monolith half is not.
**Scalable?** No, not horizontally, without an infra and data-model redesign.
**Modular?** Half. The good modules prove the target; the rest violate it.
**Enterprise-grade?** No — not with single-container-only real-time/jobs, string-keyed billing, and a five-headed org model.

**What must be redesigned before Phase 3** (in priority order): (1) canonical organization/tenancy model; (2) schema single-source-of-truth (kill the triple schema management); (3) infra baseline (Redis for cache + WS backplane + real job queue); (4) a shared service/repository layer + `common/` deps package for the monolith modules; (5) a typed configuration engine and a persisted permission engine.

**Current Architecture Readiness Score: 38 / 100** (§18). Target after Phase 3 foundation: ~70; after full platform build-out: ~86.

---

## 2. Current Architecture Score

| Dimension | Weight | Score | Rationale |
|---|---|---|---|
| Modularity / separation of concerns | 20% | 40 | Half the modules are layered; half are router-monoliths with 489 in-router `db.query()` |
| Data model / schema integrity | 20% | 25 | 5 org concepts, 3 PK regimes, 6 audit logs, string-keyed billing, dead scaffold, triple schema mgmt |
| Scalability / infra | 20% | 30 | Single container; no queue/cache/backplane; in-process schedulers + WS can't scale past 1 process |
| Multi-tenancy | 15% | 35 | App-level `organization_id` only, no RLS; billing single-tenant; commercial/offboarding not org-FK'd |
| Frontend architecture | 10% | 45 | Lazy-loading + theme are good; no design system, 3 API clients, 9.7k-line pages, no server-state lib |
| Maintainability / tech debt | 10% | 40 | Dead modules, duplicate domains, monster files, reactive migrations |
| Security architecture | 5% | 45 | Strong token design; but RBAC unpersisted, secrets committed (from prior audit) |
| **Weighted total** | | **38** | **Strong pockets, weak foundation** |

---

## 3. Recommended Target Architecture

**Verdict: stay a monolith, but make it a *modular* monolith — do not go microservices yet.** The codebase already has the seams (the layered modules). Microservices would multiply the tenancy/schema problems before they're solved. The target is a single deployable with enforced internal module boundaries, plus a thin infra layer (Redis, a job queue, object storage) and *one* extractable service candidate (`commercial`) for later.

```
┌──────────────────────────────────────────────────────────────────────┐
│                        EDGE / CDN (static SPA)                         │
└───────────────┬───────────────────────────────────┬──────────────────┘
                │                                     │
        ┌───────▼────────┐                   ┌────────▼─────────┐
        │  Web API (FastAPI, modular monolith)│  Realtime (WS)  │
        │  ┌──────────────────────────────┐  │  gateway         │
        │  │ Domain modules (layered):    │  │  (own process,   │
        │  │  router → service → repo     │  │   Redis-backed)  │
        │  │  identity · org · settings   │  └────────┬─────────┘
        │  │  tracking · leave · payroll  │           │
        │  │  billing/commercial · audit  │   ┌───────▼────────┐
        │  │  offboarding · intelligence  │   │ Redis          │
        │  └──────────────────────────────┘  │  cache +        │
        │  shared: common/(deps,errors,      │  pub/sub +      │
        │  pagination) · config-engine ·     │  Celery broker  │
        │  permission-engine · event-bus     │  └───────┬───────┘
        └───────┬──────────────┬─────────────┘          │
                │              │                 ┌───────▼────────┐
        ┌───────▼──────┐ ┌─────▼───────┐         │ Worker service │
        │ Postgres     │ │ Object store│         │ (Celery beat + │
        │ (Supabase)   │ │ (Supabase / │         │  workers) —    │
        │ + RLS        │ │  S3-compat) │         │  scheduled +   │
        └──────────────┘ └─────────────┘         │  event-driven  │
                                                  └────────────────┘
```

**Guiding principles:**
1. **One module = one bounded context** with `router → service → repository → models → schemas`. Routers never touch the DB; cross-module calls go through service interfaces, never another router's private functions.
2. **One canonical identity spine:** `Organization → Department → Team → User`, Integer PKs, real FKs, RLS-enforced tenancy.
3. **Infra is explicit, not implied:** Redis (cache + WS backplane + broker), a real worker service, object storage for all blobs, an outbox-driven event bus (the `OutboxEvent` table already exists — wire a consumer).
4. **Schema has one source of truth:** Alembic only. Delete boot-time `ensure_tables()`/`create_all`.
5. **Cross-cutting engines are shared libraries:** config, permissions, audit, notifications, feature flags — each a single engine consumed by all modules, not reimplemented per module.

---

## 4. Modules to KEEP (already correct — use as the pattern)

| Module | Why it's the reference | Line profile |
|---|---|---|
| `commercial/` | Fully layered subsystem (models/schemas/audit/state_machines/entitlements/thin routers/bootstrap). Self-contained dependency island. | 27 files + routers/ + billing/ |
| `offboarding/` | Most layered domain: router/service/repository/models/schemas/state_machine/rbac/events/jobs. | `services.py` 1,121; `repository.py` 266 |
| `active_time/` | `engine.py` (487, **zero db.query** — pure computation) + service + config + worker. Textbook engine/library. | 4 files |
| `intelligence/` | Thin router over multiple engine modules (summarizer/workflow/burnout/focus/risk). | router 291 (2 db.query) |
| `proof_of_work/` | routes + validation/scoring services + pipeline. | 6 files |
| `integrity/`, `attendance_policy/`, `breaks/governance`, `storage/`, `settings/scope.py` | Genuine reusable engines with no HTTP coupling. | small, pure |
| `payroll/`, `screenshots/`, `wec/` | Have real service + schemas layers. | layered |

These validate the target — the work is making the rest look like these.

---

## 5. Modules to MERGE

| Merge | Into | Reason |
|---|---|---|
| `org/` + `worker_settings/` + `workspace/`'s settings surface | a unified **Settings/Config module** on top of `settings/scope.py` | Four packages own "settings"; `workspace/router.py:1042` imports `settings/router.py`'s **private** `_all_settings`/`DESCRIPTIONS`. One config engine, typed + cascade. |
| `billing/` + `commercial/billing/` + `commercial/subscription_ops` | one **Commercial/Billing subsystem** (String-UUID, the newer design) | Two subscription systems (`BillingSubscription` "default" vs `CommercialSubscription` per-tenant), two invoice tables (Float dollars vs Int cents), acknowledged in-code as "collapse once legacy migrates". |
| `wec/` + `proof_of_work/wec_routes.py` | one **Work-Evidence** module | Overlapping domain/naming for work-evidence capture. |
| `auth/service.py`'s router + DI + JWT + logic | split then re-home: DI/deps → `common/deps`, engine → `identity` service, routes → `auth/router.py` | Router, dependency injection, JWT decode, and business logic all in one 1,357-line "service" file; `auth/router.py` is a 17-line stub. |
| 6 audit tables + 5 governance tables | one **Audit/Event module** with a unified append-only store + optional hash-chain | `audit_logs` + `work_evidence_chain` + `work_events` + `billing_audit_events` + `commercial_events` + `offboarding_audit_events`, three keying schemes, two hash-chain impls. |

---

## 6. Modules to SPLIT (too large / mixed concerns)

| Split | Current | Target |
|---|---|---|
| `employees/router.py` (**4,044 lines, 79 db.query**) | roster + member CRUD + live status + time-adjustments + active-time display all in one router | `identity`/`roster` module: router → service → repository; move computation to existing engines |
| `leave/router.py` (**2,508, 41 db.query**, empty `service.py`/`schemas.py` stubs) | everything in the router | fill the already-intended service/schema layer |
| `main.py` (**2,466**) | embeds `/api/app-analytics` (~435 lines), `/api/productivity-insights` (~320), `/api/track`, `/api/activities`, `/api/audit-logs` as inline handlers with direct db.query | move analytics → `intelligence`/`reports` services; tracker ingest → `tracking` service; keep main.py to wiring/middleware/lifespan only |
| `reports/router.py` (1,890, 42 db.query), `super_admin/router.py` (1,377, 41), `org/router.py` (1,084, 36), `workspace/router.py` (1,674, 37) | router-owns-DB monoliths | introduce service + repository per module |
| `database/models.py` (**1,936, one file for the whole platform**) | monolithic | per-domain model modules under a `models/` package |
| Frontend `Dashboard.jsx` (9,757), `AdminDashboard.jsx` (7,403), `SettingsPage.jsx` (2,973), etc. | monster pages | feature-folder decomposition (§12) |

---

## 7. Modules to DELETE (confirmed dead)

| Delete | Evidence |
|---|---|
| `app/attendance/` (router/service/schemas all **0 lines**) | Empty scaffold, never included in `main.py` |
| `app/timesheets/` (router/service/schemas **0 lines**; `time_tracking.py` 208 lines **unimported**) | Entire package dead, never in `main.py` |
| `app/employees/service_old.py` (218) | No importers |
| `app/ai/analytics.py` (0) | Empty |
| ORM models `Tenant` (models.py:1835), `Workspace` (1859), `Membership` (1887) | **Zero references** anywhere; speculative multi-tenant scaffold never wired. Live tenancy is `Organization` + `User.organization_id` |
| Repo cruft: `tmp_release/` (78 MB `.exe` build artifact), stray 0-byte `npm` file, stray 0-byte `|` file, empty `workers/`+`infrastructure/`+root `database/` `.gitkeep` stubs | Build artifacts / shell-redirect accidents / misleading empty dirs |

⚠️ **Decision required before deleting `Tenant`/`Workspace`/`Membership`:** if true multi-workspace-per-org is on the product roadmap, these become the *starting point* to wire up rather than delete. If not, delete them. This is a Phase-3 gating decision (flagged in the roadmap).

---

## 8. Database Redesign

### 8.1 The canonical identity spine (the single most important decision)

Today there are **five org/tenant/workspace concepts**:
- `Organization` (Int PK, 5 bare columns) — the de-facto anchor (~40 tables FK to it).
- `OrgSettings` (Int FK) — holds the *real* org columns (name, logo, country, tz, currency, branding, HR email).
- `CommercialOrganization` (String-UUID PK) — parallel org, links via **nullable** `operational_org_id`.
- `Tenant` / `Workspace` / `Membership` (Int) — **dead scaffold**.

**Recommended canonical model:**
```
Organization (Integer PK — the anchor)         ← merge OrgSettings columns INTO this
  ├─ 1:1  CommercialProfile (billing/plan/commercial extension, keyed by organization_id FK)
  ├─ 1:N  Department  (NEW table)
  │         └─ 1:N Team  (add department_id FK; keep org_id)
  │                 └─ 1:N User (add team_id + department_id FKs; keep organization_id)
  └─ 1:N  everything else (org_id Integer FK, ON DELETE CASCADE or soft-delete)
```
- **Merge** `OrgSettings` → `Organization` (or make `OrgSettings` the canonical record and demote `organizations` — either way, one org record).
- **Reconcile** `CommercialOrganization` to a 1:1 extension keyed by the Integer `organization_id` (drop the String-UUID-as-identity; keep a `commercial_ref` if the provider needs a string).
- **Delete** `Tenant`/`Workspace`/`Membership` (or adopt — §7 decision).
- **Add** `Department` table + `department_id`/`team_id` FKs on `User` and `Team`. Migrate `User.team` **string** → FK; migrate `User.department` **string** → FK.

### 8.2 Keep / Merge / Split / Delete summary

| Domain | Keep | Merge | Split | Delete |
|---|---|---|---|---|
| Org/tenancy | `Organization` (as anchor) | `OrgSettings`→org; `CommercialOrganization`→1:1 ext | — | `Tenant`, `Workspace`, `Membership` |
| Identity | `User`, `UserSession`, `RefreshToken`, `UserPreferences` | — | — | — |
| Team/Dept | `Team` | — | add `Department`; add `team_id`/`dept_id` FKs | — |
| Settings | `AppSettings` (dynamic keys), `UserPreferences` | typed `security_policies`/`integration_connections`/`notification_settings` (out of AppSettings+AuditLog) | — | AuditLog-as-config usage |
| Permissions | — | — | add `roles` + `permissions` + `role_permissions` + `user_roles` | (none exist) |
| Audit | one unified append-only store (+ optional hash chain) | 6 logs → 1; 2 outbox → 1 | — | redundant logs |
| Billing | `CommercialSubscription`/`CommercialInvoice` (cents) | fold `billing_*` "default" tables in | — | duplicate `BillingInvoice`(dollars), `FraudEvent` vs `FraudSignal` dupe |
| Tracking | `ClockSession`, `Activity`(metadata), scores, `WorkRecord` | — | move `Activity.thumbnail` base64 + `User.profile_picture` base64 → object storage; add real `screenshots` reference table | — |
| Leave/Holiday/Payroll | all (cleanest domain) | — | — | — |
| New tables | — | — | — | add: `feature_flags`, `api_keys`, `webhook_subscriptions`, `integration_connections`, `departments` |

### 8.3 Schema management (critical)
Schema is managed **three ways today**: Alembic (49 migrations, ~20 reactive `add_organization_id_to_*` retrofits + a merge-head + "add missing" patches), boot-time `ensure_tables()`/`create_all(checkfirst=True)` in `main.py` lifespan, and `ADD COLUMN IF NOT EXISTS` ALTERs. This guarantees drift (documented in `runbooks/recovery/commercial-tables-migration.md`).
**Recommendation:** Alembic as the *only* source of truth. Delete boot-time table creation. Add a CI check that model metadata == migration head. Split per-domain model files so autogenerate is trustworthy.

---

## 9. Permission Redesign

**Current:** `User.role` free-text String (employee/hr/admin/super_admin); authZ is scattered `role in (...)` checks with **four different admin-role sets** across modules; the only real matrix is `offboarding/rbac.py` — Python **enums** (13 roles, `Permission` enum, `PERMISSIONS` matrix), **not persisted**, used only by offboarding.

**Recommended: RBAC core + resource-scoped ABAC, persisted, one enforcement point.**
- **Tables:** `roles`, `permissions`, `role_permissions`, `user_roles` (org-scoped; support custom roles per org).
- **Model:** RBAC for the base grant (role → permission set), plus ABAC attributes for resource scope (own vs team vs department vs org) and separation-of-duties (an admin can't grant a role they don't hold).
- **Enforcement:** a single `require_permission(perm, scope)` FastAPI dependency in `common/deps` — replaces every ad-hoc `role in (...)`. Promote `offboarding/rbac.py`'s matrix to the platform matrix; keep the legacy-string → role mapping during migration.
- **Policy layer (optional, later):** if entitlement/feature gating gets complex, introduce a policy engine (OPA-style) — but a persisted RBAC+ABAC matrix covers enterprise needs without it initially.

---

## 10. Configuration Redesign

**Current:** settings live in **three patterns** — typed tables (`OrgSettings`, `PayrollConfig`, `LeavePolicy`…), `AppSettings` key/value EAV Text bag (tracker config, `security.*`, `integrations.state_json`, custom roles), and `AuditLog` payloads used as config (worker_settings, notifications). The cascade (`system→org→team→employee`) is good but only covers `AppSettings` keys, is uncached, and has no Department tier.

**Recommended: one Configuration Engine, hybrid typed + dynamic.**
- **Keep `AppSettings`** for genuinely dynamic/extensible keys (tracker flags, engine tunables) — it's the right tool there.
- **Promote structured config to typed tables** (`security_policies`, `integration_connections`, `notification_settings`) — no more security posture in a Text bag, no more config in the audit log.
- **One inheritance resolver** for both: extend `settings/scope.py` to 5 levels (`system→org→department→team→user`), resolve by FK (not the `team_key` string), and back it with a **cache** (Redis) keyed by scope tuple, invalidated on write (the WS push already exists).
- **Typed schema registry:** each config key declares type/validation/default/scope-overridability once (the `KEY_META` idea, generalized) so the admin UI and the validator share one definition.

---

## 11. Commercial Redesign

**Current:** `commercial/` is the **best-architected subsystem** — fully layered, its own models/state-machines/entitlements/bootstrap, a dependency island importing only itself + `database`. But it (a) uses a String-UUID identity that only nullably links to the real `Organization`, and (b) coexists with the dead-simple legacy `billing_*` "default"-keyed tables.

**Is it isolated? Yes.** **Can it become a standalone service? Yes — it's the one clean extraction candidate.**
- **Now (Phase 3–5):** reconcile its org identity to the Integer `organization_id` (1:1 extension), collapse the legacy `billing_*` tables into it, and keep it as an in-monolith module.
- **Later (if scale/compliance demands):** extract `commercial/` as a standalone billing service — it already has clean boundaries, provider webhooks, idempotency, and its own event taxonomy (`CommercialEvent`). Communicate via the event bus + a thin internal API. **Don't extract before the identity reconciliation** — you'd export the String/Integer mismatch across a network boundary.

---

## 12. Frontend Redesign

**Current:** ~81k lines, **no TypeScript**, type-based folders, **no design system**, **no server-state library**. `Dashboard.jsx` 9,757 + `AdminDashboard.jsx` 7,403 dominate; **3 axios instances + 171 raw `fetch` calls** (only the axios client has 401-refresh/dedup); `Modal` reimplemented ≥9×, `Button` ≥6×, `Toggle`/`Switch` ≥8× (two names for one control); state = 5 Contexts + manual `useState`/`useEffect`; Zustand exists only inside KAIROS.

**Recommended target:**
1. **Design system package** — one `components/ui/` (or a workspace package): `Button`, `Modal`, `Toggle`, `Card`, `Pill`, `Tabs`, `Field`, all theme-aware + ARIA-complete. Delete the 6–9× duplicates. (Existing `offboarding/primitives.jsx` is the seed.)
2. **One API client + server-state library** — collapse the 3 axios instances + raw fetches into a single client with interceptors; adopt **React Query** (or SWR) for caching, dedup, refetch, and 401 handling everywhere (kills the manual `useState`/`useEffect` fetch pattern and the refetch-churn bugs from the settings audit).
3. **Feature-based folders** — `features/<domain>/{pages,components,hooks,api}` instead of type-based `pages/`+`components/`. Co-locate.
4. **Split the monster pages** — Dashboard/AdminDashboard/SettingsPage into feature routes + lazy tabs (the intra-page `lazy()` pattern already exists — extend it).
5. **Real hooks layer** — a `hooks/` dir for reusable logic (`useDebounce`, data hooks) instead of inlining.
6. **Finish theme migration** — remove the ~5 files still hardcoding local `C` palettes; everything through `useTheme()`.
7. **TypeScript** — long-term, incremental (start at the API client + design system). Not a Phase-3 blocker.

---

## 13. Backend Redesign

1. **Enforce `router → service → repository`** across the monolith modules (`employees`, `leave`, `reports`, `org`, `super_admin`, `workspace`). Routers do HTTP only; services hold logic; repositories hold the 489 `db.query()` calls. Use the layered modules (`commercial`, `offboarding`, `active_time`) as templates.
2. **Create `common/`** — `deps.py` (move `get_current_user`/`require_permission`/`get_db` out of `auth/service.py`), `errors.py` (a generic 500 handler; today only DB errors are handled), `pagination.py`, `responses.py`. Auth deps being in a 1,357-line "service" file makes `auth.service` an accidental hub every router imports.
3. **Thin out `main.py`** — move the ~435-line `/api/app-analytics`, ~320-line `/api/productivity-insights`, `/api/track`, `/api/activities`, `/api/audit-logs` inline handlers into their domain services. `main.py` = wiring + middleware + lifespan only.
4. **Split `database/models.py`** into a `models/` package by domain (identity, org, tracking, leave, payroll, billing, audit…).
5. **Introduce the infra layer** — Redis client, Celery (broker=Redis), a real worker service; move the 7 in-process `run_forever` loops to Celery beat / Cloud Run Jobs (the offboarding job was *already disabled* because of the duplicate-per-worker problem). Move WS to a Redis-pub/sub backplane.
6. **Wire the event bus** — `OutboxEvent` already exists; add a consumer that publishes domain events to Redis/queue for async fan-out (notifications, audit, integrations, webhooks).
7. **API versioning + consistency** — standardize on `/api/v1`, deprecate the duplicate SSO/holiday/settings surfaces, consistent pagination/validation. Note `employees_router` is currently mounted **twice** (`main.py:999`).

---

## 14. Scalability Risks (Section 9)

| Target | Supported? | Blocker |
|---|---|---|
| 100 orgs | ✅ | Fine (except billing globally shared until fixed) |
| 1,000 orgs | ⚠️ | Needs billing fix + settings cache; no-RLS fragility |
| 10,000 orgs | ❌ | 3-regime data model + String commercial keys + no RLS won't hold; needs canonical model |
| 100,000 employees | ❌ | (1) uncached settings cascade on the 60s tracker poll; (2) base64 blobs in `activities`/`users`; (3) config read by scanning `audit_logs`; (4) synchronous screenshot processing inside the upload request |
| Millions of screenshots/day | ❌ | Screenshot processing is **synchronous in the request** (`main.py` `/api/track`); no queue. Base64 `Activity.thumbnail` bloats the hottest table. Needs object storage + async pipeline |
| Millions of activities | ⚠️ | Table design OK, but blob column + no partitioning/archival strategy |
| Millions of audit records | ⚠️ | 6 unpartitioned audit tables; config-in-audit-log makes reads scale with audit volume |
| 100 concurrent admins | ✅ | Read-mostly; watch frontend refetch churn |
| 500 concurrent trackers | ❌ **for real-time/jobs** | **In-process WebSocket rooms** (no backplane) → broadcasts miss clients on other workers; **in-process schedulers duplicate per Gunicorn worker**. The system is effectively **single-process for real-time and scheduled work** — the hard ceiling. 500 (or 100k) trackers require Redis pub/sub + a worker service |

**Top 4 bottlenecks:** (1) single-process real-time/jobs (no Redis/queue backplane); (2) synchronous screenshot ingest + base64-in-row; (3) uncached, no-Department settings cascade on the hot poll path; (4) five-headed org model + string-keyed billing blocking real multi-tenant scale.

---

## 15. Technical Debt (ledger)

| Debt | Location | Severity |
|---|---|---|
| Triple schema management (Alembic + boot `ensure_tables`/`create_all` + `ADD COLUMN IF NOT EXISTS`) | `main.py` lifespan + `backend/alembic/` | **High** |
| 489 `db.query()` in 28 routers (no service/repo layer in the monolith half) | `employees`, `leave`, `reports`, `org`, `workspace`, `super_admin` | **High** |
| 1,936-line single model file; 3 PK/tenancy regimes | `database/models.py` + commercial + offboarding | **High** |
| 6 audit logs, 2 outbox, 2 subscription systems, 2 invoice tables, 2 fraud tables | cross-module | **High** |
| Dead code: `attendance/`, `timesheets/`, `service_old.py`, `ai/analytics.py`, `Tenant/Workspace/Membership` models | backend | Medium |
| ~20 reactive `add_organization_id_to_X` migrations + merge-head + "add missing" patches | `alembic/versions/` | Medium |
| Empty `workers/`/`infrastructure/`/root `database/` stubs misrepresenting architecture | repo root | Medium |
| Committed build artifact (78 MB `.exe`) + stray `npm`/`|` 0-byte files | repo root | Medium |
| 3 axios instances + 171 raw fetches; no design system (Modal ×9 etc.); no server-state lib | frontend | **High** |
| Monster components (Dashboard 9.7k, AdminDashboard 7.4k) | frontend | Medium |
| `auth/service.py` = router + DI + JWT + logic in one 1,357-line file | `auth/` | Medium |
| `employees_router` mounted twice; inconsistent router prefixes forcing an SPA-collision middleware | `main.py` | Low |
| Config stored in `AppSettings` Text bag + `AuditLog` payloads | settings/worker_settings/notifications | Medium |

---

## 16. Architecture Decision Records (ADRs)

**ADR-001 — Modular monolith, not microservices (yet).**
*Context:* two half-architectures; scale pressure. *Decision:* enforce internal module boundaries (`router→service→repo`) in one deployable; defer microservices. *Consequence:* fastest path to maintainability; keeps one DB/transaction boundary while tenancy is fixed. *Revisit* when a module needs independent scale/compliance (candidate: `commercial`).

**ADR-002 — Canonical Integer-PK organization spine.**
*Decision:* `Organization`(Int) is the anchor; merge `OrgSettings` in; reconcile `CommercialOrganization` to a 1:1 Integer-keyed extension; add `Department`; convert `User.team`/`User.department` strings → FKs. *Consequence:* one identity, real FKs, RLS-able. *Migration risk: High* — dual-write + backfill + verify.

**ADR-003 — Delete the dead multi-tenant scaffold (`Tenant`/`Workspace`/`Membership`)** unless multi-workspace-per-org is a committed product goal. *Consequence:* removes confusion; if adopted instead, it becomes the workspace layer under Organization.

**ADR-004 — Alembic is the single schema source of truth.** *Decision:* remove boot-time `ensure_tables`/`create_all`/`ADD COLUMN IF NOT EXISTS`; CI asserts model==head. *Consequence:* ends drift; migrations must be disciplined.

**ADR-005 — Introduce Redis as cache + WS backplane + Celery broker; add a worker service.** *Decision:* move `run_forever` loops to Celery beat / Cloud Run Jobs; WS to Redis pub/sub. *Consequence:* unlocks horizontal scale; adds an infra dependency (already optional in `requirements.txt`).

**ADR-006 — All binary blobs in object storage.** *Decision:* `Activity.thumbnail` + `User.profile_picture` base64 → Supabase/S3-compatible storage; DB stores references; add a real `screenshots` reference table; screenshot processing moves to an async pipeline. *Consequence:* shrinks hottest tables; enables millions/day.

**ADR-007 — Persisted RBAC + resource-scoped ABAC, one enforcement point.** *Decision:* `roles`/`permissions`/`role_permissions`/`user_roles` tables + `require_permission()` dependency; promote `offboarding/rbac.py`. *Consequence:* enterprise authZ, custom roles, separation-of-duties.

**ADR-008 — One Configuration Engine (typed + dynamic) with a 5-level cascade + cache.** *Decision:* keep `AppSettings` for dynamic keys; move structured config to typed tables; extend cascade to include Department; back with Redis cache. *Consequence:* no config in Text bags or audit logs; queryable, fast.

**ADR-009 — Unified Audit/Event store.** *Decision:* collapse 6 audit logs + 2 outboxes into one append-only store (+ optional hash chain); one outbox → event bus. *Consequence:* single queryable trail; drives async fan-out.

**ADR-010 — Frontend: design system + React Query + feature folders.** *Decision:* one `components/ui/`, one API client with React Query, feature-based structure; split monster pages; finish theme migration; TS incrementally. *Consequence:* kills duplication and refetch bugs; smaller bundles.

---

## 17. Recommended Phase 3 Design (the platform foundation)

Phase 3 from the roadmap = "Platform Foundation." Concretely, in dependency order:

**3.1 Schema single-source-of-truth (prereq for everything).** Remove boot-time table creation; reconcile Alembic with reality; split `models.py` per domain; CI model==head check. *(~1–2 wks)*

**3.2 Canonical organization spine.** Merge `OrgSettings`→`Organization`; reconcile `CommercialOrganization` to Integer 1:1; delete-or-adopt `Tenant/Workspace/Membership`; add `Department`; migrate `User.team`/`department` strings → FKs. Dual-write + backfill + verify + cutover. *(~4–6 wks, High risk)*

**3.3 Tenant isolation.** Add Postgres RLS (or a mandatory `org_scoped` query dependency) so a forgotten filter can't leak. Fix commercial/offboarding org keying to the Integer FK. *(~1–2 wks)*

**3.4 Configuration engine.** 5-level cascade (add Department) resolved by FK; typed config tables for security/integration/notification; Redis cache with write-invalidation; typed key registry. *(~3 wks)*

**3.5 Shared backend layers.** `common/` (deps/errors/pagination); enforce `router→service→repo` starting with `employees` and `leave`; thin `main.py`. *(~3–4 wks, incremental)*

**3.6 Infra baseline.** Redis + Celery + worker service; move schedulers off in-process loops; WS Redis backplane; object storage for blobs + async screenshot pipeline. *(~3–4 wks)*

**3.7 Organization templates.** On top of the config engine — apply a preset config map to a scope (the platform's first reuse of the unified engine). *(~2 wks)*

**Exit criteria for Phase 3:** one org identity with FKs + RLS; schema has one source; config engine with Department tier + cache; Redis-backed real-time/jobs; `common/` layer + two monolith modules refactored as the proof pattern; no dead scaffold. **Only then start Phase 4 enterprise features** (feature flags, API keys, webhooks, integrations) — they consume this foundation.

---

## 18. Final Architecture Readiness Score

**Current: 38 / 100.**

| Milestone | Score |
|---|---|
| Today | 38 |
| After Phase 3.1–3.3 (schema + org spine + isolation) | ~55 |
| After Phase 3 complete (config engine, common layer, infra baseline) | **~70 — foundation sound** |
| After Phase 4–5 (enterprise + SaaS features on the foundation) | ~80 |
| After Phase 6–7 (perf + frontend redesign + TS) | **~86 — enterprise-grade platform** |

**Bottom line:** The platform is not enterprise-grade today, but it is **not a rewrite** — it's a consolidation. The clean modules (`commercial`, `offboarding`, `active_time`) prove the team can hit the target; half the codebase already looks right. The decisive work is Phase 3: **one org identity, one schema source, one config engine, and a real infra layer (Redis + workers + object storage).** Do that before building enterprise features, and the next 2–3 years compound on a sound base instead of fighting the five-headed org model, the triple schema management, and the single-process ceiling.

**The one rule that protects the investment:** *no Phase 4 feature merges until the Phase 3 exit criteria are met.* Feature flags, API keys, webhooks, and SAML built on today's foundation would all be reworked when the foundation is corrected — sequence to avoid it.

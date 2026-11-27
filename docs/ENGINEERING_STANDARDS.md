# Temveris — Engineering Standards & Governance Manual

**Status:** Mandatory for every contributor (employee, contractor, AI agent). This is the official engineering handbook.
**Relationship to other docs:** the [Master Spec](PLATFORM_MASTER_SPEC.md) says *what* to build, the [Roadmap](ORG_SETTINGS_ROADMAP.md) *in what order*, the [Execution Guide](PLATFORM_EXECUTION_GUIDE.md) *how to ship it*, the [PI Plan](PLATFORM_PI_PLAN.md) *tracks* it — **this manual defines the quality bar every line of code must meet.**
**Grounding:** the "❌ anti-pattern" examples below are real conditions found in the codebase audits ([ORG_SETTINGS_AUDIT.md](ORG_SETTINGS_AUDIT.md), [PLATFORM_ARCHITECTURE_AUDIT.md](PLATFORM_ARCHITECTURE_AUDIT.md)). They are prohibited going forward and are being migrated out.
**Enforcement:** standards marked **[CI]** are machine-enforced and block merge. Standards marked **[Review]** are enforced by human review (checklist §13). "SHOULD" = strong default, deviations justified in the PR; "MUST" = no exceptions without an ADR.

---

## 1. Engineering Principles (Section 1)

| Principle | What it means at Temveris |
|---|---|
| **Clean Architecture** | Dependencies point inward: `router → service → repository → models`. HTTP concerns never leak into services; DB concerns never leak into routers. **❌ Today:** 489 `db.query()` calls sit in 28 routers — that is the pattern we are eliminating, not extending. |
| **SOLID** | *S*: one module = one bounded context (§ Master Spec §4). *O*: extend via new services/adapters, not by editing shared engines. *L*: repository/adapter implementations are substitutable (real vs test). *I*: thin service interfaces per context, not god-objects (**❌ `employees/router.py` at 4,044 lines**). *D*: depend on the `require_permission`/`get_db` abstractions, not concrete internals. |
| **DRY** | One config engine, one permission check, one audit trail, one API client, one design-system primitive. **❌ Today:** `Modal` reimplemented ≥9×, `Toggle`/`Switch` ≥8×, SSO written in two places, 6 audit tables. Reuse the engine; never fork it. |
| **KISS** | Prefer the simplest thing that works. No speculative frameworks. A key/value setting stays key/value; a typed policy becomes a typed table — don't build a plugin system for either. |
| **YAGNI** | Don't build for imagined scale/features. **❌ Today:** `Tenant`/`Workspace`/`Membership` models were built and never wired — dead scaffolding. Build when the story exists, behind a flag. |
| **Separation of Concerns** | Ingest ≠ scoring ≠ reporting ≠ notification. `main.py` holds wiring only (**❌ Today: `main.py` embeds a 435-line analytics handler**). Screenshot processing is a worker job, not part of the upload request. |
| **Dependency Injection** | Use FastAPI `Depends` for `get_db`, `get_current_user`, `require_permission`. DI lives in `common/deps` — **❌ not** buried in a 1,357-line `auth/service.py`. Tests inject fakes; no global singletons for stateful deps. |
| **Composition over Inheritance** | Compose engines (config resolver + cache + registry) rather than deep class hierarchies. Frontend: compose design-system primitives, don't subclass. |
| **Event-Driven Design** | Cross-context side effects (audit, notifications, integrations, webhooks, AI) go through the **transactional outbox → event bus**, never synchronous in-request fan-out. `OutboxEvent` exists — use it. |
| **Backward Compatibility** | Additive-first. The desktop tracker and browser extension run old versions for months — **never** remove an endpoint they call until telemetry proves zero traffic. New config keys default safely for old clients. |

---

## 2. Repository Standards (Section 2)

**Structure.** Backend: `backend/app/<domain>/` each with `router.py · service.py · repository.py · schemas.py · models.py` (+ `engine.py`/`config.py` for engines). Shared: `backend/common/` (deps, errors, pagination, responses), `backend/database/` (session), `backend/alembic/`. Frontend: `frontend/src/features/<domain>/{pages,components,hooks,api}` + `components/ui/` (design system) + `lib/` + `hooks/`.

**Naming.** Python: `snake_case` modules/functions/vars, `PascalCase` classes, `UPPER_SNAKE` constants. React: `PascalCase` components/files, `camelCase` hooks (`useX`), `kebab-case` non-component files. DB: `snake_case` tables (plural) + columns. No abbreviations that aren't domain-standard.

**Size limits [Review, moving to CI]:**
- **File MUST be < 800 lines; SHOULD be < 400.** (**❌ Today:** `Dashboard.jsx` 9,757; `employees/router.py` 4,044; `main.py` 2,466 — all flagged for split.)
- **Function MUST be < 60 lines; SHOULD be < 40.** (**❌** the 435-line inline analytics handler.)
- **Class/component SHOULD be < 300 lines** and have one responsibility.
- A router file that holds business logic or `db.query()` is over-limit *regardless of line count*.

**Modules/packages.** One bounded context per package; no cross-context imports of another package's `router`/private functions (**❌ `workspace/router.py` imports `settings/router.py`'s private `_all_settings`**). Cross-context = service interface or event.

**Repo hygiene [CI].** No build artifacts committed (**❌ `tmp_release/` 78 MB `.exe`**), no stray files (**❌ 0-byte `npm` and `|` files**), no empty "aspirational" dirs (**❌ `workers/`, `infrastructure/`, root `database/` `.gitkeep` stubs**). No secrets (**❌ committed `backend/.env`** — secret scanner blocks this).

---

## 3. Backend Standards (Section 3)

- **FastAPI layering [Review→CI].** `router` = routing, auth dependency, request/response schemas, HTTP status. **No `db.query()` in routers.** `service` = business logic, transactions, orchestration. `repository` = all DB access. `main.py` = app assembly, middleware, lifespan only.
- **Routers.** One router per domain, versioned prefix `/v1/<domain>`. Every mutating route declares `Depends(require_permission(...))`. No route > 60 lines — delegate to a service.
- **Services.** Stateless; take a `Session` (or repository) via DI; own the transaction boundary (one `commit` per use-case); raise typed domain exceptions, never `HTTPException` (routers translate).
- **Repositories.** The only place `db.query()`/SQL lives; every tenant query is org-scoped (RLS + explicit filter). Return domain objects/DTOs, not raw rows to the router.
- **DTOs/Schemas.** Pydantic models for all request/response bodies; separate input vs output schemas; never expose ORM models directly. Validate at the boundary.
- **Models.** SQLAlchemy per-domain under a `models/` package (**❌ not** one 1,936-line file). Integer surrogate PKs; `organization_id` FK on tenant tables; no blobs in-row.
- **Exceptions.** Domain exceptions in each module; central handlers in `common/errors` map to HTTP; a **generic 500 handler MUST exist** (today only DB errors are handled). Never `except: pass` around audit/security writes (**❌ current pattern** swallows audit failures).
- **Logging.** Structured JSON; **correlation id on every log line**, propagated across api→worker→realtime; never log secrets/PII/tokens; log level by env.
- **Validation.** Server-side always; typed against the config key registry for settings; never trust client-supplied `role`/`organization_id` (**❌ privilege-escalation risk**).
- **Dependency Injection.** `get_db`, `get_current_user`, `require_permission` from `common/deps`. Identity cache invalidated on role change/deactivation (**❌ today: 5-min stale window**).
- **Configuration.** All config via the configuration engine or env; secrets from the secret manager, never inline. No magic numbers — named constants.
- **Caching.** Redis; cache effective settings/entitlements by scope tuple; **explicit invalidation on write**; every cache entry has a TTL and a documented invalidation trigger.
- **Background jobs [MUST].** Celery workers, not in-process `asyncio` loops (**❌ today: 7 `run_forever` loops that duplicate per Gunicorn worker**). Jobs are **idempotent**, resumable, batched; no long-held table locks; screenshot processing is a job, not part of the request.

---

## 4. Frontend Standards (Section 4)

- **React + TypeScript [target].** New code in TS; incremental migration of existing `.jsx`.
- **Components.** Functional; < 300 lines; single responsibility; presentational vs container separation. Use the shared **`components/ui/`** design system — **❌ do not** reinvent `Modal`/`Button`/`Toggle` per feature.
- **Hooks.** Reusable logic in `hooks/` (`useDebounce`, data hooks). No business logic inline in components. Custom hooks prefixed `use`, obey the rules of hooks.
- **State management.** **Server state via React Query** (caching, dedup, refetch, 401) — **❌ not** manual `useState`+`useEffect`+`fetch`. Client/UI state via Context or local state. Memoize callbacks passed as props (**❌ non-memoized `showToast` caused refetch churn**).
- **API layer [MUST].** **One API client** with interceptors (auth, 401-refresh, dedup). **❌ Prohibited:** raw `fetch` in components (171 exist today), per-component `authGet/authSend`, multiple `axios.create` instances (3 today). All calls go through the one client.
- **Feature modules.** `features/<domain>/` co-locating pages/components/hooks/api. Route- and tab-level code-splitting (`React.lazy`).
- **Routing.** Centralized; route guards by permission (not raw role-string reads); lazy-loaded routes.
- **Forms & validation.** Client validation for UX + server validation for truth; disable-on-submit; show inherited/override/locked state for settings; confirm destructive actions consistently.
- **Accessibility [Review].** WCAG 2.1 AA: semantic HTML, `aria-*` on custom controls, keyboard nav, focus management, labels. **❌ Today: `SettingsPage.jsx` has zero `aria-`/`role=` in 2,973 lines** — the floor is that every interactive primitive is accessible.
- **Styling.** One theme system via `useTheme()`; support light+dark; **❌ no** hardcoded local palettes (`AccountSettings.jsx` renders light-only today). Responsive; admin console desktop-first but no horizontal body scroll.

---

## 5. Desktop Tracker Standards (Section 5)

- **Electron.** Keep the strong security model: refresh token in main-process OS keychain (`safeStorage`), never exposed to renderer; context isolation on; no `nodeIntegration` in renderer.
- **IPC.** Only via `contextBridge`/`preload`; typed, minimal surface; renderer never gets raw tokens or fs access.
- **Offline mode [target].** Capture buffers locally when offline; queue with bounded size; never lose data silently — surface degraded state (as it does for the missing input hook → `LIMITED_TRACKING`).
- **Sync engine.** Batched, retried-with-backoff uploads (3 retries today); idempotent server ingest (dedupe by client event id); resumable large uploads.
- **Auto-update.** Signed artifacts; staged rollout; never force-break an old version mid-session.
- **Capability negotiation [MUST].** The tracker declares its version/capabilities; the server adapts. New config keys **default safely** for trackers that don't understand them. **❌ No hard cutovers** that assume all trackers upgraded.
- **Backward compatibility [MUST].** `/settings` and ingest endpoints stay compatible until telemetry shows zero old-version traffic. Server-negotiated behavior, not version assumptions.

---

## 6. Database Standards (Section 6)

- **Alembic is the single source of truth [CI].** **❌ Prohibited:** boot-time `create_all`/`ensure_tables`/`ADD COLUMN IF NOT EXISTS` (all three exist today — being removed). CI asserts model metadata == migration head. Migrations run as a gated deploy job, never at app boot.
- **Naming.** `snake_case`, plural tables; FKs `<entity>_id`; indexes `ix_<table>_<cols>`; uniques `uq_<table>_<cols>`; explicit constraint names.
- **Indexes.** Every FK and every high-selectivity filter column indexed; `organization_id` indexed on every tenant table; create `CONCURRENTLY` in prod. Composite index order matches query predicates.
- **Foreign keys [MUST].** Real FKs, correct types. **❌ Prohibited:** string tenancy keys (`billing.organization_id = "default"`), string membership (`User.team` name match) — use Integer FKs. External refs (Stripe ids) are attributes, not identities.
- **Constraints.** Enforce invariants in the DB (NOT NULL, unique, check) — don't rely on app code alone. Money as integer cents (**❌ not** Float dollars).
- **Transactions.** One use-case = one transaction, owned by the service; no partial commits; migrations are transactional where the DB allows.
- **Soft delete [MUST for tenant data].** Soft-delete + purge job; **❌ not** `ON DELETE SET NULL` on tenant FKs (today's pattern orphans rows to `organization_id = NULL`, leaking scope).
- **Audit tables.** One unified `audit_events` (org-scoped, correlation id, optional hash chain) — **❌ not** 6 parallel logs. High-volume tables (`activities`, `audit_events`, `screenshots`) time-partitioned with an archival policy.
- **Migrations.** Expand → backfill → dual-write → contract (Execution Guide §4); `upgrade` + `downgrade` both implemented and tested on prod-sized data; one logical change per migration.

---

## 7. API Standards (Section 7)

- **REST.** Resource-oriented nouns, plural collections; correct verbs/status codes; **no verbs in paths** for CRUD.
- **Versioning [MUST].** `/v1` prefix; never change a response shape in place — add a field or a new version; deprecate with `Deprecation`/`Sunset` headers + changelog.
- **Pagination.** Cursor-based for large/hot lists; consistent envelope (`{data, page_info}`); a hard max page size. **❌ No unbounded list endpoints.**
- **Filtering/Sorting.** Documented query params; whitelist filter/sort fields (no arbitrary column injection); parameterized (**❌ never** f-string SQL).
- **Bulk APIs.** Provide bulk create/update where the UI needs it (e.g. scoped-settings apply, role assignment); atomic or clearly partial-success with per-item results.
- **Error responses [MUST].** One shape: `{error: {code, message, details, correlation_id}}`; stable machine-readable `code`; never leak stack traces/SQL.
- **Idempotency.** Mutating external-effect endpoints (billing, webhooks-in) accept an idempotency key; provider webhooks deduped (as commercial does).
- **Rate limits.** Per-key/app/org tiers via Redis; applied platform-wide (**❌ today: only on auth routes**); `429` + `Retry-After`.
- **OpenAPI [CI].** The contract; every endpoint documented; SDKs generated from it; contract test in CI. No undocumented endpoints.

---

## 8. Security Standards (Section 8)

- **Authentication.** JWT access (short-lived) + rotating, reuse-detecting refresh tokens (keep current design); per-request `is_active` check; device-bound tracker tokens.
- **Authorization [MUST].** Every endpoint deny-by-default via `require_permission(permission, scope)` from `common/deps`. **❌ Prohibited:** ad-hoc `role in (...)` string checks (four inconsistent sets exist today). Separation of duties: cannot grant a permission you don't hold.
- **RBAC/ABAC.** Persisted roles/permissions; resource scope (`own/team/department/org/platform`); custom + delegated + temporary (expiring) grants.
- **MFA [MUST when enabled].** Real TOTP/WebAuthn enforced at login when org policy requires it (**❌ today: non-functional theater**); recovery codes + break-glass.
- **Secrets [CI].** Secret manager only; **never in git** (secret scanner blocks; the committed `.env` is the cautionary tale); rotation supported.
- **Encryption.** TLS in transit; KMS at rest; field-level encryption for PII/payroll/bank; screenshots access-controlled + signed URLs.
- **Input validation / output encoding.** Validate all input at the boundary; parameterized queries only; validate data-URL scheme/content for uploads (**❌ `profile_picture` base64 unvalidated today** → stored-XSS risk); encode output in the SPA.
- **Audit logging [MUST].** Every security-relevant mutation (login, role change, permission grant, settings change, SSO/MFA config) writes `audit_events`. **❌ Today: role change, org-settings save, team CRUD are silent** — that gap is closed.
- **Tenant isolation [CI].** RLS + explicit org-scoping; the tenant-isolation regression suite is a mandatory gate.

---

## 9. Testing Standards (Section 9)

| Type | Requirement |
|---|---|
| **Unit** | Pure logic (engines, services, permission checks, config resolver) — no DB; fast; runs every PR |
| **Integration** | `router→service→repository→DB` on ephemeral Postgres; every PR |
| **Tenant isolation [CI gate]** | Dedicated suite — no endpoint reads/writes cross-tenant; **mandatory** on any data-access change |
| **API/contract** | Endpoints vs OpenAPI; backward-compat check vs prior contract |
| **E2E** | Playwright on critical flows (login/MFA/SSO, settings override+reset, clock in/out, leave, billing); nightly + release |
| **Desktop** | Tracker smoke incl. old-version-vs-current-API compat; pre-release |
| **Load** | Ingest at target rates + config-resolve latency + WS fan-out; pre-release of R3/R5 |
| **Security** | SAST + dependency + secret scan every PR; DAST + pen test at R2/R5 |
| **Coverage [CI]** | ≥ 70% on core domains; **100% on billing, permissions, and auth paths**; coverage must not decrease below threshold |

Every migration ships a test exercising upgrade→backfill→read. No merge without the tenant-isolation case when data access changes.

---

## 10. Documentation Standards (Section 10)

- **ADRs [MUST].** Every significant/irreversible decision → an ADR (context, decision, alternatives rejected, consequences) in `docs/adr/`. The existing ADR-001…018 are the baseline; new decisions append.
- **README.** Every module has one: purpose, public interface, dependencies, how to test.
- **API docs.** OpenAPI is the source; developer portal renders it; examples for every endpoint.
- **Runbooks.** For every operational procedure (migration, rotation, incident, DR) in `runbooks/`; updated after every incident.
- **Release notes.** Per release: user-visible changes, deprecations, breaking changes, migration steps.
- **Migration notes.** Every migration documents expand/contract steps, backfill, verification, and rollback.

---

## 11. Observability Standards (Section 11)

- **Logging.** Structured JSON, correlation id, no secrets/PII, level by env, centralized.
- **Tracing.** OpenTelemetry across api→worker→realtime→ai; trace the ingest→score→report and login→authz paths.
- **Metrics.** RED (rate/errors/duration) per endpoint + business metrics (active trackers, screenshots/day, MRR, queue depth, ingest lag).
- **Dashboards.** Per-domain SLO dashboards + a program dashboard (§ PI Plan §12); every new hot path ships with its dashboard.
- **Alerts.** SLO-burn, queue backlog, ingest lag, DB saturation, failed migrations, webhook failures, auth anomalies; each alert links a runbook and routes to on-call.
- **Health checks [MUST].** Liveness + readiness per service; readiness gates traffic during startup/migration; synthetic checks for login, settings fetch, tracker ingest.

---

## 12. Performance Standards (Section 12)

| Concern | Standard |
|---|---|
| **API latency** | p95 < 300 ms; p99 < 800 ms; slow endpoints profiled before merge |
| **DB queries** | No N+1 (**❌ notifications does `actor_id.in_(all org users)` today**); index-backed; no full scans on hot paths; query budget per request reviewed |
| **Frontend rendering** | No unnecessary re-renders (memoize); route/tab code-split; initial route bundle budgeted; **❌ no** 9k-line single chunks |
| **Caching** | Hot reads (settings, entitlements) cached in Redis with invalidation; config resolve < 10 ms cached |
| **Memory** | No unbounded in-memory growth; blobs in object storage, **❌ not** base64 in rows; bounded queues/buffers |
| **CPU** | Heavy work (screenshot processing, exports, payroll) on workers, **❌ not** in the request path |

Performance-sensitive changes include a before/after measurement in the PR.

---

## 13. Code Review Checklist (Section 13) — every PR

- [ ] **Architecture** — correct bounded context; `router→service→repository`; no `db.query()` in routers; no cross-context private imports; file/function within size limits
- [ ] **Security** — `require_permission` (not `role in (...)`); no client-trusted `role`/`org_id`; secrets from manager; input validated; output encoded
- [ ] **Tenant isolation** — every query org-scoped/RLS-covered; isolation test added if data access changed
- [ ] **Performance** — no N+1; indexed queries; caching where hot; no blobs in-row; heavy work on workers; measurement if perf-sensitive
- [ ] **Tests** — unit + integration + contract as applicable; coverage maintained; 100% on billing/perm/auth paths touched
- [ ] **Backward compatibility** — additive; tracker/extension/API clients unaffected or gated + deprecation planned
- [ ] **Migrations** — expand/contract; reversible; dry-run noted; rollback stated
- [ ] **Observability** — logs+correlation id; metrics/traces for new hot paths; alerts for new failure modes
- [ ] **Documentation** — README/ADR/API docs/runbook updated as needed
- [ ] **Flags & rollback** — risky change flag-gated; rollback path stated
- [ ] **Approvals** — ≥1 (≥2 + domain owner for migrations/permissions/billing/auth)

---

## 14. Definition of Done (Section 14) — every feature

A feature is **Done** only when **all** hold:
1. **Code complete** — meets this manual; behind a flag if risky/incremental.
2. **Tests passing** — unit + integration + tenant-isolation + contract green in CI; coverage bar met.
3. **Documentation updated** — README/ADR/API docs/release+migration notes as applicable.
4. **Monitoring added** — logs, metrics, traces, and alerts for the new surface; dashboard updated.
5. **Rollback available** — flag kill-switch and/or previous-image path; expand/contract keeps prior version working.
6. **Performance verified** — meets §12 budgets; measured if perf-sensitive; load-tested for scale-critical features.
7. **Security reviewed** — authZ via `require_permission`; isolation verified; secrets clean; audit logging present; security scan clean.
8. **Deployed + smoke-verified on staging**, rollout plan (canary/cohort) defined.

Not Done: "works on my machine," untested migrations, stored-but-unenforced config, silent security mutations, flags never planned for retirement.

---

## 15. Technical Debt Policy (Section 15)

- **Identified.** Anyone can file debt (review comment, `# TODO(debt): …`, or a `debt` ticket). The audits seeded the initial ledger (Architecture Audit §15). A deliberate shortcut MUST be marked at the point of code with its ceiling and upgrade path (e.g. `# debt: single-instance WS until Redis backplane (S18)`).
- **Prioritized.** Scored by *impact* (correctness/security/scale/velocity) × *reach* (how many features it blocks) × *interest rate* (how fast it worsens). Security/isolation/data-integrity debt is P0 and jumps the queue.
- **Tracked.** One debt register (labeled tickets) reviewed at each PI planning; the register is visible on the program dashboard. Feature flags left on are debt and appear here until retired.
- **Resolved.** Every release reserves **~15–20% capacity** for debt paydown. Foundation debt (the audits' P0/P1 items) is scheduled explicitly into R1–R3, not "someday." New debt requires either a paydown plan or an ADR accepting it with an owner and a review date. **No new instance of an already-prohibited anti-pattern (§ this manual's ❌ list) may be introduced** — CI/review blocks it; the ledger only tracks the pre-existing instances being migrated out.

---

### Governance
This manual is versioned in `docs/`. Changes require an ADR + eng-lead approval. It is reviewed each PI. **Conformance is not optional:** CI gates enforce the `[CI]` standards; the §13 checklist enforces the `[Review]` standards; a PR that violates a MUST without an approving ADR is not merged. When this manual and existing code disagree, the code is migrated toward the manual — never the reverse.

**The through-line of every standard here:** each one either removes a real anti-pattern the audits found, or protects the foundation the roadmap is building. Follow them and Temveris compounds toward the 90/100 target; ignore them and it re-accumulates the exact debt this program exists to remove.

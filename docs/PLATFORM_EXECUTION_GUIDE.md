# Temveris — Enterprise Implementation Execution Guide

**Status:** Execution manual. Defines *how* engineers implement the finalized architecture — not *what* to build (that's the [Master Spec](PLATFORM_MASTER_SPEC.md)) and not *in what order* (that's the [Roadmap](ORG_SETTINGS_ROADMAP.md)).
**Source of truth:** [ORG_SETTINGS_AUDIT.md](ORG_SETTINGS_AUDIT.md) · [ORG_SETTINGS_ROADMAP.md](ORG_SETTINGS_ROADMAP.md) · [PLATFORM_ARCHITECTURE_AUDIT.md](PLATFORM_ARCHITECTURE_AUDIT.md) · [PLATFORM_MASTER_SPEC.md](PLATFORM_MASTER_SPEC.md).
**Environment baseline (today):** FastAPI monolith on Cloud Run, Supabase (Postgres + Storage), React SPA, Electron tracker, GitHub Actions CI, Alembic (best-effort at boot). No Redis/queue/cache yet. This guide describes how to execute the migration to the target while shipping continuously.

---

## 1. Engineering Principles (Section 1)

**The prime directive:** *fix the foundation before building on it, and never break production doing so.* Every principle below serves that.

- **Incremental development.** No big-bang rewrites. Every change ships behind a flag or as an additive migration. A phase is a sequence of independently-shippable increments, each green in CI and reversible.
- **Backward compatibility.** Additive-first: new columns nullable, new tables alongside old, new endpoints at `/v1` beside the legacy surface. Remove the old path only after all consumers (SPA, tracker, extension, API clients) have moved and a deprecation window has elapsed. The **desktop tracker and browser extension are long-tail clients** — assume old versions call old endpoints for months; never remove an endpoint they use without telemetry proving zero traffic.
- **Zero downtime.** Expand → migrate → contract for every schema change (§4). Stateless services (api/worker/realtime) so rolling deploys never drop the fleet. Migrations are forward-compatible with the *currently-running* code.
- **Feature flags.** Every risky or multi-sprint change is flag-gated (org-scoped where possible). Flags decouple deploy from release, enable canary/gradual rollout, and are the rollback lever of first resort. Retire flags once a change is fully rolled out — a flag left on forever is debt (track them; see §9).
- **Database-first for the foundation, API-first for features.** Phase 3 foundation work (canonical org model, config/permission engines) is **database-first**: settle the schema and migration, then the service, then the API, then the UI. New product features (Phase 4+) are **API-first**: agree the OpenAPI contract, generate/mock it, build UI and backend against it in parallel.
- **Release strategy.** Trunk-based with short-lived branches; continuous delivery to staging on merge; promotion to production is a deliberate, observable step (canary → full). Product **versions (V1–V5)** are marketing/QA milestones, not long-lived code branches.
- **Rollback strategy.** Three layers, in order of preference: (1) **flag off** (instant, no deploy); (2) **redeploy previous image** (minutes, stateless tiers make this safe); (3) **DB rollback** — avoided by design because expand/contract means the previous app version still works against the new schema. Destructive contractions happen only after the expansion has baked and been verified.

---

## 2. Development Workflow (Section 2)

**Git / branch strategy — trunk-based.**
- `main` is always deployable. Work on short-lived `feature/*`, `fix/*`, `chore/*` branches off `main`; merge within days, not weeks.
- `release/vX.Y` branches cut only when stabilizing a product version for QA; hotfixes cherry-picked back to `main`.
- `hotfix/*` off the production tag for emergencies; forward-merged to `main` immediately.
- Long-running work stays on trunk behind a **feature flag**, not a long-lived branch.

**Pull request rules.**
- Small and focused — one logical change; PRs touching > ~400 lines of logic need justification or splitting.
- Must pass all CI gates (§6) before review. Green-before-review is mandatory.
- Description states: what, why, how tested, rollback plan, flag name (if any), and links to the spec/roadmap item.
- No PR merges to `main` without ≥ 1 approval (≥ 2 for migrations, permissions, billing, or auth).
- Migrations, `permissions`, `billing`, and `auth` changes require a **domain-owner** review, not just any approver.

**Code review checklist.**
- [ ] Conforms to the Master Spec (right bounded context, `router→service→repository`, no `db.query()` in routers)
- [ ] Tenant isolation: every query org-scoped / RLS-covered; no cross-tenant path
- [ ] AuthZ via `require_permission`, not ad-hoc `role in (...)`
- [ ] Migration is expand/contract, reversible, forward-compatible with running code
- [ ] Backward compatible for tracker/extension/API clients (or gated + deprecation planned)
- [ ] Tests: unit for logic, integration for endpoints, tenant-isolation case if touching data access
- [ ] Observability: structured logs + correlation id; metrics/traces for new hot paths
- [ ] Secrets from the secret manager, never inline; no new committed `.env`
- [ ] Flag added for risky/incremental change; flag retirement noted
- [ ] Docs/ADR updated if a decision changed

**Merge policy.** Squash-merge to `main` with a conventional-commit title. CI green + required approvals + no unresolved threads. Auto-deploy to staging on merge.

**Hotfix process.** Branch from the prod tag → minimal fix + test → expedited review (still ≥ 1 domain owner) → canary → full → forward-merge to `main`. Post-incident note in `runbooks/`.

---

## 3. Sprint Plan (Section 3)

Two-week sprints, mapped to the roadmap's phases/versions. Each sprint has a single dominant theme so it can ship as a coherent increment. (Sprint numbers align with the Roadmap §11 backlog.)

### V1.0 — Stabilize & Secure

**Sprint 1 — Critical stability (Phase 1a).**
- *Objectives:* remove active defects and cross-tenant leaks.
- *Deliverables:* BUG-5 (`team_id`→`team_key`), BUG-6 (org-filter on integrity/active_time), BUG-7 (holidays → API), BUG-8/10/11/12 (audit gaps, profile-pic validation, dead code, cache invalidation).
- *Dependencies:* none. *Risks:* low. *DoD:* all fixed, tests added, no regression; org-level settings save works end-to-end.

**Sprint 2 — Secret rotation + billing tenancy (Phase 1b).**
- *Objectives:* close the credential exposure and single-tenant billing.
- *Deliverables:* BUG-1 (.env purge + `SECRET_KEY`/DB rotation, maintenance window) and BUG-2 (billing keyed on real org id + data migration off `"default"`, after backups + staging test).
- *Dependencies:* backup verified; maintenance window scheduled; comms to users (session invalidation). *Risks:* med (session invalidation, billing data migration). *DoD:* no secret in git history; each org sees only its own billing; migration verified with row-count reconciliation.

**Sprint 3–4 — Permission engine (Phase 2A).**
- *Objectives:* one persisted RBAC + `require_permission`.
- *Deliverables:* `roles`/`permissions`/`role_permissions`/`user_roles` tables; promote `offboarding/rbac.py` matrix; `require_permission()` dependency shipped in **shadow/log-only** mode first (compare to legacy checks), then enforced; unify the four admin-role sets.
- *Dependencies:* Sprint 1–2. *Risks:* med (lockout) — mitigated by shadow mode. *DoD:* all settings/org/employee/workspace endpoints route through `require_permission`; shadow logs show parity for ≥ 1 week before enforce.

**Sprint 5 — Tenant isolation + audit + rate limiting (Phase 2A).**
- *Deliverables:* centralized org-scoped access (RLS or a mandatory dependency); unified `audit_events` store; rate limiting on settings routers.
- *DoD:* `test_tenant_isolation.py` expanded and green; every security-mutation audited; rate limits live.

**Sprint 6 — Policy enforcement (Phase 2A).**
- *Deliverables:* session idle/absolute-timeout + IP allow-list + password-policy **enforcement middleware** (BUG-4 generic part).
- *DoD:* configured policies are actually enforced at request time; verified by test.

### V2.0 — Enterprise Identity (Phase 2B)

**Sprint 7–8 — MFA.** Real `mfa_factors` + TOTP enroll/verify + login enforcement (BUG-3). Opt-in → grace period → enforce; recovery codes + break-glass. *DoD:* MFA enforced per org policy; recovery + break-glass tested.
**Sprint 9–11 — SSO/SAML.** OIDC (Google/Azure), then SAML2 SP; enforce-for-admins actually enforced. *DoD:* login via each IdP in staging; enforcement verified.
**Sprint 11–12 — SCIM + password policy + trusted devices.** SCIM 2.0 provisioning/deprovisioning; org-config-driven password rules; trusted-device model.

### V3.0 — Platform Foundation (Phase 3) — *database-first, the highest-risk version*

**Sprint 13 — Schema single-source-of-truth.** Remove boot-time `ensure_tables`/`create_all`; reconcile Alembic with reality; split `models.py` per domain; CI model==head gate. *DoD:* one schema source; CI enforces it.
**Sprint 14–15 — Canonical org spine (expand).** New rich `organizations` + `departments` + `team_members`; `commercial_profile` 1:1; **dual-write** old+new; backfill jobs. *Risks:* HIGH. *DoD:* new model populated + verified against old; reads still on old path behind a flag.
**Sprint 16 — Org spine (migrate + contract).** Flip reads to new model behind flag org-by-org (canary); migrate `User.team`/`department` strings → FKs; delete-or-adopt `Tenant/Workspace/Membership`. *DoD:* 100% reads on new model; old columns marked for removal next version.
**Sprint 16–17 — Config engine + RLS.** 5-level cascade (add Department) resolved by FK; typed config tables; Redis cache; RLS on tenant tables. 
**Sprint 17–18 — `common/` layer + service/repo refactor.** Ship `common/deps|errors|pagination`; refactor `employees` and `leave` as the proof pattern (routers → services → repositories); thin `main.py`.
**Sprint 18 — Infra baseline.** Redis + Celery + worker service; move the 7 in-process schedulers to Celery beat; WS → Redis pub/sub; object storage + async screenshot pipeline; blobs out of rows.
**Sprint 18 — Org templates.** Preset config maps appliable to a scope.
- **V3 exit criteria (gate to V4):** one org identity with FKs + RLS; one schema source; config engine with Department + cache; Redis-backed realtime/jobs; `common/` + 2 modules refactored; no dead scaffold; base64 blobs migrated.

### V4.0 — Enterprise & Developer Platform (Phase 4)
**Sprint 19–20** feature flags + API keys. **Sprint 21–23** webhooks + integrations/connectors + developer portal. **Sprint 23+** automation, reports/analytics, branding, notifications engine. *API-first throughout.*

### V5.0 — Commercial, AI & Scale (Phase 5–7)
**Sprint 24+** full billing/subscription/seats/quotas/usage + marketplace; org cloning + import/export + backup/restore. **Sprint 29+** performance/HA (read replicas, partitioning, materialized rollups). **Sprint 31+** frontend redesign (design system + React Query + feature folders + TS) + UX polish.

*Every sprint's generic DoD:* merged behind flag where risky, CI green (all gates), tests added, observability wired, docs/ADR updated, deployed to staging and smoke-verified, rollback plan stated.

---

## 4. Migration Strategy (Section 4)

**Golden rule: expand → migrate → contract, always.** The running app must work against both the old and new schema at all times during a migration.

**Database migrations.**
1. **Expand** — add new tables/columns (nullable/defaulted), new indexes `CONCURRENTLY`. Never rename/drop in the same step. Deploy; old code unaffected.
2. **Backfill** — a worker/job copies/derives data old→new, idempotent and batched (no long table locks). Verify with row-count + checksum reconciliation.
3. **Dual-write** — new writes go to both old and new for a bake period; reads flip to new behind a flag, canaried org-by-org.
4. **Contract** — only after reads are 100% on new and the bake period passed: stop dual-write, drop old columns/tables in a later release.
- Alembic is the **only** source of truth (kill boot-time creation, per ADR-004). One migration = one logical change, both `upgrade` and `downgrade` implemented and tested. Test every migration against a **production-sized copy** before prod.

**API migrations.** New surface at `/v1`; legacy endpoints kept until telemetry shows zero traffic from all clients. Deprecations announced via `Sunset`/`Deprecation` headers + changelog. Never change a response shape in place — add a new field or a new version.

**Frontend migrations.** Strangler pattern — new feature folders + design-system components alongside old; migrate screen-by-screen behind flags. Old and new components coexist during transition. React Query adopted incrementally (new data hooks first).

**Desktop tracker migrations.** The tracker is the **hardest to migrate** — versions in the wild update slowly (auto-update helps but isn't instant). Keep `/settings` (and any endpoint it calls) backward-compatible indefinitely until telemetry proves the old version count is zero. New config keys must default safely for old trackers that don't understand them. Ship tracker changes behind server-negotiated capability, not hard cutovers.

**Organization migrations.** The canonical-org-spine change is migrated **per-org, canaried**: pick low-risk internal/test orgs first, verify, then ramp. A per-org migration flag lets you halt and roll back a single tenant without touching others.

**Data migration.** Idempotent, resumable, batched jobs run on the worker fleet (not in a request). Always snapshot before a destructive step. Reconcile counts/checksums; log a migration audit record. Dry-run in staging on a prod-sized dataset first.

**Rollback plan (per migration).** Flag-off first; then redeploy previous image (safe because expand/contract keeps old code working); DB downgrade only as last resort and only if no destructive contraction has run. Each migration PR must state its rollback path explicitly.

---

## 5. Testing Strategy (Section 5)

| Layer | What | Where it runs |
|---|---|---|
| **Unit** | Pure logic — engines (`active_time`, `integrity`, config resolver, permission checks), services. No DB. | Every PR (fast) |
| **Integration** | `router → service → repository → DB` against an ephemeral Postgres; tenant scoping verified. | Every PR |
| **API/contract** | Endpoints against the OpenAPI schema; request/response shape; backward-compat check vs prior contract. | Every PR |
| **Tenant-isolation regression** | Dedicated suite: no endpoint returns/writes cross-tenant data. Expanded from the existing `test_tenant_isolation.py`. **Mandatory gate.** | Every PR |
| **UI** | Component tests (Vitest) + E2E (Playwright) for critical flows: login/MFA/SSO, settings override+reset, clock in/out, leave request, billing. | PR (component) + nightly (E2E) |
| **Desktop** | Tracker smoke: settings poll, screenshot upload, offline/refresh, old-version compat against current API. | Pre-release + nightly |
| **Load** | Ingest at target rates (100k trackers polling, screenshots/day), config-resolve latency, WS fan-out; queue-depth drain. | Pre-release of V3/V5 + scheduled |
| **Security** | SAST + dependency scan (every PR); DAST + pen test (pre-release/quarterly); authZ tests for `require_permission`; secret scanning. | PR + scheduled + release |
| **Regression** | Full suite + E2E on release branches; billing/permissions/auth get 100% path coverage. | Release |

**Coverage targets:** ≥ 70% on core domains; **100% on billing, permissions, and auth paths**. Migrations always ship with a test that exercises upgrade→backfill→read. No feature merges without the tenant-isolation case when it touches data access.

---

## 6. CI/CD (Section 6)

**Build pipeline (GitHub Actions, on every PR):**
1. **Lint/format** — Ruff + Black (backend), ESLint + Prettier (frontend). Fail on violation.
2. **Static analysis** — mypy (backend, incremental), TypeScript typecheck (frontend as TS lands).
3. **Tests** — unit + integration + API/contract + **tenant-isolation** (parallelized).
4. **Schema gate** — Alembic model==head check; migration up+down runs against ephemeral Postgres.
5. **Dependency + secret scan** — vulnerability scan (pip-audit/npm audit), secret scanner (block committed credentials — this would have caught the `.env`).
6. **Container build** — build `api`/`worker`/`realtime`/`ai` images; scan for CVEs; push to registry on `main`.

**Quality gates (all must pass to merge):** green tests, coverage not decreased below threshold, no new criticals in dependency/secret scan, migration reversible, OpenAPI contract check passes.

**Deployment pipeline:**
- Merge to `main` → build images → **auto-deploy to staging** → smoke tests.
- Promotion to production is a manual, observable step: **canary** (small % / few orgs) → watch metrics/errors → **full rollout**.
- Migrations run as a **discrete, gated job** (Cloud Run Job / one-off), **not** best-effort at container boot (fixes today's `alembic upgrade || echo` anti-pattern). App boot never mutates schema.

---

## 7. Monitoring (Section 7)

- **Application monitoring** — request rate/latency (p50/p95/p99) per endpoint; error rate; per-domain SLOs. Correlation id on every log line, propagated api→worker→realtime→ai.
- **Infrastructure monitoring** — CPU/mem/connections per service; Postgres (connections, slow queries, replication lag); Redis (memory, hit rate); queue depth per Celery queue.
- **Performance monitoring** — screenshot ingest lag, config-resolve latency (cached vs cold), WS fan-out latency, background-job duration; OpenTelemetry traces for the ingest→score→report path.
- **Error tracking** — Sentry (or equiv) for backend + frontend + tracker; grouped, released-tagged, owner-routed.
- **Alerting** — SLO-burn alerts (error budget), queue-depth backlog, ingest lag, DB saturation, failed migrations, webhook delivery failures, auth anomaly spikes. Alerts route to on-call with runbook links.
- **Health checks** — liveness + readiness probes per service (readiness gates traffic during startup/migration); synthetic checks for login, settings fetch, tracker ingest.
- **Business metrics** — active trackers, screenshots/day, MRR, trial conversion, seat utilization — on a product dashboard.

---

## 8. Deployment Strategy (Section 8)

| Environment | Purpose | Data |
|---|---|---|
| **Development** | Local (docker-compose incl. Redis + object-storage emulator once V3 lands); per-engineer | Synthetic/seed |
| **QA** | Manual + automated E2E; feature verification | Anonymized subset |
| **Staging** | Prod-mirror; auto-deployed on merge; load/security tests; migration dry-runs | Prod-sized anonymized copy |
| **Production** | Live; canary → full; multi-AZ | Live (RLS-isolated) |

- **Rolling** deploys by default for stateless tiers (api/worker/realtime); readiness probes prevent traffic to un-ready pods.
- **Blue/Green** for releases carrying a risky migration or infra change — stand up green, migrate (expand step already done), shift traffic, keep blue warm for instant rollback.
- **Canary** for feature rollout — flag-gated % or org cohort; watch metrics; auto-halt on error-budget burn.
- **Rollback** — flag-off → previous image → (last resort) DB downgrade, per §4. Every production deploy is reversible within minutes.
- **Migrations** run as a gated pre-deploy job with the app in expand-compatible state; never at boot.

---

## 9. Risk Register (Section 9)

| Risk | Type | Likelihood | Impact | Mitigation | Owner |
|---|---|---|---|---|---|
| Canonical org-model migration corrupts/loses data | Migration | Med | Critical | Expand/contract, dual-write, per-org canary, snapshot + checksum reconciliation, prod-sized dry-run | Backend lead |
| Secret rotation invalidates sessions / missed key use | Security/Ops | Med | High | Maintenance window, user comms, rotate refresh tokens, verify all `SECRET_KEY` uses before cutover | Security owner |
| Billing `"default"` migration mis-maps orgs | Migration/Business | Med | High | Reconcile vs commercial layer, dry-run, audit before cutover, reversible | Billing owner |
| RBAC enforcement locks out admins | Security | Med | High | Shadow/log-only mode → parity check ≥ 1 wk → enforce; break-glass | Security owner |
| MFA rollout locks out users | Security | Med | Med | Opt-in → grace → enforce; recovery codes + break-glass | Identity owner |
| Tracker/extension backward-incompat (old versions break) | Technical/Migration | Med | High | Keep old endpoints until telemetry = 0; safe defaults for new keys; capability negotiation | Tracker owner |
| In-process→Redis realtime/scheduler cutover drops events/dupes jobs | Technical/Migration | Med | Med | Parallel-run + verify; idempotent jobs; DB-dedup during transition | Platform lead |
| Schema drift (triple management) causes migration failure | Technical | Med | Med | Kill boot-time creation early (Sprint 13); CI model==head gate | Backend lead |
| Feature-flag debt (flags never retired) | Operational | High | Low | Flag registry + retirement in DoD; periodic flag audit | Eng manager |
| Scope creep into V4 before V3 exit criteria met | Business | High | High | Hard version gate; no V4 merges until V3 exit criteria signed off | Eng manager |
| Load ceiling hit before infra baseline lands | Performance | Med | High | Prioritize Sprint 18 infra; load-test V3 before GA; capacity headroom alerts | Platform lead |
| Single-container real-time/jobs ceiling in production interim | Operational | Med | Med | Pin to single instance for WS/schedulers until Redis backplane ships; document the constraint | Platform lead |
| Compliance gaps block enterprise deals | Business | Med | High | Sequence SOC2/ISO controls into V1–V2; audit logging + retention early | Security owner |

---

## 10. Success Metrics (Section 10)

**Delivery (DORA + quality):**
- **Velocity** — stable, predictable sprint throughput (track completed-vs-committed; don't game story points).
- **Deployment frequency** — target daily to staging, multiple/week to prod (elite band).
- **Lead time for change** — commit → prod < 1 day for standard changes.
- **Change failure rate** — < 15%, trending down.
- **MTTR** — < 1 hour (flag-off/rollback makes this achievable).
- **Bug rate** — escaped defects per release trending down; zero criticals reaching prod.

**Product/platform (from the Master Spec, tracked continuously):**
- **Availability** — 99.9% monthly; RTO ≤ 1h, RPO ≤ 5m.
- **Performance** — API p95 < 300 ms; dashboard < 2 s; ingest lag < 30 s; config resolve < 10 ms cached.
- **Scalability** — linear cost to 10k orgs / 1M employees / 100k concurrent trackers; queue drain < 1 min at peak.
- **Maintainability** — 0 `db.query()` in routers; no file > 800 lines; ≥ 70% coverage (100% billing/permissions/auth); one CI-enforced schema source.
- **Customer satisfaction** — onboarding < 10 min; CSAT/NPS tracked per release; support-ticket rate per feature.

**Gate reviews:** at the end of each **version (V1–V5)**, a formal readiness review checks the exit criteria and the Enterprise Readiness Score (41 → 60 → 66 → **72** → 82 → **90**). A version does not close — and the next does not start — until its criteria are met and its metrics are green.

---

### How the five documents fit together
1. **Enterprise Audit** — what's broken and missing (the evidence).
2. **Gap Analysis & Roadmap** — what to fix, prioritized into 8 phases / V1–V5 (the route).
3. **Platform Architecture Audit** — current-vs-target structural gap (the diagnosis).
4. **Master Spec** — the destination architecture (the north star).
5. **Execution Guide** (this) — how engineers get there safely (the manual).

Read top to bottom for context; execute bottom-up: this guide governs day-to-day work, gated by the roadmap's order, conforming to the spec, justified by the audits.

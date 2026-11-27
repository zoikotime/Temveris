# Temveris — Program Increment (PI) Plan · 12-Month Delivery

**Status:** Master execution tracker. Converts the finalized architecture, roadmap, and execution guide into epics → features → stories → sprints that engineering teams execute.
**Source of truth:** [ORG_SETTINGS_AUDIT.md](ORG_SETTINGS_AUDIT.md) · [ORG_SETTINGS_ROADMAP.md](ORG_SETTINGS_ROADMAP.md) · [PLATFORM_ARCHITECTURE_AUDIT.md](PLATFORM_ARCHITECTURE_AUDIT.md) · [PLATFORM_MASTER_SPEC.md](PLATFORM_MASTER_SPEC.md) · [PLATFORM_EXECUTION_GUIDE.md](PLATFORM_EXECUTION_GUIDE.md).
**Horizon:** 12 months = **26 two-week sprints** across **5 releases (R1–R5 ≈ V1.0–V5.0)**, plus 1 hardening buffer.
**Cadence:** PI = one release; sprints are two weeks; a PI planning + demo + retro brackets each release.

> **Note on the 40-sprint request:** 40 two-week sprints is ~18 months. This plan fits the requested **12-month** window into **26 sprints**; the S27–S40 tail (marketplace, advanced AI, self-hosted, GraphQL) is scoped in §6 as the **post-12-month backlog** so nothing is lost — it's sequenced, not committed inside the year.

---

## 1. Program Overview (Section 1)

**Vision.** Transform Temveris from a functional-but-fragmented product (readiness 41/100) into a mature enterprise SaaS platform (target 90/100) — one canonical multi-tenant identity, enforced security, a real infra layer, a developer platform, and an AI layer — over 12 months, **without a rewrite and without breaking production.**

**Business goals.**
- Close the credibility/safety gaps blocking enterprise deals (security theater, single-tenant billing) within R1–R2.
- Unlock enterprise IT sales (MFA/SSO/SAML/SCIM) by end of R2.
- Reach a sellable enterprise baseline (readiness ~72) by end of R3.
- Open developer/integration revenue and AI differentiation in R4–R5.

**Engineering goals.**
- One org identity, one schema source, one config engine, one permission engine, one audit backbone.
- Remove the single-process ceiling (Redis + workers + object storage).
- Enforce `router→service→repository`; 0 `db.query()` in routers on refactored modules; CI-gated schema.

**Success metrics (tracked on the §12 dashboard):** DORA (deploy freq, lead time, CFR, MTTR) + availability 99.9% + API p95 < 300 ms + readiness score trajectory 41 → 60 → 66 → **72** → 82 → **90**.

---

## 2. Release Planning (Section 2)

| Release | Sprints | Objective | Headline features | Depends on | Top risk | Exit criteria |
|---|---|---|---|---|---|---|
| **R1 — Stabilize & Secure** | S1–S6 | Trustworthy, leak-free, secured foundation | Critical bugs fixed; secret rotation; billing de-`default`; persisted RBAC + `require_permission`; tenant-isolation centralization; unified audit; policy/rate enforcement | — | Secret rotation + billing migration | No secret in git; no cross-tenant leaks; RBAC enforced; all security mutations audited; readiness ~60 |
| **R2 — Enterprise Identity** | S7–S12 | Sellable to enterprise IT | Real MFA; OIDC SSO (Google/Azure); SAML2; SCIM; enforced password policy; trusted devices | R1 (permission engine) | User/admin lockout during rollout | MFA/SSO/SAML/SCIM working + enforced in staging; recovery/break-glass tested; readiness ~66 |
| **R3 — Platform Foundation** | S13–S18 | The base everything builds on (highest risk) | Schema single-source; canonical org spine + Department + team FK; config engine + cache; RLS; `common/` + service/repo refactor; Redis + workers + object storage; org templates | R1–R2 | Org-model migration data loss | One org identity + FKs + RLS; one schema source; Redis-backed realtime/jobs; blobs migrated; readiness **~72** |
| **R4 — Enterprise & Developer Platform** | S19–S23 | Extensibility & depth | Feature flags; API keys; webhooks; integrations/connectors; developer portal; automation; reports/analytics; notifications engine; branding | **R3 exit criteria met** | Building on unfinished foundation | Public API + webhooks + flags live; ≥ 2 connectors; readiness ~82 |
| **R5 — Commercial, AI & Scale** | S24–S26 (+ backlog) | Full SaaS + intelligence + HA | Full billing/subscription/seats/quotas/usage; AI platform (RAG, insights, automation); performance/HA (replicas, partitioning); frontend redesign start | R4 | Scale ceiling before infra hardened | Usage-based billing; AI insights GA; 99.9% + load-tested at target; readiness **~90 (in-progress at 12-mo mark)** |

**Hard gate:** no R4 work merges until R3 exit criteria are signed off (Execution Guide §9 risk).

---

## 3. Epic Breakdown (Section 3)

| # | Epic | Purpose | Business value | Depends on | Priority | Complexity |
|---|---|---|---|---|---|---|
| E1 | **Critical Stability** | Remove active bugs + cross-tenant leaks | Protects existing customers/revenue | — | P0 | Low |
| E2 | **Security Foundation** | Persisted RBAC/ABAC, isolation, audit, policy enforcement | Enterprise trust; unblocks deals | E1 | P0 | High |
| E3 | **Enterprise Identity** | MFA, SSO, SAML, SCIM | Enterprise IT sign-off | E2 | P0 | High |
| E4 | **Identity & Org Model** | Canonical `Org→Dept→Team→User` spine | Foundation for all tenancy | E1 | P0 | High |
| E5 | **Configuration Engine** | 5-level cascade, typed+dynamic, cached, templates | Consistent settings; feature velocity | E4 | P1 | High |
| E6 | **Permission Engine** | Custom roles, scoped/delegated/temporary grants | Enterprise admin model | E2 | P1 | High |
| E7 | **Infrastructure** | Redis, workers, object storage, event bus, observability | Scale + reliability | E1 | P0 | High |
| E8 | **Backend Modernization** | `router→service→repo`, `common/`, split models, thin main.py | Maintainability | E7 | P1 | High |
| E9 | **Billing & Commercial** | Reconcile identity, one subscription/invoice model, seats/quotas/usage | Revenue integrity + monetization | E4 | P1 | High |
| E10 | **Developer Platform** | API keys, webhooks, OAuth apps, portal, rate limits | Integration revenue/stickiness | E6, E8 | P2 | High |
| E11 | **Integrations** | Connector framework (HRIS/payroll/SSO/comms) | Enterprise fit | E10 | P2 | High |
| E12 | **Notifications** | Event-driven channels, templates, prefs | UX + engagement | E7 | P2 | Med |
| E13 | **Reporting & Analytics** | Rollups, exports, read replicas | Product depth | E7, E8 | P2 | Med |
| E14 | **AI Platform** | Assistant, RAG, insights, risk, automation | Differentiation | E7, E13 | P2 | High |
| E15 | **Compliance** | SOC2/ISO/GDPR controls, retention, DSAR, residency | Enterprise/regulated deals | E2 | P1 | Med |
| E16 | **Frontend Modernization** | Design system, React Query, feature folders, TS, split monsters | DX + UX + perf | E5, E8 | P2 | High |
| E17 | **Feature Flags** | Org + rollout + entitlement-layered flags | Safe delivery + gating | E5 | P1 | Med |
| E18 | **Tracker & Extension** | Backward-compat, capability negotiation, OAuth device auth | Client longevity | E4, E5 | P1 | Med |

---

## 4. Feature Breakdown (Section 4) — representative, per epic

*(Each feature: objective · acceptance criteria · dependencies · risk · sprint. Abbreviated to the load-bearing features; the tracker expands each into stories in §5.)*

**E1 Critical Stability**
- F1.1 `team_id`→`team_key` fix — *AC:* org-level settings save returns 200 end-to-end; regression test. *Dep:* none. *Risk:* low. *S1.*
- F1.2 Org-scope filter on integrity/active_time `load_config` — *AC:* config reads are org-isolated; test proves no cross-tenant read. *S1.*
- F1.3 Public Holidays → API (off localStorage) — *AC:* holidays persist server-side, per-tenant, survive device change. *S1.*
- F1.4 Audit gaps + profile-pic validation + dead-code removal + cache invalidation. *S1.*
- F1.5 Secret rotation + `.env` purge — *AC:* no secret in history; secret scanner in CI; sessions re-issued. *Risk:* med. *S2.*
- F1.6 Billing de-`default` migration — *AC:* each org sees only its billing; row-count reconciliation. *Risk:* med. *S2.*

**E2 Security Foundation**
- F2.1 RBAC tables + `require_permission` (shadow→enforce) — *AC:* all settings/org/employee/workspace endpoints gated; ≥1 wk shadow parity. *S3–S4.*
- F2.2 Tenant-isolation centralization (RLS/dependency) — *AC:* isolation regression suite green. *S5.*
- F2.3 Unified `audit_events` — *AC:* one queryable trail; security mutations logged. *S5.*
- F2.4 Policy enforcement middleware (session/IP/password) + rate limiting on settings routers. *S6.*

**E3 Enterprise Identity**
- F3.1 MFA (TOTP + enforcement + recovery + break-glass). *S7–S8.*
- F3.2 OIDC SSO (Google/Azure) + enforced-for-admins. *S9.*
- F3.3 SAML2 SP. *S9–S11.* F3.4 SCIM 2.0. *S11.* F3.5 Password policy from config + trusted devices. *S12.*

**E4 Identity & Org Model**
- F4.1 Schema single-source (kill boot creation, CI gate, split models). *S13.*
- F4.2 Rich `organizations` + `departments` + `team_members` (expand + dual-write + backfill). *S14–S15.*
- F4.3 Read-flip + string→FK migration + delete/adopt dead scaffold (per-org canary). *S16.*

**E5 Config Engine / E6 Permission Engine / E7 Infra** — F5.x (5-level cascade + typed tables + Redis cache + templates, S16–S18); F6.x (custom/scoped/delegated/temporary roles, layered through R3–R4); F7.x (Redis + Celery + workers + object storage + async screenshot pipeline + WS backplane, S18).

**E9–E14 (R4–R5)** — feature flags (S19–20), API keys (S20), webhooks + integrations + portal (S21–23), full billing/seats/quotas/usage (S24), AI RAG/insights/automation (S24–25), reporting rollups + notifications engine (S22–25), perf/HA + frontend modernization start (S25–26).

---

## 5. Story Mapping (Section 5) — samples across personas

*(Standard DoD for every story: merged behind flag where risky · CI green (lint/type/tests/tenant-isolation/schema gate) · tests added · observability wired · docs/ADR updated · deployed to staging + smoke-verified · rollback stated.)*

**Config / Settings (E5)**
- *As an Organization Admin,* I set a screenshot interval at the **Department** level so all its teams inherit it. **AC:** dept override shows in the cascade; child teams/users inherit unless overridden; reset restores inheritance; effective value resolves < 10 ms cached.
- *As a Team Lead,* I override idle-timeout for **my team only**. **AC:** override applies to team members, not siblings; "inherited from Org" shown for untouched keys; I cannot edit scopes above my team (permission-scoped).
- *As an Employee,* I see which settings are **managed by my workspace** (locked) with the governing policy + version. **AC:** locked controls disabled with explanation; my allowed prefs remain editable.

**Permissions (E6)**
- *As an Organization Admin,* I create a custom **"Department Admin"** role scoped to one department. **AC:** role grants only the selected permissions at `@department` scope; holder can't act outside it; creation audited; I can't grant a permission I don't hold (SoD).
- *As a Super Admin,* I grant a **temporary** support-access permission that auto-expires. **AC:** `permission_grants.expires_at` enforced; expiry emits `grant.expired` to audit.

**Identity (E3)**
- *As an Organization Admin,* I **enforce MFA** for all admins. **AC:** admins without MFA are prompted to enroll on next login; break-glass path documented; toggle actually enforced (not stored-and-ignored).
- *As an Employee,* I sign in via **company SSO**. **AC:** OIDC/SAML login provisions/links my account (invite-only), `email_verified` required, cross-tenant same-email disambiguated.

**Org model (E4)**
- *As a Super Admin,* I migrate an org to the new identity model **without downtime**. **AC:** dual-write verified, reads flipped behind a per-org flag, `User.team` strings mapped to FKs with zero orphaned overrides, reversible.

**Developer (E10)**
- *As an Organization Admin,* I create a **scoped API key** and register a **webhook**. **AC:** key hashed at rest, scoped to permissions, rotatable, rate-limited; webhook signed, retried with backoff, delivery logged.

---

## 6. Sprint Allocation (Section 6)

| Sprint | Stories / theme | Dependencies | Expected outcome |
|---|---|---|---|
| **S1** | F1.1–F1.4 critical bugs | — | No broken settings, no config leaks; CI secret scanner + schema gate added |
| **S2** | F1.5 secret rotation, F1.6 billing de-`default` | backups, window | Secrets safe; billing per-tenant |
| **S3–S4** | F2.1 RBAC + `require_permission` (shadow→enforce) | S1–S2 | One enforced permission model |
| **S5** | F2.2 isolation centralization, F2.3 unified audit | S3–S4 | RLS/dependency; single audit trail |
| **S6** | F2.4 policy enforcement + rate limiting | S5 | Stored policies actually enforced → **R1 done (~60)** |
| **S7–S8** | F3.1 MFA | S3–S4 | Real, enforced MFA |
| **S9** | F3.2 OIDC SSO | S7 | Google/Azure SSO |
| **S9–S11** | F3.3 SAML2 | S9 | Enterprise IdP login |
| **S11** | F3.4 SCIM | S9 | Provisioning/deprovisioning |
| **S12** | F3.5 password policy + trusted devices | S6 | **R2 done (~66)** |
| **S13** | F4.1 schema single-source + CI gate + split models | S2 | One schema source; drift ended |
| **S14–S15** | F4.2 org spine expand + dual-write + backfill | S13 | New identity populated + verified (reads still old) |
| **S16** | F4.3 read-flip (per-org canary) + string→FK + scaffold delete | S14–S15 | 100% on new org model |
| **S16–S17** | F5.x config engine + RLS + typed tables | S16 | 5-level cascade + cache |
| **S17–S18** | F8/E8 `common/` + employees/leave service-repo refactor + thin main.py | S13 | Proof pattern for modernization |
| **S18** | F7.x Redis + Celery + workers + object storage + WS backplane + async screenshots + org templates | S16–S17 | Single-process ceiling removed → **R3 done (~72)** |
| **S19–S20** | E17 feature flags, E10 API keys | R3 exit | Safe gating + programmatic access |
| **S21–S23** | E10 webhooks + developer portal, E11 integrations (≥2 connectors), E13 reporting rollups, E12 notifications engine | S19–20 | Developer platform live |
| **S24** | E9 full billing/seats/quotas/usage; E14 AI RAG/insights start | R4 base | Monetization + AI foundation |
| **S25** | E14 AI insights/automation; E13 analytics; E16 frontend modernization start | S24 | Differentiation features |
| **S26** | E15 compliance hardening; E7 perf/HA (replicas/partitioning); load test at target | S24–25 | **R5 in progress (~82→90 path); 12-mo mark** |
| **S27–S40 (post-12-mo backlog)** | Marketplace + paid add-ons; advanced AI (document/meeting intelligence, OCR); GraphQL (if demanded); self-hosted/private-cloud; full TS migration; org cloning/backup-restore; frontend redesign completion | R5 | Path to full 90 + parity |

---

## 7. Cross-Team Dependencies (Section 7)

| Depends → | Frontend | Backend | Tracker | Extension | Infra | AI | Commercial |
|---|---|---|---|---|---|---|---|
| **Frontend** | — | API contracts (OpenAPI) for every feature; design system needs backend flags | — | — | — | AI UI needs AI API | Billing UI needs commercial API |
| **Backend** | consumes UI feedback | — | must keep `/settings` + ingest endpoints back-compat | `/browser/*` back-compat | needs Redis/workers/object-storage (S18) | exposes events for AI | reconcile org identity for billing |
| **Tracker** | — | config keys + safe defaults; capability negotiation (S18/E18) | — | shares `/browser/*` schema | — | — | — |
| **Extension** | — | token/OAuth device auth (E18) | — | — | — | — | — |
| **Infra** | — | provides Redis/queue/storage before E9–E14 can scale | — | — | — | AI service hosting | worker fleet for dunning/invoicing |
| **AI** | AI panels | consumes event bus + read replicas | — | — | needs vector store + service | — | usage metering for AI add-ons |
| **Commercial** | billing screens | identity reconciliation (E4) before billing FK fix | — | — | workers for dunning/trials | — | — |

**Critical-path dependencies (block multiple teams):** E4 org identity → E9 billing + E5 config; E7 infra (S18) → E10–E14 scale; E2 permission engine → E10 developer platform + E3 enforcement. **Backend↔Tracker back-compat** is a standing constraint on every backend change (see Execution Guide §4).

---

## 8. Risk Tracking (Section 8)

| Risk | Category | Sprint window | Mitigation | Owner |
|---|---|---|---|---|
| Org-model migration data loss | Migration | S14–S16 | Expand/contract, dual-write, per-org canary, checksum reconciliation, prod-sized dry-run | Backend lead |
| Secret rotation session/uses missed | Security/Ops | S2 | Window + comms + rotate all uses + verify | Security owner |
| Billing `"default"` mis-map | Migration/Business | S2 | Reconcile vs commercial, dry-run, reversible | Billing owner |
| RBAC lockout | Security | S3–S4 | Shadow→parity→enforce; break-glass | Security owner |
| MFA lockout | Security | S7–S8 | Opt-in→grace→enforce; recovery codes | Identity owner |
| Tracker/extension back-incompat | Technical | all backend sprints | Keep old endpoints until telemetry=0; safe defaults | Tracker owner |
| Realtime/scheduler cutover drops/dupes | Migration | S18 | Parallel-run + idempotent jobs + DB dedup | Platform lead |
| Schema drift breaks migration | Technical | S13 | Kill boot creation early; CI model==head | Backend lead |
| Scope creep into R4 pre-R3-exit | Business | S16–S19 | Hard gate; no R4 merges until R3 criteria signed | Eng manager |
| Load ceiling before infra lands | Performance | ≤S18 | Prioritize S18; pin single-instance WS/schedulers interim; load-test R3 | Platform lead |
| Feature-flag debt | Operational | ongoing | Flag registry + retirement in DoD | Eng manager |
| Compliance gaps block deals | Business | R1–R2, S26 | Sequence SOC2/ISO controls early; audit+retention in R1 | Security owner |

---

## 9. Milestones (Section 9)

| Milestone | Target | Definition |
|---|---|---|
| **Security Complete** | End S6 (R1) | RBAC enforced, isolation centralized, audit unified, policies enforced, secrets safe |
| **Enterprise Identity Complete** | End S12 (R2) | MFA/SSO/SAML/SCIM working + enforced |
| **Architecture Complete** | End S18 (R3) | Canonical org identity + RLS; one schema source; config engine; Redis+workers+object-storage; modernization pattern proven |
| **Developer Platform Ready** | End S23 (R4) | Public API + webhooks + API keys + ≥2 connectors + portal |
| **Commercial Ready** | S24 (R5) | Usage-based billing, seats, quotas, entitlements reconciled to real identity |
| **Enterprise Ready** | ~S26 | Readiness ~82→GA path; compliance controls in place; load-tested at target |
| **Beta** | Per major feature (MFA/SSO beta R2; developer platform beta R4) | Feature-flagged to design-partner orgs |
| **RC** | End of each release | Release branch stabilized, full regression + E2E green, exit criteria met |
| **GA** | Per release after RC bake | Canary→full rollout, metrics green, rollback verified |

---

## 10. Resource Planning (Section 10)

Sized for a 12-month, single-PI-team-of-teams execution. Scale up to compress R4–R5 (which parallelize once R3 lands).

| Role | Count | Primary focus | Peak window |
|---|---|---|---|
| Backend Engineers | 4–5 | E1–E9, E11–E13 (migrations, engines, APIs) | R1–R3 |
| Frontend Engineers | 2–3 | E16 + all feature UIs; design system | R3–R5 |
| DevOps / Platform | 1–2 | E7 infra, CI/CD, observability, migration jobs | R1 (CI), R3 (infra) |
| QA / SDET | 2 | test strategy, tenant-isolation suite, E2E, load/security | continuous |
| AI Engineers | 1–2 | E14 (RAG, insights, automation) | R5 |
| Security Engineer | 1 | E2, E3, E15 (RBAC, MFA/SSO, compliance) | R1–R2, S26 |
| Product Manager | 1 | backlog, priorities, gate reviews | continuous |
| Designer | 1 | design system, settings/permission/developer UX | R3–R5 |
| Technical Writer | 0.5–1 | API docs, developer portal, runbooks, ADRs | R2–R5 |
| Eng Manager / RTE | 1 | PI orchestration, dependency + risk management, gates | continuous |

**Total ≈ 14–19 people.** Backend-heavy early (foundation), frontend/AI-heavy late (features). Tracker/extension work is absorbed by backend + 1 rotating owner (small, back-compat-driven).

---

## 11. Release Readiness Checklist (Section 11) — gate before every GA

- [ ] All release-scope stories meet DoD; exit criteria (from §2) demonstrably met
- [ ] Full regression + E2E green on the release branch; **tenant-isolation suite green**
- [ ] Migrations: expand/contract verified, reversible, dry-run on prod-sized data, rollback documented
- [ ] Backward compatibility confirmed for tracker/extension/API clients (telemetry checked)
- [ ] Security: SAST/DAST/dependency + secret scan clean; authZ tests pass; pen test for R2/R5
- [ ] Performance: load test at target passes SLOs (p95 < 300 ms, ingest lag < 30 s)
- [ ] Observability: dashboards + alerts live for new surfaces; correlation ids flowing
- [ ] Feature flags configured; rollout plan (canary %/cohort) + kill-switch defined
- [ ] Rollback rehearsed (flag-off → previous image → DB path); on-call briefed
- [ ] Docs/ADRs/runbooks updated; customer comms drafted for user-visible changes
- [ ] Compliance: audit logging + retention verified for the release's data changes
- [ ] Gate review sign-off: PM + Eng Manager + Security owner + domain owner

---

## 12. Success Dashboard (Section 12) — KPIs

| KPI | Target | Cadence |
|---|---|---|
| **Velocity** | Stable committed-vs-completed; no systemic overcommit | Per sprint |
| **Defect rate** | Escaped defects/release ↓; 0 criticals to prod | Per release |
| **Deployment frequency** | Daily→staging, multiple/week→prod | Weekly |
| **Lead time** | Commit→prod < 1 day (standard change) | Weekly |
| **Change failure rate** | < 15% | Per release |
| **MTTR** | < 1 hour | Per incident |
| **Availability** | 99.9% monthly (RTO ≤1h, RPO ≤5m) | Monthly |
| **Performance** | API p95 < 300 ms; dashboard < 2 s; config resolve < 10 ms | Continuous |
| **Security** | 100% security-mutations audited; 0 pen-test criticals; MFA/SSO adoption ↑ | Monthly |
| **Maintainability** | 0 `db.query()` in refactored routers; no file > 800 lines; ≥70% coverage (100% billing/perm/auth); CI schema gate green | Per release |
| **Customer adoption** | Onboarding < 10 min; feature adoption per release; CSAT/NPS | Per release |
| **Readiness score** | 41 → 60 → 66 → **72** → 82 → **90** | Per release gate |

---

### How this PI plan is used
Read the strategy top (Program Overview) → releases → epics → features → stories → sprints. **Execute** sprint-by-sprint from §6, respecting the §7 dependencies and §8 risks, gating each release on §11. Track progress on §12. This document is the master tracker; the Execution Guide governs *how* each story is built; the Master Spec defines *what* the end state is; the Roadmap sets *the order*; the Audits are *why*.

**The one rule that protects the whole year:** *no R4 work merges until R3 exit criteria are signed off.* Everything after the foundation compounds on it — build the foundation first, gate hard, and Temveris reaches enterprise-grade instead of accumulating rework.

# Temveris — Organization Settings: Gap Analysis & Implementation Roadmap

**Date:** 2026-07-16
**Source of truth:** [docs/ORG_SETTINGS_AUDIT.md](ORG_SETTINGS_AUDIT.md)
**Nature:** Planning document. No code written. This converts the audit findings into a prioritized, sequenced execution plan.

**Convention:** Effort in engineer-weeks assuming 1–2 engineers. Priority `P0` (immediate) → `P3` (later). Business value H/M/L. Sprint = 2 weeks.

---

## 1. Executive Summary

The audit scored the module **41/100**. The engine underneath (the `system→org→team→employee` cascade, the refresh-token security design, the audit coverage, the commercial billing engine's design) is solid. The problem is not the core idea — it's three things stacked on top of it:

1. **Three P0 defects that are dangerous or broken today:** a committed secret (`backend/.env`), a save endpoint that throws (`AppSettings.team_id`), and single-tenant billing (`"default"` key).
2. **Security theater:** MFA, session/IP/SSO/password policies, and RBAC permissions are all stored in the DB but **enforced by no runtime code**. Marketing any of these is a compliance liability.
3. **A fragmented foundation:** org identity split across 3 unlinked tables, no Department tier, team membership as a string, settings split across typed tables + key/value + audit-log-as-config.

**Roadmap shape (8 phases):** stability → security foundation (2A) → identity/auth (2B) → platform foundation → enterprise features → SaaS commercial → performance → UX polish. Phases 1 → 2A → 2B → 3 (~5.5–6.5 months) reach a *credible enterprise baseline* (target score ~72). Full parity (~10–11 months) targets ~88.

**Two sequencing rules drive the whole plan:**
1. **Security is split so architecture precedes features.** *Phase 2A* builds the security **foundation** (RBAC, permission matrix, tenant isolation, audit, session/policy enforcement) — these are prerequisites. *Phase 2B* builds security **features** (MFA, SSO/SAML, SCIM, password policies, trusted devices) — customer-facing capabilities that depend on a stable permission model. Building MFA/SSO before RBAC is enforced means reworking their authZ later.
2. **Do not build enterprise features (Phase 4+) before the platform foundation (Phase 3).** Feature flags, API keys, and integrations built on the current 3-table org model and string RBAC will have to be reworked when the foundation is corrected.

---

## 2. Critical Bugs (Step 1)

Ranked. "Fix time" is the code change; add QA/migration/rollout separately where noted.

### CRITICAL

**BUG-1 — `backend/.env` committed to git (live secrets exposed)**
- *Description:* Live `SECRET_KEY`, `DATABASE_URL`, `SUPER_ADMIN_EMAILS` are in version control (verified via `git ls-files`; already in `.gitignore` but committed earlier).
- *Root cause:* File committed before being gitignored; never purged.
- *Files:* `backend/.env`, git history.
- *Risk:* `SECRET_KEY` signs all JWTs → anyone with repo access forges a token for any user/role/tenant = full auth bypass + impersonation. DB creds = direct data exfiltration.
- *Severity:* **Critical.**
- *Fix time:* 0.5 day code + coordinated secret rotation (JWT secret rotation invalidates all sessions — schedule a maintenance window) + history purge (BFG/`git filter-repo`) + force-push + teammate re-clone. ~1 day total.

**BUG-2 — Billing is single-tenant (`"default"` org)**
- *Description:* `get_summary` → `ensure_subscription(db)` with no org id → `DEFAULT_ORG_ID = "default"`; all billing tables key on that literal. Every tenant reads/writes one billing record.
- *Root cause:* Billing layer built before multi-tenancy was retrofitted; org id never threaded through.
- *Files:* `backend/app/billing/service.py:17,149`, `backend/app/billing/router.py:112`, `backend/app/org/router.py:557`; `BillingSubscription.organization_id` is `String` default `"default"` (`models.py:1544`) + all `billing_*` tables.
- *Risk:* Cross-tenant billing data exposure; wrong plan/seat/invoice per org; revenue integrity.
- *Severity:* **Critical.**
- *Fix time:* 3–5 days (thread real org id + data migration off `"default"` + backfill). Depends on org-identity decision (§3) — do the minimal "key on `organization_id`" fix now, full FK in Phase 3.

**BUG-3 — MFA is non-functional theater**
- *Description:* `UserMfaFactor` model doesn't exist; all MFA paths guarded by `hasattr(models,"UserMfaFactor")` → always 0 enrolled; login has no MFA step; `mfa_required` toggle does nothing.
- *Root cause:* Feature scaffolded in UI/config without the backend model or enforcement.
- *Files:* `backend/app/auth/service.py:525-632`, `workspace/security_center.py:302-316`, `workspace/router.py:593,748`.
- *Risk:* Customers believe MFA is enforced when it isn't — compliance/audit failure, breach exposure.
- *Severity:* **Critical** (as a security claim). It is a broken promise, but the fix is a real feature build, so it lands in **Phase 2B** (identity/auth), not Phase 1.
- *Fix time:* 2–3 weeks (real feature — see §Phase 2B).

**BUG-4 — Security policies stored but unenforced**
- *Description:* Session idle/absolute timeouts, `revoke_on_role_change`, IP allow-list, SSO enforcement, password `min_length`/breach rules persist in `AppSettings` but no runtime reads them. Password validation uses hard-coded rules.
- *Root cause:* Config UI built; enforcement middleware never written.
- *Files:* `workspace/security_center.py:520-614`, `workspace/router.py:1424-1442`, `auth/service.py:1025-1039`, `main.py` (no policy middleware).
- *Risk:* Same as BUG-3 — controls appear active but aren't.
- *Severity:* **Critical** (security claim).
- *Fix time:* 2–3 weeks. The generic policy-enforcement middleware lands in **Phase 2A** (security foundation); password-complexity-from-org-config is finished in **Phase 2B**.

### HIGH

**BUG-5 — `PUT /settings/update` references nonexistent `AppSettings.team_id`**
- *Description:* Org-level tracker/policy save filters `AppSettings.team_id.is_(None)` — model has `team_key`, not `team_id` → `AttributeError`/500.
- *Root cause:* Column renamed to `team_key`; this endpoint + its docstring not updated.
- *Files:* `backend/app/settings/router.py:160` (+ docstring `:102`), migration `e7ebc3f1fdb5` docstring.
- *Risk:* Any org-level save through this endpoint 500s. Impact depends on whether the frontend uses `/settings/update` or `/settings/scoped` (scope=org). The scoped path works.
- *Severity:* **High** (latent; possibly unreachable if UI only uses `/scoped`).
- *Fix time:* 30 min (rename to `team_key`, or delete the endpoint and route org saves through `/scoped`).

**BUG-6 — `integrity`/`active_time` `load_config` ignore `organization_id` (cross-tenant config bleed)**
- *Description:* Both read the first matching org-level row by key+null team/user **without** filtering `organization_id` → org A can read org B's engine tuning.
- *Root cause:* `load_config` written before per-org overrides existed.
- *Files:* `backend/app/integrity/config.py:117-123`, `backend/app/active_time/config.py:218-224`.
- *Risk:* Cross-tenant setting leakage; per-org overrides silently don't apply.
- *Severity:* **High.**
- *Fix time:* 30 min each (add `organization_id == org_id` filter + thread org id into callers).

**BUG-7 — Public Holidays persisted to `localStorage` (per-browser, not per-tenant)**
- *Description:* `PublicHolidaysTab` writes holidays/applicability/audit/suppressed to `localStorage`.
- *Root cause:* Built against browser storage instead of the existing `holidays` API.
- *Files:* `frontend/src/pages/SettingsPage.jsx:1605-1611,1752-1755`.
- *Risk:* Data loss on device change; no sync across admins; not tenant-isolated. (A real `holidays` table + endpoints already exist — this UI just isn't wired to them.)
- *Severity:* **High** (data integrity).
- *Fix time:* 2–3 days (wire the tab to `holidays/router.py`).

### MEDIUM

**BUG-8 — Unaudited security-relevant mutations**
- *Files:* `settings/router.py:133-202` (org-wide settings), `employees/router.py:544-593` (**role change**), `org/router.py:795-891` (team CRUD).
- *Risk:* No trail for privilege changes; audit gaps in compliance review.
- *Severity:* **Medium.** *Fix time:* 1 day.

**BUG-9 — `linked_status` reads global billing + unscoped audit count**
- *Files:* `org/router.py:557,597-608`.
- *Risk:* Wrong per-org status tiles; folded into BUG-2. *Fix time:* included in BUG-2.

### LOW

**BUG-10 — `profile_picture` accepted without scheme/content validation** (`auth/service.py:1184-1189`) — stored-XSS risk if rendered unsanitized. *Fix:* validate data-URL scheme + content type. 0.5 day.
**BUG-11 — Dead code:** `_LegacySecurityTab` (`WorkspaceSettingsTabs.jsx:824-935`), no-op ternary (`OrganizationSettingsTab.jsx:172-173`). *Fix:* delete. 15 min.
**BUG-12 — Identity cache 5-min TTL** doesn't invalidate on role change/deactivation (`auth/service.py:208-286`). *Fix:* invalidate cache entry on user mutation. 0.5 day.

---

## 3. Architecture Improvements (Step 3)

| Area | Current Design | Problems | Recommended Design | Migration Risk |
|---|---|---|---|---|
| **Multi-tenancy** | App-level `organization_id` filter, no RLS; 3 unlinked org tables; billing on `"default"` | Any forgotten filter = leak; billing broken; commercial keyed on String | Single canonical org identity; FK commercial/billing to it; add a shared `org_scoped_query` dependency or Postgres RLS | **High** (data migration across 3 models) |
| **Settings Storage** | 3 patterns: typed tables, `AppSettings` EAV Text bag, `AuditLog`-as-config | Untyped, unqueryable, hot-path uncached; audit-log scans grow unbounded | Keep `AppSettings` for tracker/policy keys (it's good); move security/integration/notification config to typed tables; kill audit-log-as-config | **Medium** |
| **Inheritance Engine** | 4-level cascade in `scope.py`, per-request queries | No Department tier; team = string; uncached | Extend to 5 levels (add Department); resolve by FK; cache effective map per (org,dept,team,user) w/ ws-invalidation | **Medium** (add level is additive; team→FK is the risky part) |
| **Permission System** | `role` String + scattered `role in (...)`; 4 different admin sets; real matrix only in offboarding | Inconsistent, unenforced, no separation-of-duties | Promote `offboarding/rbac.py` matrix app-wide; central `require_permission(perm)` dependency; roles/permissions tables | **Medium** |
| **Audit System** | 4 separate logs; `target_id` no FK; opaque JSON | No unified query; no tamper-evidence on `AuditLog` | Single audit surface (view or table) + correlation id + optional hash chain (WEC pattern exists) | Low–Medium |
| **Database** | See §4 | Stub org, no dept/team FK, blob-in-row, SET-NULL orphaning | See §4 | High |
| **API** | Mixed prefixes, overlapping surfaces | Duplicate SSO/holiday/security endpoints | Versioned `/api/v1`, deprecate duplicates, consistent auth dependency | Low |
| **Frontend** | 4 data clients, 2 toast systems, 2,500+ line files | Missed 401-refresh/dedup, a11y, bundle | Single axios client, shared primitives, code-split | Low |
| **State Mgmt** | Prop-drilling, non-memoized callbacks | Refetch churn | Memoize callbacks; consider a settings context/query cache (React Query) | Low |
| **Caching** | None on settings; 5-min identity cache | Hot-path re-query | Effective-settings cache keyed by scope tuple, invalidated on write | Medium |
| **Feature Flags** | Plan-gated JSON only | No per-org override, no eval API | Org feature-flag table + eval service + admin UI, layered over plan entitlements | Low (additive) |

**Redesign verdict:** Database and Multi-tenancy require **redesign** (not patch). Settings storage, Permission system, Inheritance engine require **significant refactor**. API/Frontend/Caching/Feature-flags are **incremental**.

---

## 4. Database Refactoring (Step 4)

| Entity | Verdict | Action |
|---|---|---|
| `app_settings` (tracker/policy keys) | **Keep** | Good design for its purpose. Add the COALESCE unique index to the model metadata; add org-scoped caching. |
| `security.*` / `integrations.*` / notification config in `AppSettings` | **Replace** | Move to typed tables (`security_policies`, `integration_connections`, `org_notification_settings`). |
| worker_settings / notifications config in `AuditLog` | **Replace** | Give them real tables; stop scanning the audit log to read config. |
| `organizations` (5-col stub) | **Modify** (major) | Make it (or `org_settings`) the canonical org record; add plan/seat/quota/region/domain or FK them in. |
| `org_settings` | **Modify** | Candidate canonical org record; reconcile with `organizations` + `commercial_organizations`. |
| `commercial_organizations` (String PK) | **Modify** | FK to `organizations.id`; retire the String/`operational_org_id` glue. |
| `billing_*` (`"default"` String key) | **Modify** (major) | Key on real `organization_id` FK; migrate the global record. |
| `Team` (no membership FK) | **Modify** | Add `team_id` FK on `users`; keep `teams` as source of truth; migrate `User.team` strings → FKs; repoint `AppSettings.team_key` → `team_id`. |
| `Department` | **Add** | New `departments` table + `department_id` FK on users; add the cascade tier. |
| `users.role` (String) | **Modify** | Back with roles/permissions tables; keep string for compat during migration. |
| `AuditLog` (+ 3 others) | **Modify** | Unify into one queryable surface; add `target_id` FK where possible + correlation id. |
| `AuditRetentionRule`, `Holiday`, `PayrollConfig`, `LeavePolicy` | **Keep** | Well-formed typed tables. |
| `User.profile_picture`, `Activity.thumbnail` (base64 Text) | **Modify** | Move to object storage; store URL reference. |
| Tenant FK `ondelete=SET NULL` (everywhere) | **Modify** | Change to `CASCADE` (or soft-delete + purge job); NULL-scoping leaks rows. |
| `tenants` / `workspaces` / `memberships` (unused scaffold) | **Decide** | Either adopt as the real tenancy model or remove — don't leave dead scaffolding. |
| Feature flags / API keys / webhooks / templates tables | **Add** | New tables in Phases 3–4. |

**Migration approach:** dual-write + backfill + verify + cutover (patterns already present in the repo — `backfill_missing_commercial_organizations`, `merge_heads`). Reconcile model metadata with raw-SQL migrations; **ban `create_all` in prod** (model diverges from migrated schema).

---

## 5. API Improvements (Step 5)

**Good (keep):** `/settings/scoped` (GET/PUT/DELETE) — clean override contract. `PUT /org/settings` (regex-validated, audited). `payroll/*` (best tenant isolation). `commercial/webhooks` (signature-verified, idempotent).

**Needs improvement:** four different admin-role sets → one auth dependency. Add rate limiting to org-settings routers (currently zero). Add pagination/filtering to list endpoints (audit, events, employees). Wrap audit writes so failures don't get silently swallowed.

**Duplicate (consolidate):** SSO written by both `workspace/router.py:1401` and `security_center.py:649`; holidays by both `holidays/` and `workspace/router.py:967`; MFA/IP toggles in two places. Pick one, deprecate the other.

**Deprecated (retire):** `PUT /settings/update` (broken `team_id`) → fold into `/settings/scoped` (scope=org). `_LegacySecurityTab` backend equivalents.

**Missing (add):** feature-flag eval API, API-key CRUD, outbound webhook subscriptions, settings-template apply, bulk scoped-settings apply, org export/import, SAML ACS + SCIM endpoints, department CRUD + department-scope in `/settings/scoped`.

**Cross-cutting:** adopt `/api/v1` versioning before adding the new surfaces; standard validation (pydantic) + pagination envelope; consistent error shape.

---

## 6. Frontend Improvements (Step 6)

| Area | Status | Action | Files |
|---|---|---|---|
| Data layer | 4 clients; raw-fetch paths miss 401-refresh/dedup | Consolidate on axios `API` | `OrganizationSettingsTab.jsx`, `workspace/_api.js`, `SecurityCenter.jsx`, `PoliciesTab` |
| Re-render churn | Non-memoized `showToast`/`onPreferencesChange` → refetch loops | Memoize callbacks | `SettingsPage.jsx:2642`, `SettingsModule.jsx:407-429` |
| Bundle | 2,973 / 2,519 / 1,372-line files, not code-split | Split large files, lazy-load tabs | `SettingsPage.jsx`, `WorkspaceSettingsTabs.jsx`, `SecurityCenter.jsx` |
| Accessibility | `SettingsPage` has **zero** `aria-`/`role=`; custom controls lack roles | Extract shared ARIA-complete `Switch`/`Modal`/`Field` (SettingsModule's are the reference) | ≥5 `Switch`, ≥4 `Modal` copies |
| Toast systems | Two (react-hot-toast vs hand-rolled) | Standardize on one | `SettingsPage.jsx` |
| Loading | Plain "Loading…" text, no skeletons | Add skeletons | all settings tabs |
| Dark mode | All use `useTheme()` **except** `AccountSettings` (hard-coded light) | Switch to `useTheme()` | `AccountSettings.jsx:11-26` |
| IA | Sidebar shows "Departments & Teams" but no dept scope | Resolve once Department tier lands | `SettingsPage.jsx:184` |
| Override UX | Best-in-class in `OrganizationSettingsTab`/`SettingsModule` | Add confirm-on-reset for parity | `OrganizationSettingsTab.jsx:260` |
| Data integrity | Holidays in `localStorage` | Wire to API (BUG-7) | `SettingsPage.jsx:1605` |
| Dead code | `_LegacySecurityTab`, no-op ternary | Delete | (BUG-11) |

(No live screenshots taken — this is a static audit; component/line refs stand in.)

---

## 7. Security Improvements (Step 7)

| Area | Current | Missing | Recommended | Priority |
|---|---|---|---|---|
| Authentication | JWT + per-request `is_active`; strong rotating refresh tokens | — | Keep; rotate `SECRET_KEY` (BUG-1) | P0 |
| Authorization / RBAC | `role` String, 4 admin sets, advisory permissions | Enforcement, separation-of-duties | Central `require_permission`; promote `offboarding/rbac.py` app-wide; roles/perms tables | P1 |
| Permission Matrix | Exists only for offboarding | App-wide matrix | Adopt it as the platform matrix | P1 |
| MFA | Non-functional | Everything (BUG-3) | `UserMfaFactor` + TOTP + login enforcement | P0 |
| SSO | Google only | SAML2, Okta, Microsoft, enforcement | Real SAML SP + enforce toggle | P2 |
| SCIM | Absent | Provisioning | SCIM 2.0 endpoints | P2 |
| Sessions | Expiry + revocation solid | Idle/absolute/IP not enforced (BUG-4) | Policy-enforcement middleware | P0 |
| Rate limiting | Auth routes only | All settings routers | Apply slowapi to settings routers + install middleware | P1 |
| Tenant isolation | App-level, gaps (BUG-2, BUG-6) | RLS/centralization | Shared org-scoped query dependency or Postgres RLS | P0/P1 |
| Audit logs | Broad but 4 logs, gaps (BUG-8) | Unified, tamper-evident | Single surface + correlation id + hash chain | P1 |
| Encryption | None field-level | PII/payroll/bank at-rest | Field-level encryption for sensitive columns | P2 |
| Secrets | Fail-fast, but committed (BUG-1) | — | Purge + rotate + secret manager | P0 |
| Data retention | Audit retention only | General per-org retention | Extend retention beyond audit | P2 |

---

## 8. Performance Improvements (Step 8)

| Area | Current | Issue | Recommendation | Expected Gain |
|---|---|---|---|---|
| Settings read | Up to 3 queries/`GET /settings`, uncached, tracker hot path | Query load scales with tracker count | Cache effective map per scope tuple, ws-invalidate | Large — removes N queries × trackers |
| worker_settings/notifications | Scan `audit_logs` to read config | Read cost grows with audit volume | Typed tables | Large |
| Notifications admin read | N+1 (`actor_id.in_(all org users)`) | Slow per org | Typed table + indexed query | Medium |
| Blob columns | base64 in `users`/`activities` | Bloats biggest tables, kills row cache | Object storage + URL | Large at scale |
| React re-renders | Non-memoized callbacks → refetch | Wasted requests/renders | Memoize (§6) | Medium |
| Bundle | Large files not code-split | Slow initial load | Code-split + lazy tabs | Medium |
| DB indexes | Mostly present, some retrofitted | `create_all` diverges from migrations | Reconcile metadata | Correctness |
| Concurrency | Last-write-wins on settings | Lost updates | Version column / optimistic lock | Medium |

---

## 9. Scalability Review (Step 9)

| Target | Supported today? | Why / blocker |
|---|---|---|
| 100 orgs | ✅ Yes | Fine, except billing is globally shared (BUG-2) — orgs see each other's billing now |
| 500 orgs | ⚠️ With BUG-2 fixed | Billing + no-RLS fragility; otherwise OK |
| 1,000 orgs | ⚠️ Conditional | Needs BUG-2 + settings cache; SET-NULL orphaning accumulates NULL-scoped rows |
| 10,000 orgs | ❌ Not yet | 3-table org model + String commercial keys + no RLS won't hold; needs Phase 3 foundation |
| 100,000 employees | ❌ Risk | Uncached cascade on tracker poll + base64 blobs in `users`/`activities` + audit-log-as-config scans = three cliffs |
| 1M screenshots/day | ⚠️ Storage-dependent | Screenshots are URL-referenced (good); thumbnails base64 in-row (bad); needs object-storage discipline |
| 100 concurrent admins | ✅ Likely | Admin surfaces are read-mostly; watch the frontend refetch churn |
| 500 concurrent trackers | ⚠️ | Each polls `GET /settings` (uncached, up to 3 queries) — cache is the fix; **the audit's "500 trackers" is low vs the 100k-employee goal — real target is far higher, so the cache is mandatory** |

**Verdict:** Comfortable to ~500–1,000 orgs *after* BUG-2 + settings cache. Beyond that requires the Phase 3 foundation work (org consolidation, RLS/centralized scoping, blob offload, config-out-of-audit-log).

---

## 10. Implementation Phases (Step 10)

| Phase | Theme | Contents | Est. | Goal |
|---|---|---|---|---|
| **Phase 1** | Critical Stability | Ordered: BUG-5 (`team_id`) → BUG-6 (org filter) → BUG-7 (holidays API) → BUG-8/10/11/12 (audit gaps, validation, dead code, cache invalidation) → **BUG-1 secret rotation (maintenance window)** → **BUG-2 billing `"default"` migration (after backups + testing)** | **1–2 weeks** | No broken settings, no cross-tenant leaks, stable production |
| **Phase 2A** | Security Foundation | RBAC unification + permission matrix (`offboarding/rbac.py` → app-wide `require_permission`); tenant-isolation centralization (shared org-scoped dependency / RLS on current schema); unified + tamper-evident audit logging; session enforcement; generic policy-enforcement middleware (BUG-4); rate limiting on settings routers | **6–8 weeks** | Enterprise-grade security architecture |
| **Phase 2B** | Identity & Authentication | MFA (BUG-3, `UserMfaFactor` + TOTP + login enforcement); Google/Microsoft SSO; SAML2 SP; SCIM 2.0 provisioning; password policies (complexity/breach from org config); trusted devices | **6–9 weeks** | Enterprise identity support |
| **Phase 3** | Platform Foundation | **The most important phase.** Organization model consolidation; Department model; Team hierarchy (team FK on users); settings inheritance engine (5-level cascade); cache layer; configuration engine (config → typed tables); organization templates. **The platform every later feature builds on.** | **8–12 weeks** | Stable, reusable platform |
| **Phase 4** | Enterprise Features | Feature flags; API keys; webhooks; integrations (OAuth connectors); developer APIs; automation; reports; compliance; branding; notifications | **8–12 weeks** | Enterprise capability parity |
| **Phase 5** | SaaS Commercial | Billing (full FK hardening); subscription plans; seat management; usage quotas; trial management; license management; organization cloning; import/export; backup/restore | **6–10 weeks** | Commercial SaaS readiness |
| **Phase 6** | Performance | Database optimization; caching; lazy loading; React optimization; API optimization; background jobs; queue processing | **3–4 weeks** | Scale to target load |
| **Phase 7** | UX Polish | Accessibility; responsive UI; better search; guided onboarding; settings templates UI; bulk editing; help documentation; visual polish | **3–4 weeks** | Polished, discoverable UX |

Phases 4–7 partly parallelize once the Phase 3 platform foundation lands. **Billing splits across phases:** the minimal `"default"`→real-org fix is Phase 1 (BUG-2, stops the leak); full billing/subscription/seat hardening is Phase 5 (on the consolidated org identity).

---

## 11. Priority Matrix / Sprint Plan (Steps 11 + 12 deliverable-13)

Sprints = 2 weeks. This is the sequenced backlog, grouped by phase.

| Item | Phase | Priority | Biz Value | Dev Cost | Risk | Dependencies | Sprint |
|---|---|---|---|---|---|---|---|
| BUG-5 `team_id` fix | 1 | P0 | H | 0.5d | Low | — | S1 |
| BUG-6 org-filter fix | 1 | P0 | H | 0.5d | Low | — | S1 |
| BUG-7 holidays → API | 1 | P0 | H | 2–3d | Low | — | S1 |
| BUG-8/10/11/12 | 1 | P1 | M | 2d | Low | — | S1 |
| BUG-1 .env purge + secret rotation | 1 | P0 | H | 1d | Med (session invalidation) | maintenance window | S1 |
| BUG-2 billing key on real org | 1 | P0 | H | 3–5d | Med (migration) | backups + test first | S1–S2 |
| RBAC unification + `require_permission` + permission matrix | 2A | P0 | H | 3–4w | Med | — | S2–S4 |
| Tenant-isolation centralization (dependency/RLS) | 2A | P0 | H | 1–2w | Med | RBAC | S4 |
| Unified + tamper-evident audit | 2A | P1 | H | 2w | Low | — | S5 |
| Session + generic policy-enforcement middleware (BUG-4) | 2A | P0 | H | 2–3w | Med | RBAC | S5–S6 |
| Rate limiting on settings routers | 2A | P1 | M | 3d | Low | — | S6 |
| MFA (BUG-3, TOTP + enforcement) | 2B | P0 | H | 2–3w | Med | RBAC stable | S7–S8 |
| Google/Microsoft SSO | 2B | P1 | H | 2w | Med | — | S8–S9 |
| SAML2 SP | 2B | P1 | H (sales) | 4w | Med | RBAC | S9–S11 |
| SCIM 2.0 provisioning | 2B | P2 | H (sales) | 2w | Med | RBAC | S11 |
| Password policies + trusted devices | 2B | P1 | M | 2w | Low | policy middleware | S12 |
| Org-identity consolidation | 3 | P1 | H | 4–6w | **High** | BUG-2 | S13–S15 |
| Department model + team FK + 5-level cascade | 3 | P1 | H | 4w | Med | org consolidation | S15–S17 |
| Config → typed tables | 3 | P1 | M | 3w | Med | — | S16–S17 |
| Settings cache + configuration engine | 3 | P1 | H | 2w | Med | cascade | S17–S18 |
| Organization templates (engine) | 3 | P2 | M | 2w | Low | cascade | S18 |
| Blob → object storage | 3 | P2 | M | 2w | Med | — | S18 |
| Feature flags (per-org) | 4 | P2 | H | 3w | Low | permission system | S19–S20 |
| API keys | 4 | P2 | M | 2w | Low | permission system | S20 |
| Webhooks + integrations (OAuth) | 4 | P2 | M | 6w+ | Med | API versioning | S21–S23 |
| Automation / reports / compliance / branding / notifications | 4 | P2 | M | 4w+ | Med | flags + config engine | S23+ |
| Billing/subscription/seat/quota/trial/license hardening | 5 | P2 | H | 6–8w | Med | org consolidation | S24+ |
| Org cloning + import/export + backup/restore | 5 | P3 | M | 4w+ | Med | stable schema | S27+ |
| Performance (DB/cache/React/API/queues) | 6 | P2 | M | 3–4w | Low | all above | S29+ |
| UX polish + settings-templates UI + bulk editing + onboarding | 7 | P2 | M | 3–4w | Low | frontend refactor | S31+ |

---

## 12. Recommended Development Order (Step 12) — with rationale

1. **Critical bugs** — safety/data-integrity first; the .env leak makes everything else moot, and the billing/config-bleed bugs are active cross-tenant exposure.
2. **Database cleanup (metadata reconcile, SET-NULL, ban create_all)** — before any migration-heavy work, so the schema you migrate against is trustworthy.
3. **Permission system / security foundation (Phase 2A)** — everything downstream (settings writes, billing, integrations, API keys, *and MFA/SSO*) needs enforced authZ; building anything on string-role checks means re-touching it all later. Tenant-isolation centralization and unified audit ride here too.
4. **Identity & authentication (Phase 2B)** — MFA/SSO/SAML/SCIM close the "theater" liability but are customer-facing *features* that gate on the Phase 2A permission model for admin enforcement. Doing them before 2A means reworking their authZ.
5. **Platform foundation (Phase 3: org consolidation + Department + team FK + inheritance engine + cache + config engine + templates)** — the foundation every settings feature sits on; adding the Department tier and consolidating org identity *before* building feature flags/templates avoids reworking them.
6. **Organization Settings surface (typed config, frontend refactor)** — now that the engine is right, make the org-facing surface consistent.
7. **Billing/subscription hardening** — full FK + seat/quota, on top of the corrected org identity.
8. **Integrations** — need enforced permissions + API versioning already in place.
9. **Feature flags** — layered over plan entitlements + permission system; unlocks gating for later features.
10. **API keys + webhooks** — developer surface; depends on permission system + feature flags for gating.
11. **SAML/SCIM** — enterprise-sales unlock; depends on RBAC + org identity.
12. **Templates** — sit on top of the cascade + feature flags.
13. **Reports / automation** — consume the now-consistent data model.
14. **Backup & restore / import-export / cloning** — safe only once the schema is stable (doing this before consolidation means rewriting the exporters).
15. **Optimization & scale hardening** — last, against the finished system, validated by load tests.

Each step precedes the next because it removes a dependency or a source of rework for what follows: fix danger → trustworthy schema → enforced authZ → real security → sound engine → consistent surface → features that gate on all of it → scale-test the whole.

---

## 13. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Secret rotation breaks all sessions | Certain (by design) | Medium | Scheduled maintenance window; comms; refresh-token re-issue |
| Org-identity migration data loss | Medium | Critical | Dual-write + backfill + verify + reversible cutover; snapshot first |
| Billing migration mis-maps orgs | Medium | High | Reconcile against commercial layer; dry-run + audit before cutover |
| RBAC rollout locks out admins | Medium | High | Ship matrix in shadow/log-only mode first; compare to legacy checks; then enforce |
| MFA rollout locks out users | Medium | Medium | Opt-in → enforce with grace period + recovery codes + break-glass |
| Model/DB drift causes migration failures | Medium | Medium | Reconcile metadata early (step 2); CI check model==migrations |
| Scope creep into Phase 4 before Phase 3 done | High | High | Gate: no enterprise feature merges until foundation phase closes |
| Team-string → FK migration orphans overrides | Medium | Medium | Backfill `team_key`→`team_id` in the same migration; verify counts |

---

## 14. Estimated Timeline

| Milestone | Cumulative | Target score |
|---|---|---|
| Phase 1 complete (critical stability) | Week 2 | ~50 |
| Phase 2A complete (security foundation) | ~Week 10 | ~60 |
| Phase 2B complete (identity/auth) | ~Week 18 | ~66 |
| Phase 3 complete (platform foundation) | ~Week 28 | **~72 — credible enterprise baseline** |
| Phase 4 complete (enterprise features) | ~Week 38 | ~80 |
| Phase 5 complete (SaaS commercial) | ~Week 44 | ~84 |
| Phases 6–7 complete (performance + UX) | ~Week 48–52 | **~88 — parity with comparison set** |

Assumes 1–2 engineers; Phases 4–7 parallelize with more headcount. **Credible enterprise baseline: ~6.5 months. Full parity: ~11–12 months.** (Longer than the prior 7-phase estimate because 2A/2B run sequentially rather than as one bundled security phase — a deliberate trade of calendar time for less rework.)

---

## 15. Final Enterprise Readiness Score

**Current: 41 / 100** (carried from the audit).
**Trajectory:** Phase 1 → ~50 · Phase 2A → ~60 · Phase 2B → ~66 · Phase 3 → **~72 (ship-to-enterprise baseline)** · Phase 4 → ~80 · Phase 5 → ~84 · Phases 6–7 → **~88 (competitive parity)**.

**The sequencing rules that govern the plan:** (1) stabilize (Phase 1) → build the security **foundation** (2A) → build security **features** on it (2B); (2) finish the platform **foundation** (Phase 3) before any enterprise feature (Phase 4+). Skipping ahead in either chain guarantees rework.

---

### Next steps (as you outlined)
1. **Phase 1 implementation prompts** — critical stability only, in order: BUG-5 → BUG-6 → BUG-7 → BUG-8/10/11/12 → BUG-1 (maintenance window) → BUG-2 (after backups + testing). Smallest, safest, highest-urgency diffs.
2. **Phase 2A prompts** — security foundation (RBAC + permission matrix, tenant-isolation centralization, unified audit, session/policy enforcement).
3. **Phase 2B prompts** — identity/auth (MFA, SSO/SAML, SCIM, password policies, trusted devices).
4. **Phase 3+** — platform foundation, then enterprise features in the order above.

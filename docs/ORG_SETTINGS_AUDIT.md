# Temveris — Organization Settings Enterprise Readiness Audit

**Date:** 2026-07-16
**Scope:** Complete audit of the Organization Settings module (backend + frontend + database + security + SaaS/tenant isolation).
**Nature:** Read-only discovery. No code was modified. File:line references throughout so implementation can be planned without a second discovery pass.

---

## 1. Executive Summary

Temveris has the **surface area** of an enterprise settings product — teams, leave/holiday/payroll policies, a Security Center, an RBAC catalog, a billing/subscription engine, audit logging, data-retention rules — but underneath it is **three disconnected settings architectures stitched together reactively**, with several capabilities that are UI-only (stored but never enforced) and at least two outright defects that break the primary save path and tenant isolation.

The core, purpose-built settings module (`app/settings/`) is genuinely good: a clean `system → org → team → employee` cascade with proper reset-to-inherit, per-key override views, and a websocket push. But:

- The **inheritance cascade is 4 levels, not the 5 you specified** — there is **no Department tier** anywhere (no `Department` model, no department scope in the UI). "Department" exists only as a free-text string on `User`.
- The primary org-level save endpoint (`PUT /settings/update`) **references a column that does not exist** (`AppSettings.team_id`) and throws `AttributeError` at runtime. (Confirmed against the model.)
- Organization configuration is **fragmented across three tables** (`Organization` — a 5-column stub; `OrgSettings` — profile/branding; `AppSettings` — key/value) plus a parallel `commercial_*` universe keyed by a **String** org id, and a `billing_*` universe keyed by the literal string `"default"`. **Billing is effectively single-tenant.**
- Multiple "enterprise" controls are **theater**: MFA (`UserMfaFactor` model does not exist), session timeout / IP allow-list / SSO enforcement / password policy are all **stored in the DB but read by no runtime code**.
- **`backend/.env` is committed to git** with a live `SECRET_KEY` and `DATABASE_URL`. This is a critical, exploitable finding (forge any JWT → full auth bypass).

**Verdict:** Not enterprise-ready as-is. Strong bones (auth token design, audit coverage, the settings cascade) undermined by architectural fragmentation, non-functional security toggles, and a broken billing tenancy model. Estimated remediation to a credible enterprise baseline: **~4–6 engineer-months** (see §16).

**Final Enterprise Readiness Score: 41 / 100** (see §18 for the breakdown).

---

## 2. Current Architecture

### 2.1 The core settings cascade (the good part)
`backend/app/settings/scope.py` + `router.py`.

- Storage: one key/value table `app_settings` — columns `organization_id`, `team_key` (String), `user_id`, `key`, `value` (Text), `description`, `updated_by`, `updated_at`.
- Cascade resolution (`scope.py:114-126`): `defaults ← org ← team ← employee`, each level a separate query, merged in Python.
- Scope disambiguation by NULLs: org = `team_key IS NULL AND user_id IS NULL`; team = `team_key = <name>`; employee = `user_id = <id>`.
- Admin UI contract: `GET /settings/scoped` returns per-key `{value, parent_value, is_override, default, type, options…}`; `PUT /settings/scoped` upserts; `DELETE /settings/scoped` resets-to-parent.
- Keys are an allow-list (`DEFAULTS`, `router.py:38-97`) merged from four config modules (`active_time`, `attendance_policy`, `integrity`, plus the core tracker keys).

### 2.2 The three-headed "organization" model
| Notion | Table | Key type | Holds |
|---|---|---|---|
| Operational org | `organizations` | Integer PK | `id, name, is_active, created_at, updated_at` — **nothing else** |
| Profile/branding | `org_settings` | Integer FK | company name, logo, colors, tz, currency, country, contacts, owner |
| Commercial org | `commercial_organizations` | **String PK** | domain, billing linkage — joined back only via nullable `operational_org_id` |
| Billing | `billing_*` (10 tables) | **String `"default"`** | plans, subscription, invoices, tax, contacts — **not** FK'd to `organizations` |
| Tenant/region | `tenants`, `workspaces`, `memberships` | Integer FK | isolation_mode, region_code — a multi-tenant scaffold that the rest of the app does not use |

These are **not linked by foreign keys**; they are reconciled in application code (and often not at all).

### 2.3 Settings storage patterns (three, inconsistent)
1. **Typed tables** — `OrgSettings`, `Holiday`, `Team`, `PayrollConfig`, `AuditRetentionRule`, `LeavePolicy`, `billing_*`.
2. **`AppSettings` key/value rows** — tracker config, security toggles, integrations (`integrations.state_json`), custom roles, SSO config.
3. **`AuditLog` payloads used as a settings store** — `worker_settings` and `notifications` persist live config as audit rows; every read scans the audit log.

### 2.4 Backend module map (11+ modules touching "org settings")
`settings/`, `org/`, `workspace/{router,security_center,audit_governance}`, `worker_settings/`, `attendance_policy/`, `leave/`, `holidays/`, `billing/`, `commercial/`, `payroll/`, `notifications/`, `integrity/`, `active_time/`.

### 2.5 Frontend map
- `OrganizationSettingsTab.jsx` (317 lines) — the only true org/team/employee scoped-cascade UI.
- `SettingsPage.jsx` (2,973 lines) — the shell + many inline tabs (teams, employees, rebrand, leave, **PublicHolidays persisted to `localStorage`**, track-settings).
- `WorkspaceSettingsTabs.jsx` (2,519), `SecurityCenter.jsx` (1,372), `AccessGovernanceCenter.jsx` (1,085) — workspace tabs.
- `SettingsModule.jsx` (1,127) — personal preferences (best-engineered file).
- `AccountSettings.jsx` (288) — personal profile.

---

## 3. Current Settings Inventory

### 3.1 Tracker / policy settings (the `app_settings` cascade — org/team/employee overridable)
All keys below are org-overridable, team-overridable, and employee-overridable via `/settings/scoped`. Validation is `KEY_META`-driven (`scope.py:32-79`); DB stores everything as Text.

| Category | Key | Default | Type | Consumer | UI (KEY_META) |
|---|---|---|---|---|---|
| Screenshot Monitoring | `screenshot_enabled` | `true` | bool | desktop tracker | ✅ |
| Screenshot Monitoring | `screenshot_interval` | `1800` | enum (60–3600s) | desktop tracker | ✅ |
| Screenshot Monitoring | `screenshot_blur` | `false` | bool | desktop tracker | ✅ |
| Attendance | `idle_threshold_minutes` | `15` | int (1–120) | desktop tracker | ✅ |
| Working Hours | `work_hours_threshold` | `8:45` | enum (HH:MM) | risk router | ✅ |
| Notifications | `risk_alert_enabled` | `true` | bool | risk router | ✅ |
| Activity Tracking | `track_mouse` | `true` | bool | desktop tracker | ✅ |
| Activity Tracking | `track_keyboard` | `true` | bool | desktop tracker | ✅ |
| Activity Tracking | `track_apps` | `true` | bool | desktop tracker | ✅ |
| Activity Tracking | `track_browser` | `true` | bool | desktop tracker | ✅ |
| Activity Tracking | `track_urls` | `true` | bool | desktop tracker | ✅ |
| (hidden) | `active_time.*` weights/thresholds | see `active_time/config.py` | int/float | Active Time engine | ❌ in DEFAULTS, not in UI |
| (hidden) | `integrity.*` (incl. `automation_processes`) | see `integrity/config.py` | mixed | integrity engine + tracker | ❌ not in UI |
| (hidden, dormant) | `early_clockout_enabled`, `disconnect_detection_enabled`, `disconnect_grace_minutes` | OFF | bool/int | **nothing reads them** | ❌ |
| (hidden) | `expected_work_durations`, `flexible_clock_out_days` | — | JSON/list | attendance_policy resolvers | ❌ |

**Note:** Advanced engine tunables (`active_time.*`, `integrity.*`) are accepted by the PUT validator and returned by `GET /settings` but are deliberately hidden from the admin UI (`scope.py:155-157`). They are org-overridable in principle but **`load_config` for both engines ignores `organization_id`** (see §7/§9) — so per-org overrides do not actually take effect and can read another tenant's row.

### 3.2 Org profile / branding (typed — `org_settings` table, org-scope only)
`PUT /org/settings` (`org/router.py:239`). **No team/employee override.** Regex-validated (`org/router.py:39-44`), diffed to AuditLog.

`company_name, logo_path, logo_url, slug, internal_description, primary_country, default_timezone, default_currency, hr_email, support_email, company_address, company_phone, company_website, company_description, primary_brand_color, secondary_brand_color, owner_user_id`. Data residency (region) is **read-only/env-driven** (`org/router.py:34-36`).

### 3.3 Security settings (stored in `app_settings`, org-scope; mostly UNENFORCED)
Via `workspace/security_center.py` and `workspace/router.py`: `security.mfa_required`, `security.sso_enabled`, SSO provider config, `password-policy` (min_length, breach rules), `sessions-policy` (idle_minutes, absolute_hours, revoke_on_role_change), IP allow-list rules, trusted domains, invite restrictions, exceptions, break-glass. **See §7 — most of these are written but never read by runtime code.**

### 3.4 RBAC (custom roles — stored in `app_settings` JSON)
`ROLE_CATALOG` of 8 system roles (`workspace/router.py:73-200`); `POST /roles` creates custom roles against a permission whitelist. Permissions are **advisory** (returned to the UI) — actual enforcement is still coarse `role in (...)` string checks.

### 3.5 Policy config (typed or key/value, org-scope only)
- **Leave**: types, policies, shared-pool, waiting-period — stored as `AppSettings` JSON **double-scoped** with key suffix `:{org_id}` (inconsistent — `leave/router.py:2033`).
- **Holidays**: `holidays` table, CSV import, per-org seeded calendar.
- **Payroll**: `PayrollConfig` table (best isolation of the group).
- **Attendance policy**: `AppSettings` rows resolved by `attendance_policy/resolvers.py`.
- **Data retention**: `AuditRetentionRule` table (real, per-org, audited).

### 3.6 Personal preferences (`user_preferences` + `worker_settings` — per-user)
Display/accessibility, language/region, workday prefs, app behavior. Supports **workspace policy locks** (best inherited/override UX in the product). Stored partly in `UserPreferences` table, partly as `AuditLog` payloads.

**Inventory gaps (settings a peer product has that Temveris has no field for):** company legal entity/tax id at org level, working-week definition, overtime rules, geofencing/IP-based attendance, per-project settings, notification routing/escalation, SLA/approval-chain config beyond leave, per-org feature flags, data-export/retention beyond audit, API/webhook config.

---

## 4. Inheritance Audit (Step 2)

Target hierarchy you specified: **System → Organization → Department → Team → Employee.**
Implemented hierarchy: **System → Organization → Team → Employee.**

| Check | Result | Evidence |
|---|---|---|
| Inheritance works | ✅ for the 3 override tiers | `scope.py:114-126` merges default←org←team←employee correctly |
| Overrides work | ✅ (team/employee via `/settings/scoped`) | `router.py:278-320` |
| Reset-to-inherited | ✅ | `DELETE /settings/scoped` deletes the row → falls back to parent (`router.py:330-353`); UI ↺ button |
| Effective value calc | ✅ (per-user via `GET /settings`) | `router.py:108-119` |
| **Department tier** | ❌ **MISSING ENTIRELY** | No `Department` model; no dept scope in `SCOPES` (`OrganizationSettingsTab.jsx:26-30`) |
| Cascading updates | ⚠️ Partial | A team override reaches trackers via websocket + 60s re-poll, but **team membership is a string** — renaming a team orphans `team_key` rows (no cascade) |
| Inheritance conflicts | ⚠️ | Team scope keys on `User.team` **string**, not `teams.id`. A user whose `team` string doesn't match any override simply inherits org — silent, not an error |
| Org-level save path | ❌ **BROKEN** | `PUT /settings/update` filters `AppSettings.team_id` which **does not exist** on the model → `AttributeError`. The `/settings/scoped` path (scope=org) works; the legacy `/update` path is a latent 500 |

**Additional inheritance defects:**
- Advanced-engine keys (`active_time.*`, `integrity.*`) are org-overridable in the data model but their `load_config` **ignores `organization_id`** (`integrity/config.py:117`, `active_time/config.py:218`) — so overrides don't take effect and may read another tenant's value.
- No inheritance for org-profile, security, leave, payroll, holidays — those are **org-scope only** (no team/employee tier), which is a legitimate product gap for enterprises that delegate policy to business units.

---

## 5. Database Audit (Step 3)

**Files:** `backend/database/models.py` (~1,937 lines, ~55 tables), `backend/app/commercial/models.py` (598), `backend/app/offboarding/models.py`, alembic with **55 revisions**.

### Confirmed defects
1. **`AppSettings.team_id` referenced but nonexistent** — model has `team_key` only (`models.py:1046`); `settings/router.py:160` uses `team_id` → runtime error. Docstrings in the router and migration also say "team_id".
2. **`Organization` is a 5-column stub** (`models.py:1819-1834`) — no branding/plan/tz/currency/seats/quota/domain/region/owner. All scattered to `OrgSettings`, `Tenant`, `billing_*`, `commercial_*`.
3. **No `Department` table.** `Department` is a free-text `User.department` string (`models.py:65`).
4. **Team membership has no FK.** `User.team` is `String(100)` (`models.py:69`); a parallel `teams` table exists (`models.py:1769`) but nothing links a user row to a team row. Renames/deletes silently orphan `AppSettings.team_key`.
5. **`role` is an unconstrained `String(50)`** (`models.py:64`) — no enum/lookup/check. All authorization depends on scattered string literals.
6. **Billing keyed by `String "default"`** (`models.py:1544`) — not FK'd to `organizations`. `commercial_*` uses `String(40)` org ids with several tables carrying it as a plain, FK-less string.

### Normalization / scalability concerns
- **EAV bag in a Text column** — `AppSettings.value` holds "true"/"false", ints, and JSON blobs (`security.*`, `integrations.state_json`). No typing, no value-indexing; every read does `json.loads` in try/except. On the tracker hot path the cascade runs multiple queries per fetch.
- **Base64 blobs in-row** — `User.profile_picture` and `Activity.thumbnail` are base64 `Text` columns. At 100k employees + screenshot cadence this bloats the largest tables; belongs in object storage.
- **`ondelete="SET NULL"` on every tenant FK** — deleting an org **orphans** its rows to `organization_id = NULL` rather than removing/isolating them; combined with nullable `organization_id`, NULL-scoped rows accumulate and leak into any query that forgets to filter.
- **Four separate audit logs** (`audit_logs`, `billing_audit_events`, `commercial_events`, `offboarding_audit_events`) — no unified trail; `AuditLog.target_id` has no FK; `payload` is opaque JSON.
- **JSON-as-bag** beyond settings: `BillingPlan.feature_flags_json`, `LeavePolicy.approval_chain`, `EntitlementsCache.profile_json` — can't query "which orgs have feature X" without scanning.

### Indexes / FKs / migration quality
- Most operational tables index `organization_id`; **but `users.organization_id` index was retrofitted** (`c1d2e3f4a5b6`) and ~24 of 55 migrations are single-purpose "add organization_id to X" — **tenant scoping was retrofitted table-by-table, not designed in.**
- The real uniqueness for `app_settings` is a **COALESCE expression index created only in raw SQL migration** (`e7ebc3f1fdb5:47-50`) — invisible to SQLAlchemy metadata, so `create_all` in dev diverges from migrated prod.
- Heavy use of `op.execute("CREATE TABLE IF NOT EXISTS…")` idempotency hacks; presence of `merge_heads` and multiple "add missing columns/tables/backfill" migrations indicates repeated model/DB drift patched reactively.

### Missing tables (enterprise)
Feature flags (per-org), API keys, outbound webhook subscriptions, integration configs, general data-retention (only audit retention exists), backup/restore metadata, settings templates, seat/quota limits, unified audit, `Department`, `team_membership` join.

---

## 6. Backend Audit (Step 4)

| Dimension | Finding |
|---|---|
| REST consistency | ⚠️ Mixed prefixes/patterns: `/settings`, `/org`, `/api/workspace/*`, `/worker`, `/leave`, `/payroll`, `/api/admin/billing`, `/api/v1/billing/webhooks`. Overlapping endpoints (holidays exposed by both `holidays/` and `workspace/router.py:967`; SSO written by both `workspace/router.py:1401` and `security_center.py:649`). |
| Authentication | ✅ Consistent `get_current_user` (JWT, per-request `is_active` check). |
| Authorization | ❌ **Four different admin-role sets** guard "org settings": `{admin,hr}` (org/holidays/payroll/leave), `{admin,hr,owner,billing_admin,super_admin}` (workspace), `{admin,hr,billing_admin,owner}` (billing), `{admin,hr,owner,payroll_admin,finance_admin}` (notifications). Inconsistent bar per page. |
| Validation | ✅ Good in `settings`, `org` (regex), `worker_settings`, `attendance_policy`, `payroll`. ⚠️ Weak/absent pre-save validation client-side (server catches). |
| Error handling | ✅ Generally clean HTTPException; ⚠️ audit writes wrapped in `try/except: pass` swallow failures. |
| Logging / audit | ✅ Strong coverage on Security Center & org profile. ❌ **Unaudited:** `PUT /settings/update` (org-wide tracker settings), employee update incl. **role change**, team CRUD. |
| Caching | ⚠️ In-process 5-min identity cache only; **no settings cache** — cascade re-queries per request. |
| Performance | ⚠️ Settings cascade = up to 3 queries/fetch on tracker hot path; notifications does N+1 (`actor_id.in_(all org user ids)`); worker_settings/notifications **scan the audit log** to read config. |
| Transactions | ✅ Single `db.commit()` per handler, generally correct. |
| Concurrency | ⚠️ No optimistic locking / version columns on settings; last-write-wins. |
| Rate limiting | ❌ `rate_limit.py` (slowapi) applied only to auth/employees/screenshots/offboarding/commercial — **zero coverage on any org-settings router**. |
| Duplicate logic | ❌ SSO (2 surfaces), holidays (2 surfaces), MFA/IP toggles (2 surfaces), settings-as-AuditLog reimplemented in worker_settings + notifications, leave double-scoping. |

### Confirmed tenant-isolation bugs in the backend
- **Billing globally shared** — `get_summary` → `ensure_subscription(db)` with no org id → `DEFAULT_ORG_ID = "default"` (`billing/service.py:17,149`; `router.py:112`). Every tenant reads/writes one billing record.
- **`integrity`/`active_time` `load_config` ignore `organization_id`** — takes the first matching org-level row regardless of tenant (`integrity/config.py:117`, `active_time/config.py:218`).
- `org/router.py:557` `linked_status` reads `BillingSubscription.organization_id == "default"` and its `audit_count` uses an unscoped user-id `in_()` pattern.

---

## 7. Security Audit (Step 7)

### CRITICAL
- **`backend/.env` is committed to git** (verified: `git ls-files backend/.env` returns it; it's also listed in `.gitignore` line 42 but was committed before being ignored). Contains live `SECRET_KEY`, `DATABASE_URL`, `SUPER_ADMIN_EMAILS`. `SECRET_KEY` signs all JWTs → **anyone with repo access can forge a token for any user_id/role/tenant = full auth bypass**. Also a direct DB-credential leak. **Rotate key + DB creds, purge from history, keep it gitignored.**

### HIGH
- **MFA is entirely non-functional.** The `UserMfaFactor` model **does not exist**; every MFA code path is guarded by `hasattr(models, "UserMfaFactor")` → always 0 enrolled. Login (`service.py:525-632`) has **no MFA step**. `security.mfa_required` / SSO `enforce_for_admins` are toggles that do nothing.
- **Security "policies" stored but unenforced** — session idle/absolute timeouts, `revoke_on_role_change`, IP allow-list, SSO enforcement, and password `min_length`/breach rules are persisted in `AppSettings` but **no runtime code reads them** (`main.py` has no middleware for them). Password validation uses a hard-coded rule set, not the org's configured policy.

### MEDIUM
- **RBAC is inconsistent hard-coded strings** (four admin sets, §6). The real permission matrix (`offboarding/rbac.py`) is used **only** for offboarding. An `admin`/`hr` can promote any employee to `admin` with **no separation-of-duties**.
- **No row-level security** — tenant isolation is convention-only per-query. Any future endpoint that forgets `organization_id` becomes a cross-tenant leak. (A `test_tenant_isolation.py` regression test exists — a genuine strength.)
- **Identity cache TTL 5 min** — role changes and deactivations lag up to 5 minutes; role change does not invalidate the cache.
- **Unaudited security-relevant mutations** — `PUT /settings/update`, employee role change, team CRUD.

### LOW
- Rate limiting only on auth routes; login at 200/min per IP (weak brute-force protection, partially offset by per-email OTP throttle + account lock).
- **No field-level encryption** for PII/payroll/bank/screenshots — relies on infra at-rest encryption only.
- `profile_picture` accepted as a 2 MB base64 data URL with **no scheme/content validation** — potential stored-XSS if the SPA renders it unsanitized.
- CSRF relies solely on SameSite=lax; `COOKIE_SECURE` defaults to `0` (must be set in prod). Account enumeration on `/auth/forgot-password` by design.

### Security strengths (credit where due)
- Rotating, reuse-detecting, SHA-256-hashed refresh tokens with family revocation (`tokens.py`) — genuinely enterprise-grade.
- Per-request `is_active` enforcement closes the post-offboarding access gap.
- bcrypt passwords, HMAC-SHA256 OTP (plaintext never stored), constant-time compares.
- SSO invite-only, `email_verified`-gated, HMAC-signed state, cross-tenant same-email disambiguation.
- CORS fails safe (no wildcard-with-credentials); secrets fail-fast on missing `SECRET_KEY`/`DATABASE_URL`.
- **No SQL injection found** — ORM with bound params throughout; the one `text()` f-string interpolates an int constant.

### SSO / SAML / SCIM / MFA status
- **SSO: Google only** (real OAuth2 code-flow). `microsoft`/`okta`/`saml` are **config placeholders with no handshake**.
- **SAML: not implemented. SCIM: not present. MFA: non-functional** (see above).

---

## 8. Performance Audit (Step 8)

### Backend
- Settings cascade = up to 3 queries per `GET /settings`, on the tracker hot path, no cache. At 100k trackers polling this is the top scaling risk.
- Notifications N+1: collects all org user ids then `actor_id.in_(...)` against the audit log.
- worker_settings/notifications read config by **scanning `audit_logs`** — grows unbounded; read cost rises with audit volume.
- Base64 blobs in `users`/`activities` bloat the largest tables and kill row-cache efficiency.

### Frontend
- **Bundle**: `SettingsPage.jsx` = 2,973 lines, `WorkspaceSettingsTabs.jsx` = 2,519, `SecurityCenter.jsx` = 1,372 — none code-split (only the `admin/` monitoring tabs are). Four settings files exceed 1,000 lines.
- **Re-render churn**: `showToast` is passed as a **non-memoized inline function** from `SettingsPage` (`:2642`), so `OrganizationSettingsTab`'s `load` identity changes → **refetches `/settings/scoped` on unrelated parent re-renders**. Same risk in `SettingsModule`'s `onPreferencesChange` effect.
- **Fragmented data layer** — settings code calls the backend **four different ways**: axios `API` (dedup + 401-refresh), `apiFetch` (axios wrapper), `_api.js` raw `fetch`, and `OrganizationSettingsTab`'s own `authGet/authSend`. **Only the axios paths get 401-refresh and in-flight dedup**; the raw-fetch paths silently miss both.
- On mount `OrganizationSettingsTab` chains `/employees` → `/org/teams` → `/settings/scoped`; `PoliciesTab` fires 5 requests via `Promise.all` mixing `wsGet` and raw `fetch`.
- **No skeletons anywhere** — plain "Loading…" text; no `React.memo` on sub-components.

---

## 9. SaaS Readiness Audit (Step 9)

| Check | Status | Evidence |
|---|---|---|
| Tenant isolation | ⚠️ App-level only, no RLS; **broken in billing** and **weak in integrity/active_time/notifications/worker_settings** |
| Tenant settings | ⚠️ Fragmented across 3 tables + `commercial_*` |
| Billing isolation | ❌ **Broken** — global `"default"` subscription shared by all tenants |
| Storage isolation | ⚠️ Shared DB, `isolation_mode="shared_db"` on `tenants`; screenshots by URL — no per-tenant bucket enforcement verified |
| Org branding | ✅ `OrgSettings` colors/logo (org-scope) |
| Subscription plans | ✅ `billing_plans` + `commercial` engine (Stripe, dunning, trials, entitlements) — mature, but keyed on String org |
| Feature availability | ⚠️ Plan-gated entitlements exist (`commercial/entitlements.py`); **no per-org feature-flag admin surface** |
| Custom domains | ⚠️ `commercial_organizations.domain` only; not wired to routing |
| Org quotas | ❌ No quota columns/enforcement |
| Seat limits | ⚠️ `BillingPlan.user_limit` + `billbillable_users` counters exist; enforcement not verified |
| Usage tracking | ⚠️ Commercial layer tracks billable/active users; no general usage metering |

**Multi-region:** display-only (`Tenant.region_code` default `ap-south-1`, `org/router.py` residency string is read-only). **Org cloning / templates / backup-restore:** absent.

---

## 10. UX Audit (Step 10)

**Strengths:** `OrganizationSettingsTab` has the best scoped-override UX (scope selector, per-row Overridden/Inherited/System-default badges, "Inheriting: <value>" hint, ↺ reset). `SettingsModule` has the best inherited-lock UX (policy locks show "Managed by workspace" + version + "View policy") and the best accessibility.

**Problems:**
- **IA mismatch**: sidebar advertises a "Departments & Teams" tab (`SettingsPage.jsx:184`) but there is **no Department scope** in the cascade — departments are only a filter dimension.
- **Two toast systems** in one product (`react-hot-toast` vs a hand-rolled `showToast` banner).
- **Accessibility scorecard**: `SettingsModule` good → workspace/org tabs sparse (`title=` only, custom controls lack roles) → **`SettingsPage` (2,973 lines) has zero `aria-`/`role=`**. Custom `Switch`/`Toggle` reimplemented ≥5× — only one copy has ARIA.
- **No skeletons**; reset in `OrganizationSettingsTab` fires with **no confirmation** (inconsistent with delete-confirm modals elsewhere).
- **`AccountSettings` ignores dark mode** (hard-coded light palette).
- **Not mobile-friendly** — fixed 230px aside doesn't collapse (acceptable for a desktop admin console, but worth stating).
- **Data-integrity smell**: `PublicHolidaysTab` persists holidays/applicability/audit **to `localStorage`** (`SettingsPage.jsx:1605-1611`) — **per-browser, not per-tenant**; won't survive a device change or sync across admins.

---

## 11. Gap Analysis (Step 11)

| Feature | Current Status | What's Missing | Priority | Complexity | Impact |
|---|---|---|---|---|---|
| `.env` secret exposure | Committed to git | Rotate + purge history + gitignore | **P0** | Low | Critical |
| `PUT /settings/update` `team_id` bug | Broken (AttributeError) | Fix to `team_key` (or delete endpoint, use `/scoped`) | **P0** | Trivial | High |
| MFA | Non-functional theater | `UserMfaFactor` model + TOTP enroll/verify + login enforcement | **P0** | High | Critical |
| Unenforced security policies | Stored, not read | Middleware to enforce session/IP/SSO/password policy | **P0** | Med | Critical |
| Billing tenancy | Global `"default"` | Key billing on real org id; migrate data | **P0** | Med | Critical |
| Department tier | Absent | `Department` model + FK + cascade level + UI scope | **P1** | Med | High |
| Team membership FK | String match | `team_id` FK on users + membership join | **P1** | Med | High |
| RBAC enforcement | Advisory only | Server-side permission checks; unify role sets | **P1** | High | High |
| Org model consolidation | 3 tables, no FKs | Unify org identity; FK the commercial/billing layer | **P1** | High | High |
| Settings cache | None | Cache effective settings per (org,team,user) | **P1** | Med | High |
| integrity/active_time org filter | Cross-tenant read | Add `organization_id` filter to `load_config` | **P1** | Trivial | High |
| Feature flags (per-org) | Plan-gated only | Org feature-flag table + admin UI + eval API | **P2** | Med | High |
| API keys | Absent | API-key model, scopes, rotation, hashing | **P2** | Med | Med |
| Outbound webhooks | Inbound only | Subscription model + delivery + retries + signing | **P2** | High | Med |
| Real integrations | Stub registry | OAuth connectors + sync | **P2** | High | Med |
| Settings templates | Absent | Template/preset apply-to-scope | **P2** | Med | Med |
| Org cloning | Absent | Deep-copy org config | **P3** | Med | Low |
| SAML / SCIM | Placeholders | SAML2 SP + SCIM provisioning | **P2** | High | High (enterprise sales) |
| Frontend data layer | 4 clients | Consolidate on axios | **P2** | Low | Med |
| localStorage holidays | Per-browser | Move to tenant endpoint | **P1** | Low | High |
| Accessibility | Poor on admin | ARIA on shared primitives; consolidate Switch/Modal | **P2** | Med | Med |
| Unified audit | 4 logs | Single queryable audit surface | **P3** | Med | Med |
| Backup / restore | Absent | Tenant export/import | **P3** | High | Med |

---

## 12. Best-Practices Comparison (Step 12)

| Capability | TimeDoctor/Hubstaff/Insightful/ActivTrak/DeskTime | Rippling/BambooHR | Workfolio | **Temveris** |
|---|---|---|---|---|
| Screenshot/activity settings w/ scope | ✅ | — | ✅ | ✅ (org/team/employee) |
| Department/team hierarchy | ✅ | ✅ (deep org chart) | ✅ | ❌ team-string only, **no department** |
| Custom roles + granular permissions | ✅ | ✅ | ✅ | ⚠️ catalog exists, **not enforced** |
| MFA / SSO / SAML / SCIM | ✅ | ✅ (SCIM + SAML core) | ✅ | ⚠️ Google SSO only; **MFA/SAML/SCIM absent** |
| Per-org feature flags | ✅ | ✅ | ✅ | ❌ plan-gated only |
| API keys + webhooks | ✅ | ✅ | ✅ | ❌ inbound webhooks only |
| Integrations marketplace | ✅ (Slack/Jira/payroll) | ✅ (100s) | ✅ | ❌ stub registry |
| Billing/subscription self-serve | ✅ | ✅ | ✅ | ⚠️ engine exists but **single-tenant bug** |
| Data retention / compliance / audit | ✅ | ✅ | ✅ | ✅ retention + audit (fragmented) |
| Localization / multi-region | ✅ | ✅ | ✅ | ⚠️ i18n partial; region display-only |
| Settings templates / org cloning | ⚠️ some | ✅ | ⚠️ | ❌ |
| Privacy controls (blur, URL-off) | ✅ | — | ✅ | ✅ (`screenshot_blur`, `track_urls`) |

**Temveris competitive advantages:** the clean per-key override cascade UI with reset-to-inherit and the workspace policy-lock UX are as good as or better than most competitors; the refresh-token security design and tamper-evident work-evidence chain are ahead of the monitoring-tool pack; the commercial billing engine (dunning/trials/entitlements) is mature in design.

**Where Temveris trails:** organizational hierarchy (no departments), enforced RBAC, MFA/SAML/SCIM, per-org feature flags, API/webhooks/integrations, and true billing multi-tenancy.

---

## 13. Implementation Readiness (Step 13)

| Question | Answer |
|---|---|
| Is the architecture scalable? | ⚠️ Partially. The cascade design is sound but uncached and EAV-backed; the 3-table org split and String billing keys will fight you at scale. |
| Can new settings be added easily? | ✅ Yes for tracker/policy keys — add to `DEFAULTS` + `KEY_META` and it flows to UI + cascade. ❌ No for typed org settings (schema change + endpoint + UI each time). |
| Is inheritance reusable? | ⚠️ Only for `app_settings` keys. Org-profile/security/leave/payroll are org-scope only and can't reuse the cascade without refactor. |
| Is the database future-proof? | ❌ Not without consolidating the org model, adding `Department`+`team_id` FKs, moving blobs to object storage, and typing the security/integration bags. |
| Will 1,000 organizations work? | ⚠️ Likely, but the `"default"` billing key means billing is broken across orgs today, and the SET-NULL orphaning + no-RLS design is fragile. |
| Will 100,000 employees work? | ⚠️ Risk. Uncached settings cascade on the tracker poll path + base64 blobs in the biggest tables + audit-log-as-config scans are the three scaling cliffs. |
| Can feature flags be added? | ✅ Foundation exists (`feature_flags_json`, entitlements) — needs a per-org override table + eval API + admin UI. |
| Can settings templates be added? | ⚠️ Achievable on top of `/settings/scoped` (apply a preset map to a scope) but nothing exists today. |

---

## 14. Recommended Improvements (ordered)

**Phase 0 — Stop the bleeding (days):**
1. Rotate `SECRET_KEY` + DB creds; purge `backend/.env` from git history; confirm gitignore.
2. Fix `settings/router.py:160` `team_id` → `team_key` (or retire `/settings/update` and route org saves through `/settings/scoped`).
3. Add `organization_id` filter to `integrity`/`active_time` `load_config`.
4. Fix billing to key on the real org id; migrate the `"default"` record.
5. Move `PublicHolidaysTab` off `localStorage` to a tenant endpoint.

**Phase 1 — Make security real (weeks):**
6. Implement `UserMfaFactor` + TOTP enroll/verify + login enforcement.
7. Add middleware that enforces the stored session/IP/SSO/password policies.
8. Unify the four admin-role sets; move to server-enforced permission checks (extend `offboarding/rbac.py` app-wide).
9. Audit-log the currently-silent mutations (settings update, role change, team CRUD).

**Phase 2 — Fix the foundation (weeks–months):**
10. Consolidate org identity: enrich `Organization` (or make `OrgSettings` the canonical org record) and FK the commercial/billing layer to it.
11. Add `Department` model + `team_id`/`department_id` FKs on users; extend the cascade to 5 levels; add the department scope to the UI.
12. Cache effective settings per (org, team, user) with websocket-driven invalidation.
13. Consolidate the frontend data layer on axios; extract shared ARIA-complete `Switch`/`Modal`/`Field` primitives; code-split the 2,500+ line files; delete `_LegacySecurityTab`.

**Phase 3 — Enterprise features (months):**
14. Per-org feature flags (table + eval API + admin UI).
15. API keys + outbound webhooks + real integration connectors.
16. Settings templates; SAML2 + SCIM; unified audit; backup/restore/export.

---

## 15. Priority Matrix

| | Low complexity | Medium complexity | High complexity |
|---|---|---|---|
| **Critical impact** | .env purge; `team_id` fix; integrity/active_time org filter | Billing tenancy fix; enforce security policies | MFA implementation |
| **High impact** | localStorage→endpoint | Department tier + team FK; settings cache; org consolidation | RBAC enforcement; SAML/SCIM |
| **Medium impact** | Frontend data-layer consolidation | Feature flags; API keys; settings templates; a11y | Outbound webhooks; integrations |
| **Low impact** | delete dead code (`_LegacySecurityTab`, no-op ternary) | org cloning; unified audit | backup/restore |

---

## 16. Estimated Development Effort

| Phase | Scope | Est. effort |
|---|---|---|
| Phase 0 | Secret rotation, `team_id` fix, org-filter fixes, billing key fix, localStorage fix | **1–2 weeks** |
| Phase 1 | MFA, policy enforcement middleware, RBAC unification, audit gaps | **6–8 weeks** |
| Phase 2 | Org-model consolidation + migrations, Department/team FKs + 5-level cascade, settings cache, frontend refactor | **8–12 weeks** |
| Phase 3 | Feature flags, API keys, webhooks, integrations, SAML/SCIM, templates, unified audit, backup/restore | **12–20 weeks** |
| **Total to a credible enterprise baseline (Phases 0–2)** | | **~4–5.5 months** (1–2 engineers) |
| **Full parity with the comparison set (through Phase 3)** | | **~8–10 months** |

---

## 17. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Credential compromise via committed `.env` | **High** (repo access = key) | **Critical** (full auth bypass) | Phase 0 #1 immediately |
| Cross-tenant data leak (no RLS, billing global, config-bleed) | Medium | Critical | Phase 0 #3–#4, add RLS or centralized org-filter dependency |
| "Enterprise security" that doesn't work (MFA/policy theater) | **Certain today** | High (compliance/legal, failed audits) | Phase 1 #6–#7; until then, do not market these |
| Org-level settings save 500s via `/settings/update` | Depends on whether frontend calls it | High | Phase 0 #2 |
| Scaling cliff at 100k trackers (uncached cascade, blob tables, audit-as-config) | Medium at target scale | High | Phase 2 #12 + object storage migration |
| Migration risk consolidating 3 org tables | Medium | High (data loss) | Staged migration, dual-write, backfill jobs (patterns already in the repo) |
| Model/DB drift (raw-SQL indexes invisible to autogenerate) | Medium | Medium | Reconcile model with migrations; ban `create_all` in prod |

---

## 18. Final Enterprise Readiness Score

| Dimension | Weight | Score (0–100) | Notes |
|---|---|---|---|
| Settings architecture & inheritance | 15% | 55 | Clean cascade, but 4-level not 5, no dept, fragmented storage, `team_id` bug |
| Database design | 15% | 35 | Stub org, no dept/team FK, String billing keys, EAV bags, blob-in-row, SET-NULL orphaning |
| Backend quality | 12% | 50 | Good validation/audit/transactions; inconsistent roles, no rate limiting, duplicate surfaces |
| Security | 20% | 30 | Great token design; **committed .env**, MFA theater, unenforced policies, advisory RBAC |
| SaaS / tenant isolation | 15% | 40 | App-level scoping works mostly; **billing single-tenant**, config-bleed, no RLS |
| Performance / scalability | 10% | 45 | Uncached hot path, blob tables, audit-as-config scans |
| Frontend / UX | 8% | 55 | Best-in-class override UX; poor a11y, fragmented data layer, localStorage holidays |
| Enterprise feature completeness | 5% | 30 | No dept, feature flags, API keys, webhooks, SAML/SCIM, templates, cloning |
| **Weighted total** | | **41 / 100** | **Not enterprise-ready; strong foundations, critical gaps** |

**Bottom line:** The scoped-settings engine and auth-token design are genuinely good and worth building on. But three P0 defects (committed secret, broken billing tenancy, security theater) plus the missing Department tier and fragmented org model mean this cannot be sold as enterprise-ready today. Phases 0–1 (~2 months) close the credibility-and-safety gaps; Phase 2 (~2–3 more months) makes the foundation sound; Phase 3 reaches feature parity.

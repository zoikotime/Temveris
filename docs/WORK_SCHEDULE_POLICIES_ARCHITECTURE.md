# Work Schedule Policies — Architecture Design

**Date:** 2026-08-01
**Status:** Design proposal — awaiting approval. No code written.
**Scope:** Extend the existing `system → org → team → employee` settings cascade with working-schedule policy (working days, shifts, clock-in/out, grace, expected hours, employment type) and wire it into the attendance engine.
**Related:** [ORG_SETTINGS_AUDIT.md](ORG_SETTINGS_AUDIT.md), [ORG_SETTINGS_ROADMAP.md](ORG_SETTINGS_ROADMAP.md)

---

## 1. Current Architecture

### 1.1 The settings cascade (works, reusable as-is)

Storage is a single key/value table, `app_settings`, with two nullable scope discriminators:

| Scope | `organization_id` | `team_key` | `user_id` |
|---|---|---|---|
| Organization | set | NULL | NULL |
| Team | set | team **name** string | NULL |
| Employee | set | NULL | set |

`models.py:1080-1108`. Uniqueness is enforced by a `COALESCE(team_key,'')/COALESCE(user_id,0)` expression index created in migration `e7ebc3f1fdb5_app_settings_scope_tiers.py`, because Postgres treats NULLs as distinct.

Resolution lives in [backend/app/settings/scope.py](../backend/app/settings/scope.py):

- `resolve_effective(db, user, defaults)` — flat overlay: `defaults ← org ← team ← employee` (`scope.py:114`)
- `parent_effective(...)` — what a scope *would* inherit, for the "Inheriting: X" UI hint
- `scoped_view(...)` — per-key admin view: own override, parent value, `is_override`, plus render metadata
- `KEY_META` (`scope.py:32`) — the allow-list that drives the generic admin UI. Control types today: `bool`, `int`, `time`, `enum`.
- `GROUP_ORDER` (`scope.py:82`) — UI section ordering.

Team membership is `User.team` (a `String(100)`), **not** a FK to `teams.id`. Team scope keys on that same string.

### 1.2 API surface

[backend/app/settings/router.py](../backend/app/settings/router.py):

| Endpoint | Purpose |
|---|---|
| `GET /settings` | Effective map for the calling user (tracker + dashboards poll this) |
| `GET /settings/defaults` | Factory defaults |
| `PUT /settings/update` | Org-level write |
| `GET /settings/scoped?scope=&team=&user_id=` | Admin per-key view |
| `PUT /settings/scoped` | Upsert overrides at one tier + `AuditLog` |
| `DELETE /settings/scoped` | Reset to parent + `AuditLog` |

`DEFAULTS` / `DESCRIPTIONS` are assembled by merging contributions from feature packages (`app.active_time.config`, `app.attendance_policy`) inside `try/except` blocks — **this is the extension point for new keys.** Writes broadcast `settings_updated` over the org-scoped WebSocket.

### 1.3 Schedule primitives that already exist

| Model | Location | Notes |
|---|---|---|
| `Shift` | `models.py:1232` | `organization_id`, `name`, `shift_type`, `start_time` "09:00", `end_time` "18:00", `days_of_week` JSON `[0..6]`, `color`, `is_active` |
| `ShiftAssignment` | `models.py:1256` | `user_id` + `shift_id` + `date` "YYYY-MM-DD", optional `start_time`/`end_time` override, `task_type`, `status` |
| `EmployeeAvailability` | `models.py:1288` | per-date availability flag |
| `Holiday` | `models.py:1837` | org-scoped, `UniqueConstraint(organization_id, date)` |
| `Team` | `models.py:1813` | `UniqueConstraint(organization_id, name)` |
| `OrgSettings` | `models.py:1770` | has `default_timezone` (IANA) — **currently unused by attendance** |

`Shift` carries only the *scheduled window*. It has no expected-worked-duration, grace, or late-threshold fields.

CRUD for shifts/assignments already exists at `/schedule/*` ([app/schedule/router.py](../backend/app/schedule/router.py)): `GET|POST /schedule/shifts`, `PUT|DELETE /schedule/shifts/{id}`, `POST /schedule/assign`, `GET /schedule/my-schedule`, `/schedule/team`, `/schedule/day-insights`, `/schedule/availability`.

### 1.4 Per-employee schedule fields (the current de-facto schedule)

`User` has three columns (`models.py:80-82`):

```
default_clock_in_time      VARCHAR(5)   -- "HH:MM", nullable
default_clock_in_permanent BOOLEAN
default_clock_in_grace_min INTEGER      -- default 20
```

These are **outside the cascade** — set per user via `PATCH`-style endpoints in `app/employees/router.py:4036-4170` (including a bulk "apply to all without a shift" endpoint). There is no org or team default; `SYSTEM_DEFAULT_CLOCK_IN` / `SYSTEM_DEFAULT_CLOCK_IN_GRACE_MIN = 20` (`employees/router.py:61,66`) are the only fallback.

### 1.5 Attendance policy foundation (Phase 1 + 2, already shipped)

[backend/app/attendance_policy/resolvers.py](../backend/app/attendance_policy/resolvers.py) — pure, defensive, org-scoped:

- `resolve_expected_work_seconds(db, user, on_date)` — precedence: *(shift-level field: intentionally skipped, does not exist)* → org `expected_work_durations` JSON map keyed by employment type → org `work_hours_threshold` → 28800 s
- `is_flexible_clock_out_day(db, org_id, on_date)` — weekday in `flexible_clock_out_days` (default `["SATURDAY","SUNDAY"]`) or an org `Holiday`
- `parse_hhmm_to_seconds`, `normalize_employment_type` (alias map ft/pt/full-time/…), `default_expected_seconds_for_type`
- Registers `expected_work_durations`, `flexible_clock_out_days`, `early_clockout_enabled`, `disconnect_detection_enabled`, `disconnect_grace_minutes` into the settings router

**These resolvers read `app_settings` directly via `_get_setting_value` (`resolvers.py:198`), which filters on `organization_id` + `key` only — it does NOT walk the team/employee cascade.** So expected hours are effectively org-level today even though the cascade exists.

Phase 2 is wired: `POST /employees/clock-out` (`employees/router.py:1322-1405`) calls both resolvers and returns `EARLY_CLOCK_OUT_REASON_REQUIRED` (HTTP 200, session left open) when `worked_secs < required_secs` on a non-flexible day, then accepts a 15–1000 char typed reason on re-submit. Fail-open on any policy error.

### 1.6 Attendance status today

**Backend presence states** (`employees/router.py:3509-3561`, the `/employees/team` aggregate): `On Leave` → `Absent` → `Yet to Start` → `Stopped` → `Working` → `On Break`. Absent cutoff is 12:00 IST.

**"Late" is computed client-side.** `computeShiftInfo(emp)` in [frontend/src/components/admin/adminShared.jsx:81-150](../frontend/src/components/admin/adminShared.jsx) reads `emp.default_clock_in_time` + `emp.default_clock_in_grace_min ?? 20`, hardcodes `weekday === 0 || weekday === 6` as non-working, and derives phases `no_shift | before_shift | within_grace | late_present | absent_after_noon`. It is a display decoration, not a stored status.

**There is no "Very Late" anywhere in the codebase.**

`app/attendance_alerts.py` is a separate background monitor that re-derives the same thing server-side for *email alerts only* (`DEFAULT_GRACE_MIN = 20`, `SHIFT_NOON_HOUR = 12`), and comments say it is deliberately kept "in lock-step" with the frontend constants.

### 1.7 Downstream consumers

| Consumer | What it assumes | Location |
|---|---|---|
| Risk engine | `work_hours_threshold` (default **8:45** = 31500 s); skips `part_time`; everything else measured against 8:45 | `app/risk/router.py:33-97` |
| Early clock-out gate | `expected_work_durations` (default **8:00** = 28800 s) | `attendance_policy/resolvers.py:61` |
| Monthly report | `d.weekday() >= 5` = weekend, `EXPECTED_HOURS_PER_DAY = 8`, `LATE_THRESHOLD_HOUR/MINUTE = 9:30` | `app/reports/router.py:130-133,478` |
| Utilization report | `default_expected_seconds_for_type(u.employment_type) * days` | `app/reports/router.py:1176` |
| Payroll | Raw worked hours; LWP uses a **fixed 30-day divisor**; no expected-hours or working-days input at all | `app/payroll/service.py:95,104,144` |
| Desktop tracker | Polls `GET /settings` every 60 s, consumes flat string keys, applies live | `desktop-tracker/main.js:2328-2427,2461` |
| Break governance | `schedule_version_id(user)` = `default_clock_in_time + grace` as provenance | `app/breaks/governance.py:261` |

### 1.8 Defects and landmines found during this audit

| # | Finding | Location | Impact |
|---|---|---|---|
| **D1** | `PUT /settings/update` references `models.AppSettings.team_id` — **the column does not exist** (`AppSettings` has `team_key`). Verified: raises `AttributeError`. | `settings/router.py:150` | Org-level save via this endpoint is broken. Called from `SettingsPage.jsx:1266,2725`. Already logged as a P0 in ORG_SETTINGS_ROADMAP. |
| **D2** | `_cached_setting_value` queries `app_settings` filtered on **`key` only** — no `organization_id`, no scope — and caches in a **process-global dict keyed on `key` alone**, 10 s TTL. | `breaks/governance.py:165-191` | Cross-tenant settings leak. Must not be reused as a pattern for schedule keys. |
| **D3** | `_is_non_working_day` queries `Holiday` with **no `organization_id` filter**. | `attendance_alerts.py:118` | One tenant's holiday suppresses late/absent alerts for **every** tenant. |
| **D4** | Weekend hardcoded `Sat/Sun` in 3 independent places. | `attendance_alerts.py:115`, `reports/router.py:478`, `adminShared.jsx:93` | Org B (Mon–Sat) and Org C (24×7) get wrong working-days, absent-days, and alerting. |
| **D5** | IST (`timedelta(hours=5, minutes=30)`) hardcoded **37 times across 11 files** (18 in `employees/router.py` alone). `OrgSettings.default_timezone` exists but is never read by attendance. | see §8 | No tenant outside IST can have correct day boundaries or shift thresholds. |
| **D6** | Two divergent "expected hours" defaults: risk engine **8:45**, early-clock-out **8:00**. | `risk/router.py:33` vs `resolvers.py:61` | An employee can be allowed to clock out at 8:00 and *then* be flagged by the risk engine. Pre-existing; the new resolver must not add a third. |
| **D7** | `clock_out` caps `effective_start = max(clock_in, today_start_IST)` (`employees/router.py:1423`). | `employees/router.py:1409-1426` | A 22:00–06:00 night shift is truncated to the post-midnight portion. Blocks Org C. |

---

## 1.9 Multi-Tenancy Verification (2026-08-02)

Second pass, specifically validating tenant isolation before implementation. Findings below were **reproduced at runtime** against an in-memory SQLite build of `models.py`, not inferred.

### D8 — Tier-blind reads: an employee override leaks org-wide *(live, in the attendance path)*

30 `app_settings` reads filter `organization_id` + `key` but **not** `team_key` / `user_id`, then take `.first()`. Row order is undefined, so a team- or employee-scope override row can be returned as the organization's value.

Reproduced — Org 1, Priya (id 10) has an employee-scope `expected_work_durations = {"full_time":"4:00"}`; Raj (id 11) has no override and the org has no org-level row:

```
resolve_expected_work_seconds(db, Raj, 2026-08-03)  -> 14400s (4.0h)   expected 28800s (8.0h)
_threshold_secs(db, org_id=1)                        -> 14400s (4.0h)   expected 31500s (8.75h)
```

**One employee's override silently becomes the whole organization's expected hours and risk threshold.** `resolvers.py:198` is the reader and it is what the live Early Clock Out gate calls. Affected in the attendance path: `attendance_policy/resolvers.py:206`, `risk/router.py:37,51`, `employees/router.py:1858,2257`, `breaks/router.py:62,83`, `activity_log/router.py:251`. Also 22 sites in `leave/` and `workspace/`.

This is the single most important finding: it sits directly underneath the mechanism this design is built on.

### D9 — `AppSettings.team_id` is 4 sites, not 1, and 3 fail silently

The column does not exist. Every reference is inside a `try/except` that swallows the `AttributeError` and returns a hardcoded default:

| Site | Consequence |
|---|---|
| `settings/router.py:150` | `PUT /settings/update` raises (D1) |
| `active_time/config.py:221` | **Every org's Active Time tuning is silently ignored — always `DEFAULT_CONFIG`** |
| `tracking/router.py:618` | `screenshot_interval` always 30 |
| `tracking/router.py:651` | `active_poll_period_seconds` always 3 |

Reproduced — org-level `active_time.weight_keyboard = 0.99` in the DB:
```
load_config(db).w_keyboard -> 0.28   (the hardcoded default)
```

So the Active Time engine's 20 documented per-org tunables are **currently non-functional for every tenant**. This is invisible because the failure mode is a silent fallback, not an error.

### D10 — Provisioning writes to a shared NULL-org bucket, and seeds no attendance settings

One org-creation path exists: `super_admin/router.py:1184`. It seeds `OrgSettings`, `LeavePolicy`, `Team` — and two `AppSettings` rows **with `organization_id` omitted, i.e. NULL** (`router.py:1203-1212`), using an org-id-in-the-key convention (`shared_pool_enabled:{org.id}`).

- **No schedule or attendance settings are seeded at all.** New orgs fall through to hardcoded Python constants.
- `commercial/provisioning.py:307` seeds the same keys but *does* set `organization_id` — the two provisioning paths disagree.
- Reproduced: a NULL-org row is read by any org-less user, and a NULL-org `Holiday` is treated as a flexible day for `org_id=None`. A real org is correctly isolated from the NULL bucket.

### D11 — `ON DELETE SET NULL` orphans tenant data into that bucket

`app_settings`, `holidays`, `shifts`, `shift_assignments`, `teams` all declare `organization_id ... ondelete="SET NULL"`. Deleting an organization therefore **moves its rows into the shared NULL bucket instead of removing them** — settings, holidays and shifts outlive their tenant and become readable from org-less contexts. Should be `CASCADE`.

### D12 — Leave uses a second, parallel tenancy convention

`leave/router.py` keys settings as `f"leave_policies:{org_id}"` and queries **by key only**, with no `organization_id` filter (`:308,343,504,751,978`). Functionally isolated (the key differs per org) but it bypasses the cascade entirely: no team/employee override is possible, and the keys are absent from `DEFAULTS` so `/settings/scoped` rejects them. Relevant because "Leave integration" is a target feature.

### D13 — `POST /schedule/assign` can persist a cross-org `shift_id`

`schedule/router.py:214` guards the shift lookup with `if data.shift_id and (not start_t or not end_t)`. When the caller supplies **both** `start_time` and `end_time`, the org-ownership check is skipped and a foreign `shift_id` is written to `shift_assignments`. Narrow today; **blocking for this design**, which makes `shift_id` precedence level 1.

### D6 restated — expected hours has 5 sites / 2 values, and "late" has 3 definitions

| Expected hours | Value |
|---|---|
| `risk/router.py:33` | 31500 (8:45) |
| `attendance_policy/resolvers.py:61` | 28800 (8:00) |
| `reports/router.py:132` | 8 h |
| `employees/router.py:3823` | 8 h |
| `intelligence/risk_engine.py:23` | 8.0 h |

| "Late" definition | Basis |
|---|---|
| `adminShared.jsx:81` | per-user `default_clock_in_time` + grace |
| `attendance_alerts.py:242` | per-user `default_clock_in_time` + grace |
| `reports/router.py:214,695,1227` | **global 09:30 UTC** — ignores every per-user setting |

Weekend is hardcoded in **5** places (add `intelligence/burnout.py:154`, `reports/router.py:1787` to D4). Default clock-in is `"10:30"` at `employees/router.py:65` but `09:00` in the settings UI — another divergence.

### Verdict on isolation

**The cascade mechanism itself is sound and correctly isolated** — every `_rows_for_scope` call filters `organization_id`, and a real org provably cannot read another org's or the NULL bucket's rows. The design's reuse of `settings_scope` is the right call.

**The consumers around it are not isolated.** D8 and D9 must be fixed before any schedule key is read by anything, because the new resolver inherits both defects the moment it reuses those readers.

---

## 2. What Already Exists (reuse inventory)

**Reuse unchanged:**
- The whole `app_settings` 4-tier cascade + `scope.py` resolution algorithm
- `GET|PUT|DELETE /settings/scoped` endpoints and their `AuditLog` writes
- `OrganizationSettingsTab.jsx` — fully generic, metadata-driven; new keys appear automatically
- The `try/except` DEFAULTS-merge extension point in `settings/router.py:70-87`
- `Shift`, `ShiftAssignment`, `EmployeeAvailability`, `Holiday`, `Team` tables and `/schedule/*` CRUD
- `attendance_policy` parsing/normalization helpers (`parse_hhmm_to_seconds`, `normalize_employment_type`)
- `GET /settings` polling in the desktop tracker (60 s) — flat keys reach it for free
- `settings_updated` WebSocket broadcast
- `User.employment_type` as the employee-level employment type (already read by risk + reports)

**Reuse with extension:**
- `KEY_META` — needs three new control types (`multiselect`, `shift_ref`, `time`-range) and a `Working Schedule` group
- `resolve_expected_work_seconds` — must be upgraded to walk the full cascade instead of org-only

**Do not reuse:**
- `breaks/governance.py:_cached_setting_value` (D2 — org-blind)
- `adminShared.jsx:computeShiftInfo` client-side derivation (replace with server-provided values)

---

## 3. Design Principles

1. **One resolver, one truth.** Every consumer asks the same function for the same date. No consumer re-derives shift start, grace, or expected hours.
2. **Additive status.** `arrival_status` is a **new, orthogonal field** alongside the existing presence `status`. Existing statuses keep their exact meaning and values.
3. **Extensibility via JSON, not migrations.** Scalar policy → cascade keys. Per-shift open-ended rules → a `config` JSONB on `shifts`. Future features add JSON keys, not columns.
4. **Default = today's behavior, byte-for-byte.** Every default reproduces current output. A tenant that touches nothing sees no change.
5. **Fail-open.** Malformed policy never blocks a clock-in/out. Matches the existing `attendance_policy` contract.
6. **Tenant isolation by construction.** Every read filters `organization_id`. No process-global caches keyed without org.

---

## 4. Database Changes

### 4.1 No new tables for the core

The three storage layers already exist. Assignment of responsibility:

| Concern | Storage | Why |
|---|---|---|
| Scalar policy (grace, expected hours, working days, thresholds, flags) | `app_settings` cascade | Inheritance already solved; already reaches tracker via `GET /settings` |
| Shift *definitions* (name, window, days, per-shift rules) | `shifts` table | Already org-scoped with `days_of_week` |
| Shift *assignment* — default | `app_settings` key `schedule.shift_id` at org/team/employee scope | **Assignment inherits through the existing cascade for free** |
| Shift *assignment* — date-specific / rotating roster | `shift_assignments` rows | Already exists, already keyed `(user_id, date)` |
| Holiday calendar | `holidays` table | Already org-scoped |

Employee "Assign Shift / Override Shift" is therefore just an employee-scope `app_settings` row — the same mechanism, the same UI, the same audit trail.

### 4.2 Migration A — extend `shifts` (all nullable, additive)

```
ALTER TABLE shifts ADD COLUMN
  expected_work_seconds  INTEGER      NULL,  -- required WORKED time excl. breaks
  grace_minutes          INTEGER      NULL,
  late_after_minutes     INTEGER      NULL,
  very_late_after_minutes INTEGER     NULL,
  min_hours_required_seconds INTEGER  NULL,
  allow_early_clock_in   BOOLEAN      NULL,
  allow_late_clock_out   BOOLEAN      NULL,
  crosses_midnight       BOOLEAN      NOT NULL DEFAULT FALSE,
  timezone               VARCHAR(64)  NULL,  -- IANA; NULL = inherit org
  is_default             BOOLEAN      NOT NULL DEFAULT FALSE,
  config                 JSONB        NULL   -- open-ended future rules
;
CREATE INDEX ix_shifts_org_default ON shifts (organization_id, is_default);
```

`expected_work_seconds` is what §1.5's precedence level 1 was reserved for — the comment at `resolvers.py:25-29` explicitly documents this slot. Note it is **required worked time excluding breaks**, deliberately *not* `end_time - start_time` (the window includes lunch).

`config` JSONB absorbs everything in the "future features" list without another migration:

```jsonc
{
  "breaks":   [{ "name": "Lunch", "start": "13:00", "end": "14:00", "paid": false }],
  "overtime": { "enabled": true, "after_seconds": 32400, "multiplier": 1.5,
                "weekly_cap_seconds": 180000 },
  "night":    { "differential_pct": 10, "window": ["22:00", "06:00"] },
  "work_mode": { "default": "onsite",
                 "by_weekday": { "TUE": "remote", "THU": "remote" } },   // hybrid
  "flex":     { "mode": "flexible", "core_hours": ["11:00", "16:00"] },
  "rotation": { "cycle_days": 21, "sequence": ["morning", "evening", "night"] }
}
```

### 4.3 Migration B — `shift_assignments` (additive)

```
ALTER TABLE shift_assignments ADD COLUMN
  expected_work_seconds INTEGER NULL,   -- per-date override
  is_off_day            BOOLEAN NOT NULL DEFAULT FALSE;  -- explicit weekly-off / roster off
```

`is_off_day` makes rotating rosters expressible without inventing a status string, and gives "Weekly off days" a per-date escape hatch on top of the weekly `schedule.working_days` pattern.

### 4.4 Migration C — future-proofing holidays (additive, defer usage)

```
ALTER TABLE holidays ADD COLUMN calendar_key VARCHAR(100) NULL;
-- Relax: uq_holidays_org_date -> UNIQUE (organization_id, COALESCE(calendar_key,''), date)
```

Nothing reads `calendar_key` in v1. It exists so "multiple holiday calendars" (per country/team) later needs no redesign. **Also fix D3** (missing org filter) in the same phase.

### 4.5 Not changed

- `app_settings` — no schema change at all; new keys are rows
- `User.default_clock_in_time` / `_permanent` / `_grace_min` — **retained and still read** as an employee-scope fallback (§6.2). Deprecated, not dropped.
- `payroll_*` — no change; payroll consumes the resolver later (§10)

---

## 5. Setting Keys

> **Superseded by P4 (implemented 2026-08-02).** The authoritative key list is now
> [`backend/app/schedule_policy/keys.py`](../backend/app/schedule_policy/keys.py).
> Two deliberate deviations from the table below, both approved in the P4 brief:
>
> 1. **`schedule.grace_minutes` default is `15`, not `20`.** The 20 in the table
>    matched `SYSTEM_DEFAULT_CLOCK_IN_GRACE_MIN`, i.e. today's behaviour. 15 is a
>    product decision, so the resolver's default is **not** behaviour-neutral for an
>    org that never sets a grace value. P6's backfill must therefore write an
>    explicit `schedule.grace_minutes = 20` per org to stay neutral on rollout, and
>    the change to 15 becomes a separate, deliberate policy change.
> 2. **`schedule.clock_in_time` / `clock_out_time` are named
>    `schedule.shift_start_time` / `schedule.shift_end_time`.** Matches the approved
>    UI labels and avoids being confused with the legacy `User.default_clock_in_time`
>    column, which the resolver reads at precedence 3.
> 3. **`schedule.shift_start_time` defaults to `""` (no scheduled shift), not
>    `09:00`** — changed during P6 after the dry run showed 09:00 was the *only*
>    thing breaking equivalence. With it, 62 of 69 users gained a scheduled start
>    and became late-classifiable; today those users have no
>    `default_clock_in_time`, so `attendance_alerts` skips them and they can never
>    be Late. Empty reproduces that exactly. An org opts in by configuring a start
>    time, which employees without their own override then inherit normally.
>    Result: **345/345 user-days identical, zero differences**, before and after
>    the backfill.
>
> Also: `schedule.timezone` was **dropped** — timezone is already editable under
> Company & Workspace Profile (`OrgSettings.default_timezone`), and duplicating it
> would violate the no-duplicate-settings rule. The resolver reads it from there.

All namespaced `schedule.*` so they group cleanly and can never collide with existing keys.

| Key | Type | Default | Notes |
|---|---|---|---|
| `schedule.engine_enabled` | bool | **`false`** | Master kill switch. `false` → resolver returns legacy-equivalent values (§11) |
| `schedule.timezone` | enum(IANA) | `Asia/Kolkata` | Seeded from `OrgSettings.default_timezone` when present |
| `schedule.working_days` | multiselect | `["MON","TUE","WED","THU","FRI"]` | Weekly off = the complement |
| `schedule.shift_id` | shift_ref | `null` | Assigned shift. `null` → use the flat keys below |
| `schedule.clock_in_time` | time | `09:00` | Used when no shift is assigned |
| `schedule.clock_out_time` | time | `18:00` | |
| `schedule.expected_work_hours` | enum HH:MM | `8:45` | Matches the risk engine's current default (D6) |
| `schedule.grace_minutes` | int 0–240 | `20` | `20` matches today's `SYSTEM_DEFAULT_CLOCK_IN_GRACE_MIN` |
| `schedule.late_after_minutes` | int 0–480 | `null` → `grace_minutes` | "Late after" |
| `schedule.very_late_after_minutes` | int 0–720 | `120` | New threshold. The last arrival step — there is no half-day threshold |
| `schedule.min_hours_required` | enum HH:MM | `4:00` | |
| `schedule.allow_early_clock_in` | bool | `true` | `false` → clock-in before `start − early_window` refused |
| `schedule.allow_late_clock_out` | bool | `true` | |
| `schedule.absent_after_hour` | int 0–23 | `12` | Replaces hardcoded `SHIFT_NOON_HOUR` |
| `schedule.employment_type` | enum | `full_time` | **Org/team default for new hires only** — see §6.3 |
| `schedule.work_mode` | enum | `onsite` | `onsite \| remote \| hybrid` — stored in v1, consumed later |

Existing `expected_work_durations` (employment-type → HH:MM map) is kept and extended with `contract` and `intern` entries.

### 5.1 KEY_META additions

New `"Working Schedule"` group, placed first in `GROUP_ORDER`. Three new control types the generic UI must learn:

- `multiselect` — `options` + `option_labels`; value serialized as a JSON array string
- `shift_ref` — options fetched live from `GET /schedule/shifts`; value is a stringified shift id, `""` = none
- `time` — `<input type="time">`; already declared in the `KEY_META` docstring but never implemented

---

## 6. Backend Service Changes

### 6.1 New package `app/schedule_policy/`

```
app/schedule_policy/
  __init__.py      # public exports + SETTING_DEFAULTS / SETTING_DESCRIPTIONS
  keys.py          # key constants + KEY_META contribution
  resolver.py      # resolve_work_schedule()
  classify.py      # classify_arrival() / classify_departure()  — pure, no DB
```

Registered in `settings/router.py` through the same `try/except` merge block as `active_time` and `attendance_policy`.

### 6.2 The single resolver

```python
@dataclass(frozen=True)
class EffectiveSchedule:
    on_date: date
    timezone: str                    # IANA
    is_working_day: bool
    is_holiday: bool
    shift_id: int | None
    shift_name: str | None
    start_local: time | None         # wall clock in `timezone`
    end_local: time | None
    crosses_midnight: bool
    expected_work_seconds: int       # required WORKED time, breaks excluded
    min_required_seconds: int
    grace_minutes: int
    late_after_minutes: int
    very_late_after_minutes: int
    absent_after_hour: int
    allow_early_clock_in: bool
    allow_late_clock_out: bool
    employment_type: str
    work_mode: str
    config: dict                     # shift.config, merged
    sources: dict[str, str]          # field -> "employee"|"team"|"org"|"shift"|
                                     #          "assignment"|"legacy_user"|"system"

def resolve_work_schedule(db, user, on_date) -> EffectiveSchedule: ...
```

**Precedence, highest first:**

1. `ShiftAssignment` for `(user_id, on_date)` — rotating roster / one-off. Its `is_off_day`, `start_time`/`end_time`, `expected_work_seconds` win.
2. Employee-scope `app_settings` (`schedule.*` at `user_id`)
3. **Legacy** `User.default_clock_in_time` / `default_clock_in_grace_min` — applied *only* for the fields they cover and *only* when no employee-scope key exists. This is what makes rollout behavior-neutral.
4. Team-scope `app_settings`
5. Org-scope `app_settings`
6. Assigned `Shift` row's own columns (resolved from the winning `schedule.shift_id`) fill any field still unset
7. System defaults (§5)

Field-level, not document-level: an employee may override only `grace_minutes` and inherit everything else. Every resolved field records its origin in `sources` so the UI and audit can explain *why*.

`is_working_day` = `on_date.weekday()` ∈ `schedule.working_days` (or the assigned shift's `days_of_week`) **AND** not an org `Holiday` **AND** not an `is_off_day` assignment.

**Implementation notes**
- Reads go through `settings_scope.resolve_effective` on a `schedule.*`-only defaults dict — reusing the audited cascade rather than a new query path. Explicitly **not** the `breaks/governance.py` helper (D2).
- One batched variant `resolve_work_schedules(db, users, on_date)` is required for `/employees/team` and reports, which iterate hundreds of users. Without it this is an N+1 across three tables.
- Never raises. Any exception → the system-default schedule, logged once.

### 6.3 Employment type — deliberately one source of truth

`User.employment_type` stays authoritative for an individual (it is already read by `risk/router.py:89` and `reports/router.py:1176`). `schedule.employment_type` is an **org/team default applied at employee-creation time only**, never a competing read path. Resolution: `User.employment_type` if non-empty → cascade default → `full_time`.

This is the one place I recommend *against* full cascade override semantics — two live sources for a field that gates risk alerting is how D6 happened.

`contract` and `intern` are added to `_EMPLOYMENT_TYPE_ALIASES` and to `DEFAULT_EXPECTED_WORK_DURATIONS`.

### 6.4 `resolve_expected_work_seconds` upgrade

Keep the signature and the fail-open contract. Change the body to delegate:

```python
def resolve_expected_work_seconds(db, user, on_date) -> int:
    return resolve_work_schedule(db, user, on_date).expected_work_seconds
```

This is what finally fills precedence level 1 (shift-level required duration) documented at `resolvers.py:25-29`, and it upgrades the early-clock-out gate from org-only to full cascade **with no change at the call site** (`employees/router.py:1342`).

`is_flexible_clock_out_day` likewise becomes `not resolve_work_schedule(...).is_working_day`, keeping `flexible_clock_out_days` readable as a legacy alias for the inverse of `schedule.working_days`.

### 6.5 Timezone helper

New `app/_tz.py`:

```python
def org_timezone(db, org_id) -> ZoneInfo   # schedule.timezone -> OrgSettings.default_timezone -> Asia/Kolkata
def local_now(db, org_id) -> datetime
def local_day_bounds(db, org_id, on_date) -> tuple[datetime, datetime]  # UTC
```

**Scoped deliberately:** only the new resolver and the new classifier use it in v1. The 37 existing IST literals (D5) are *not* touched in this phase — that is a separate, riskier migration (§8, §12). Because the default is `Asia/Kolkata`, new and old code agree for every current tenant.

---

## 7. Attendance Engine Changes

### 7.1 New pure classifier

```python
def classify_arrival(clock_in_local, sched, now_local) -> ArrivalStatus
```

`ArrivalStatus ∈ {not_applicable, awaiting, present, late, very_late, absent}` plus `late_minutes`.

Boundaries, all measured from `start_local` (inclusive lower, exclusive upper):

| Condition | Result |
|---|---|
| `not sched.is_working_day` | `not_applicable` |
| clock-in ≤ start + `grace_minutes` | **`present`** |
| clock-in ≤ start + `late_after_minutes` … | `present` if `late_after > grace`, else `late` |
| clock-in ≤ start + `very_late_after_minutes` | **`late`** |
| clock-in beyond that | **`very_late`** |
| no clock-in, now < start | `awaiting` |
| no clock-in, now ≥ `absent_after_hour` | `absent` |

Worked example against the request (`start 09:00`, `grace 15`, `late_after 15`, `very_late_after 120`):

| Clock-in | Result |
|---|---|
| 09:08 | `present` (within 15 min grace) |
| 09:20 | `late` (past grace, within 120) |
| 11:00 | `very_late` (past 120 → 11:00 ≥ 09:00+120) |

### 7.2 Departure

```python
def classify_departure(worked_seconds, sched) -> DepartureStatus
```
→ `{complete, early, overtime}` + `shortfall_seconds` / `overtime_seconds`, comparing `worked_seconds` (breaks and offline already excluded — reusing `risk/router.py:_session_worked_secs`, **not** a new calculation) against `sched.expected_work_seconds`.

### 7.3 Integration — additive only

| Touch point | Change |
|---|---|
| `POST /employees/clock-out` gate (`router.py:1322-1405`) | **No call-site change.** `resolve_expected_work_seconds` now cascade-aware, so Early Clock Out automatically compares against the employee's own expected hours. |
| `GET /employees/team` (`router.py:3509-3561`) | Existing `status` field **untouched**. Add `arrival_status`, `late_minutes`, `expected_seconds`, `shift_name`, `schedule_source` to each row. |
| `attendance_alerts.py` | Replace `_parse_shift_start` + `DEFAULT_GRACE_MIN` + `SHIFT_NOON_HOUR` + `_is_non_working_day` with resolver calls. **Fixes D3 and D4 in the process.** Alert trigger condition stays "past start + grace, before absent cutoff". |
| `POST /employees/clock-in` | New **soft** guard: when `allow_early_clock_in = false` and now < `start − early_window`, refuse with a structured 200 body (never a 4xx — matches the Early-Clock-Out pattern so no existing client breaks). Default `true` = no change. |
| `risk/router.py:_run_risk_check` | Swap `_threshold_secs` for `resolve_work_schedule(...).expected_work_seconds`. **Behavior change — gated on `schedule.engine_enabled`.** Also replaces the `part_time`-only skip with "skip when `expected_work_seconds == 0`", so contract/intern stop being measured against 8:45 (D6). |
| `reports/router.py` | Replace `d.weekday() >= 5` (line 478) and `EXPECTED_HOURS_PER_DAY` (line 132) with the batched resolver. Fixes Org B/C working-day counts (D4). |

### 7.4 Night shifts / work-day attribution (Org C)

A 22:00–06:00 shift belongs to a **logical work date**, not a calendar date. Two things must change or Org C is simply wrong:

1. `clock_out`'s `effective_start = max(_clock_in, _today_start_utc)` (`employees/router.py:1423`) truncates the pre-midnight portion → an 8 h night shift records ~6 h. Must become `max(_clock_in, shift_window_start_utc)` from the resolver.
2. Every `_day_start = midnight IST` bucket (`router.py:1349`, `1472`; reports; `active_time/service.py`) must bucket on the logical work date.

Recommendation: get the **schema and resolver** right now (`crosses_midnight` + `local_day_bounds` are the hooks), but land night-shift *reporting* correctness as its own phase (§13 step 7). Do not ship 24×7 to a customer before it.

---

## 8. Desktop Tracker Changes

Minimal — the tracker already polls `GET /settings` every 60 s and reads flat string keys (`main.js:2328-2427`).

1. `fetchAndApplySettings()` gains a `schedule` block: cache `schedule.clock_in_time`, `clock_out_time`, `grace_minutes`, `expected_work_hours`, `allow_late_clock_out`, `working_days`. Same `flag()`/`parseInt` defensive style, same "absent key leaves current value untouched" rule.
2. Tray tooltip / dashboard countdown shows "Shift 09:00–18:00 · 2 h 15 m remaining" from those values.
3. When `allow_late_clock_out = false`, prompt at `end_local` instead of silently continuing.
4. The auto-clock-out / suspend / shutdown paths must not fire a clock-out that crosses `crosses_midnight` boundaries — gate on the shift window rather than local midnight.
5. **No new endpoint.** No tracker release is required for the v1 backend rollout — the tracker keeps working unchanged; these are enhancements.

`EarlyClockOutModal.jsx` already renders `required_seconds` / `worked_seconds` / `break_seconds` / `offline_seconds` from the clock-out response, so it inherits per-employee expected hours with **zero change**.

---

## 9. Frontend Changes

### 9.1 Organization Settings → Working Schedule (largely free)

`OrganizationSettingsTab.jsx` is fully metadata-driven, so a `"Working Schedule"` group appears automatically once `KEY_META` declares the keys. Required additions to that file:

- `control()` (line 144) — cases for `multiselect` (day chips), `shift_ref` (dropdown from `GET /schedule/shifts`), `time` (`<input type="time">`)
- `fmt()` (line 302) — render arrays and shift names in the "Inheriting: …" hint
- Fetch the shift list once alongside the existing employees/teams fetch (line 62)

Screen order per the request: Working Days → Shift → Start/End → Expected Daily Hours → Grace → Late After → Half Day After → Allow Early Clock In → Allow Late Clock Out → Minimum Hours → Employment Type.

### 9.2 Shift management

New `WorkingScheduleTab.jsx` (a sibling nav entry at `SettingsPage.jsx:183`) for shift **definitions** — the CRUD `app_settings` can't express. Backed by the existing `/schedule/shifts` endpoints plus the new columns.

### 9.3 Retire client-side late derivation

`adminShared.jsx:computeShiftInfo` must stop computing and start **reading** `arrival_status` / `late_minutes` from the API row. Keep the function as a thin formatter (`_formatLateLabel` is fine) so `AdminDashboard.jsx` call sites don't churn. This removes the hardcoded weekend (D4) and the `?? 20` default that silently disagreed with any org-level grace.

Add a `Very Late` pill (existing pills: Late / Absent / Working / …).

### 9.4 Employee scope

Already works via the generic UI — `Assign Shift`, `Override Expected Hours`, `Override Grace` are just employee-scope keys with a `Reset` button and an "Inheriting: X" hint. `MyScheduleTab.jsx` gains a read-only "Your schedule" header showing the resolved values and where each came from (`sources`).

---

## 10. Migration Strategy

**Phase 0 — prerequisites (no schema)**
- Fix **D1** (`team_id` → `team_key`, `settings/router.py:150`). Blocking: org-level saves are broken.
- Fix **D2** (org filter + org-keyed cache in `breaks/governance.py`) and **D3** (org filter on the Holiday query).

**Phase 1 — schema**
- One Alembic revision, `down_revision` = current head at implementation time (`d5e8a1c34b70` on this branch, but that file is uncommitted — re-derive with `alembic heads` before writing).
- All additions nullable or `DEFAULT`-backed → no table rewrite, no lock of consequence.
- `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` style, matching `z9f8e7d6c5b4`. Real `down_revision`, working `downgrade()` — the `domain_categories` outage came from a half-applied migration, so verify `alembic upgrade head` **and** `downgrade -1` on a clone first.

**Phase 2 — backfill (idempotent script, one org at a time)**

Per org, write **only** values that reproduce current behavior:

| Key | Backfilled from |
|---|---|
| `schedule.timezone` | `OrgSettings.default_timezone` → `Asia/Kolkata` |
| `schedule.working_days` | inverse of `flexible_clock_out_days` → Mon–Fri |
| `schedule.expected_work_hours` | existing `work_hours_threshold` row → `8:45` |
| `schedule.grace_minutes` | modal `User.default_clock_in_grace_min` → `20` |
| `schedule.clock_in_time` | modal non-null `User.default_clock_in_time` → `09:00` |
| `schedule.absent_after_hour` | `12` |
| `schedule.engine_enabled` | **`false`** |

Create one `Shift` per org (`is_default = true`) from those values so the shift path is exercised, but leave `schedule.shift_id` unset — the flat keys drive v1.

Employees whose `default_clock_in_time` differs from the org modal value get an **employee-scope** `schedule.clock_in_time` row, preserving their individual schedule exactly. (The legacy columns are also still read at precedence 3, so this is belt-and-braces.)

**Phase 3 — deprecate, don't drop.** `User.default_clock_in_*` columns stay for ≥2 releases, still written by the existing endpoints, still read at precedence 3. Drop only after telemetry shows no reads.

---

## 11. Rollout Plan

Gated on `schedule.engine_enabled` (default `false`). When `false`, `resolve_work_schedule` returns exactly the legacy-equivalent values, and `risk/router.py` / `reports` keep their current constants. Flipping one org's key is the entire rollout unit — and the entire rollback.

| Stage | Scope | Gate to advance |
|---|---|---|
| R0 | Migration + backfill everywhere, engine **off** | `GET /settings` unchanged for every org; no diff in `/employees/team` |
| R1 | Internal Temveris org, engine **on** | Shadow-compare `arrival_status` vs `computeShiftInfo` for 3 days — zero unexplained diffs |
| R2 | 1–2 design-partner orgs (one Mon–Fri, one Mon–Sat) | Working-day counts and absent-days match manual expectation for a full week |
| R3 | Working Schedule UI visible to all admins; engine still per-org | — |
| R4 | Engine on by default for **new** orgs | — |
| R5 | Backfilled orgs migrated in batches | Per-batch report diff review |
| R6 | Night-shift / 24×7 correctness (§7.4) → Org C profile | Separate phase; do not sell 24×7 before it |

**Shadow mode is the key safety mechanism.** In R1–R2, compute `arrival_status` and log it *without* surfacing it, and emit a metric when it disagrees with the legacy client-side derivation. That converts "did we break attendance?" from a support question into a dashboard.

---

## 12. Risks

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| R-1 | **Night shifts break day bucketing.** Every `midnight IST` bucket (`employees/router.py:1349,1423,1472`; reports; `active_time`) mis-attributes a 22:00–06:00 session; D7 truncates it outright. | **High** | Ship schema + resolver now; land work-day attribution as its own phase (§7.4). Do not enable 24×7 for a customer until then. |
| R-2 | **Expected-hours divergence (D6) triples.** 8:45 (risk) vs 8:00 (early clock-out) vs a new resolver value → an employee is allowed to leave and *then* alerted. | **High** | Resolver is the single source; default `8:45` matches the risk engine; migrate the early-clock-out path in the same release, not a later one. |
| R-3 | **Two "late" derivations disagree** while the frontend still computes locally (hardcoded weekend, `?? 20` grace). | **High** | §9.3 is not optional — retire `computeShiftInfo`'s derivation in the same release that exposes `arrival_status`. Shadow mode (R1) catches drift first. |
| R-4 | **Cross-tenant settings leak via D2's global cache.** Any new schedule read copying that pattern leaks across tenants. | **High** | Fix D2 in Phase 0. Resolver reads only via `settings_scope`. Add a test asserting Org A's schedule is invisible to Org B. |
| R-5 | **N+1 collapse.** `/employees/team` and monthly reports iterate hundreds of users × 3 tables. | Medium | Batched `resolve_work_schedules` is a Phase-1 requirement, not an optimization. `/employees/team` already has an 8 s cache; reuse it. |
| R-6 | **Timezone half-migration.** 37 IST literals remain while the resolver is tz-aware — a non-IST org gets internally inconsistent answers. | Medium | Default `Asia/Kolkata` keeps them in agreement today. **Do not sell a non-IST tenant until the IST sweep lands.** Track as an explicit follow-up. |
| R-7 | **Team scope keyed on a name string.** Renaming a team silently orphans its overrides (pre-existing cascade weakness). | Medium | Out of scope to fix; add a rename guard that migrates `team_key` rows, and note it in ORG_SETTINGS_ROADMAP Phase 3. |
| R-8 | **Half-applied migration.** Exactly the `domain_categories` failure mode from 2026-07. | Medium | Single revision, `IF NOT EXISTS`, verified `upgrade`+`downgrade` on a clone, applied in a window. |
| R-9 | **Backfill misreads a heterogeneous org** (mixed per-user clock-in times) and homogenizes schedules. | Medium | Backfill writes employee-scope rows for every deviation; legacy columns still read at precedence 3; dry-run diff report before writing. |
| R-10 | Break windows in `shift.config` vs `break.*` governance settings could double-count lunch. | Low | v1 stores break windows but **does not** consume them; `break_governance` stays authoritative. |
| R-11 | UI complexity — 17 new keys in one panel. | Low | Collapsed-by-default group, existing search box, progressive disclosure (advanced thresholds behind a toggle). |

---

## 13. Recommended Implementation Order

> **Revised 2026-08-02.** Phase 0 grows from 3 fixes to 7 after the multi-tenancy verification (§1.9). Steps 1+ are unchanged, but **step 0 is now strictly blocking** — D8 and D9 sit underneath the mechanism this design reuses.

| Step | Work | Depends on | Est. |
|---|---|---|---|
| **0a** | **D8** — add tier filters (`team_key IS NULL AND user_id IS NULL`) to all 30 tier-blind reads, or route them through `settings_scope`. Start with the attendance path: `attendance_policy/resolvers.py:206`, `risk/router.py:37,51`, `employees/router.py:1858,2257`, `breaks/router.py:62,83`. Regression test: an employee override must not change the org value. | — | 2 d |
| **0b** | **D9** — `team_id` → `team_key` at all 4 sites. Then **re-verify** Active Time tuning and `screenshot_interval` actually take effect; they have been silently dead. Remove the blanket `except Exception` that hid this, or log at `error`. | — | 1 d |
| **0c** | **D2** — org filter + org-keyed cache in `breaks/governance.py:165`. **D3** — org filter on `attendance_alerts.py:118`. | — | 1 d |
| **0d** | **D13** — validate shift org ownership unconditionally in `POST /schedule/assign`; audit `PUT /assign/{aid}` for the same gap. | — | 0.5 d |
| **0e** | **D11** — `ON DELETE SET NULL` → `CASCADE` on `app_settings`, `shifts`, `shift_assignments` (+ decide `holidays`, `teams`). Migration; sweep existing NULL-org rows first. | — | 1 d |
| **0f** | **D10** — make provisioning consistent: `organization_id` set on every seeded row. **Do not seed schedule defaults** — absence-means-inherit is the cascade's contract and `SETTING_DEFAULTS` already supplies them. Seed only one default `Shift`. | 0b | 0.5 d |
| **0g** | Regression test suite: Org A ≠ Org B for every schedule key; employee override does not leak to org; cross-org `shift_id` rejected; NULL bucket unreachable from a real org. | 0a–0f | 1 d |
| **1** | `app/schedule_policy/` — keys, `KEY_META`, DEFAULTS registration. Keys visible in `GET /settings/scoped`, read by nothing. | 0 | 2 d |
| **2** | Alembic migration A+B+C. Verify upgrade/downgrade on a clone. | 1 | 1–2 d |
| **3** | `resolve_work_schedule` + batched variant + `classify_arrival`/`classify_departure` + `app/_tz.py`. **Pure + fully unit-tested, wired into nothing.** Mirrors how `attendance_policy` Phase 1 was landed. | 2 | 4–5 d |
| **4** | Backfill script + dry-run diff report. Run with engine off. | 3 | 2 d |
| **5** | Read path: `resolve_expected_work_seconds` delegates to the resolver; `/employees/team` gains `arrival_status` (**shadow-logged only**). Existing `status` untouched. | 4 | 3 d |
| **6** | Frontend: Working Schedule group + 3 new control types; `WorkingScheduleTab` for shift CRUD; retire `computeShiftInfo` derivation; `Very Late` pill. | 5 | 4–5 d |
| **7** | Write path: `attendance_alerts` on the resolver (fixes D4); clock-in early guard; `risk` + `reports` on the resolver. **Plus the three consumers missed in the first draft:** `intelligence/risk_engine.py:23,123`, `intelligence/burnout.py:154`, `employees/router.py:3823,3863`. Also collapse the third "late" definition (`reports/router.py:214,695,1227`, global 09:30 UTC) onto the resolver. **Gated on `engine_enabled`.** | 6 | 5 d |
| **8** | Rollout R1→R5 with shadow-diff review. | 7 | 1–2 wk |
| **9** | *Separate phase:* night-shift work-day attribution + the IST sweep (D5/D7) → unlocks Org C and non-IST tenants. | 8 | 1–2 wk |
| **10** | *Later:* payroll consumes `expected_work_seconds` + `working_days` (replaces the fixed 30-day divisor); overtime/night rules read `shift.config`. | 9 | — |

Steps 1–3 are pure additive and land safely behind no flag at all — they change no behavior. The first behavior change is step 5, and it is shadow-only. The first customer-visible change is step 6.

---

## 14. Multi-Tenant Isolation Checklist

Every item must have a test before step 8:

- [ ] Every `app_settings` read filters `organization_id` (resolver goes through `settings_scope` only)
- [ ] Every `shifts` / `shift_assignments` / `holidays` read filters `organization_id`
- [ ] `_resolve_scope` already validates the target employee belongs to the caller's org (`settings/router.py:219-222`) — extend the same check to shift ids in `schedule.shift_id` writes: **an admin must not be able to reference another org's shift**
- [ ] No process-global cache keyed without `organization_id` (D2)
- [ ] Test: Org A sets `working_days = Mon–Sat`; Org B's resolved schedule is unchanged
- [ ] Test: Org A holiday does not suppress Org B's late/absent alerts (D3 regression)
- [ ] Test: `schedule.shift_id` pointing at another org's shift is rejected
- [ ] `AuditLog` written for every scoped schedule change (already free via `PUT /settings/scoped`)

---

## 15. Worked Examples

**Org A** — `working_days = MON..FRI`, `clock_in_time = 09:00`, `grace = 15`, `late_after = 15`, `expected_work_hours = 8:45`, shift `09:00–18:00`, `config.breaks = [{Lunch, 13:00, 14:00}]`. Clock-in 09:08 → `present`; 09:20 → `late`; 11:00 → `very_late`.

**Org B** — `working_days = MON..SAT`, `clock_in_time = 10:30`, `grace = 5`, `expected_work_hours = 9:30`, shift `10:30–20:00`. Saturday is a working day, so the early-clock-out gate applies (today it would not: `flexible_clock_out_days` defaults to Sat/Sun).

**Org C** — three `Shift` rows (`Morning 06:00–14:00`, `Evening 14:00–22:00`, `Night 22:00–06:00 crosses_midnight=true`), `working_days = MON..SUN`. Fixed assignment = employee-scope `schedule.shift_id`; rotation = `ShiftAssignment` rows generated from `config.rotation`. **Requires step 9** to report correctly.

**Employee override** — Priya (Org A, team Engineering) is part-time: `User.employment_type = part_time`, employee-scope `schedule.expected_work_hours = 4:00`, `schedule.clock_in_time = 10:00`. She inherits `grace = 15` from Engineering's team override and `working_days` from the org. `sources` reports `expected_work_hours: employee`, `grace_minutes: team`, `working_days: org`.

---

# Appendix A — Canonical Inventory of Schedule/Attendance Logic

**Audit date:** 2026-08-02. Surfaces swept: `backend/app/**`, `frontend/src/**`, `desktop-tracker/*.js`, `workers/`, `browser-extension/`.

**Classification legend**

| Code | Meaning |
|---|---|
| **A** | Already goes through a resolver (`attendance_policy`) — keep, upgrade in place |
| **B** | Hardcoded value — must be replaced by a resolver read |
| **C** | Legacy / dead — delete, do not migrate |
| **D** | Must be migrated to the new Schedule Resolver |
| **—** | Out of scope: a genuinely different concept, keep as-is |

`workers/` is empty and `browser-extension/` contains no schedule logic. Neither appears below.

---

## A.1 Expected work hours — 5 backend definitions, 2 values, + 1 frontend copy

| # | Location | What it does | Class |
|---|---|---|---|
| 1 | `attendance_policy/resolvers.py:281` `resolve_expected_work_seconds` | The intended resolver. Org-only (no cascade), tier-blind (D8) | **A** → upgrade to delegate to Schedule Resolver |
| 2 | `attendance_policy/resolvers.py:61` `DEFAULT_EXPECTED_WORK_SECONDS = 28800` | 8h fallback | **A** — becomes a Schedule Resolver default |
| 3 | `attendance_policy/resolvers.py:148` `default_expected_seconds_for_type` | DB-free bulk variant, part-time 4h / else 8h | **A** — keep as the batch path |
| 4 | `risk/router.py:33` `DEFAULT_THRESHOLD_SECS = 31500` (8:45) | Risk threshold default | **B** |
| 5 | `risk/router.py:36` `_threshold_secs` | Reads `work_hours_threshold`, tier-blind (D8) | **D** |
| 6 | `reports/router.py:132-133` `EXPECTED_HOURS_PER_DAY = 8`, `OVERTIME_THRESHOLD_SEC` | Overtime basis at `:774` | **B** |
| 7 | `reports/router.py:1176` `default_expected_seconds_for_type(...) * days` | Utilization report | **A** → **D** (multiplies by raw day count, ignores working days) |
| 8 | `employees/router.py:3823,3863` `EXPECTED_HOURS_PER_DAY = 8` | Second copy; drives a deficit/risk payload at `:3867-3875` | **B** — *missed in the first plan draft* |
| 9 | `intelligence/risk_engine.py:23,123` `EXPECTED_HOURS_PER_DAY = 8.0` | Third copy; `worked_ratio` | **B** — *missed in the first plan draft* |
| 10 | `frontend/src/lib/attendancePolicy.js:16-39` | **Client-side duplicate of the whole policy** (8h/4h + alias map). Docstring admits it ignores org overrides. Consumed by `MyTeamTab.jsx:115,186,401` and `Dashboard.jsx:3974` | **D** — replace with a server-provided `expected_seconds` per row |
| 11 | `frontend/src/__tests__/attendancePolicy.test.js:38` asserts `8 * 3600` | Locks in the hardcoded value | **C** — rewrite against the helper |
| 12 | `settings/router.py:43` `work_hours_threshold: "8:45"` | System default | **A** |

**Two live values: 8:45 (risk) vs 8:00 (early clock-out / reports / intelligence / frontend).**

---

## A.2 Clock-in time — 5 independent sources

| # | Location | What it does | Class |
|---|---|---|---|
| 13 | `models.py:80` `User.default_clock_in_time` | Per-employee shift start, **outside the cascade** | **C** (deprecate) — read at resolver precedence 3 during migration, then drop |
| 14 | `models.py:81` `User.default_clock_in_permanent` | Permanent-vs-one-off flag | **C** |
| 15 | `employees/router.py:65` `SYSTEM_DEFAULT_CLOCK_IN = "10:30"` | Backend fallback | **B** — and it **disagrees with the 09:00 UI default** |
| 16 | `employees/router.py:4046-4175` | CRUD + bulk-apply for the legacy columns | **C** — replace with `/settings/scoped` writes |
| 17 | `employees/router.py:4186-4320` `/require-action` | Recomputes expected-vs-actual arrival from legacy columns | **D** |
| 18 | `frontend/AdminDashboard.jsx:2183` `SYSTEM_DEFAULT_CLOCK_IN = "10:30"` | Fourth copy of the default | **B** |
| 19 | `frontend/AdminDashboard.jsx:2239-2280` | Writes the legacy columns | **C** |
| 20 | `reports/router.py:1227,1238,1247` `scheduled_in = time(9,30)` | **A fifth clock-in definition, hardcoded 09:30 UTC**, ignores every per-user setting | **B** |
| 21 | `Shift.start_time` (`models.py:1239`) / `ShiftAssignment.start_time` | Real scheduled window; read by `/schedule/*` only — **no attendance consumer reads it** | **D** — becomes precedence 1 |

---

## A.3 Clock-out time

| # | Location | What it does | Class |
|---|---|---|---|
| 22 | `Shift.end_time`, `ShiftAssignment.end_time` | Scheduled end; unread by attendance | **D** |
| 23 | `employees/router.py:1423` `effective_start = max(clock_in, today_start_IST)` | Caps session at local midnight → **truncates night shifts** (D7) | **D** |
| 24 | `employees/router.py:2631` `_ABANDONED_GRACE_SECONDS = 15*60` | Auto-close silent sessions | **—** (tracker liveness, not schedule) |
| 25 | `desktop-tracker/main.js:2570-2660` `clockOutSilently` / `handleSuspend` / `handleShutdown` | Auto clock-out on sleep/shutdown; no shift awareness | **D** (light) — must not fire across a `crosses_midnight` boundary |
| 26 | *(nothing)* | No expected clock-out time exists anywhere today | **D** — new |

---

## A.4 Late arrival — 3 independent definitions

| # | Location | What it does | Class |
|---|---|---|---|
| 27 | `frontend/adminShared.jsx:81-150` `computeShiftInfo` | **Client-side truth.** Per-user shift + grace, hardcoded Sat/Sun, `?? 20` grace, phases `no_shift/before_shift/within_grace/late_present/absent_after_noon` | **D** — must become a formatter over server values |
| 28 | `frontend/adminShared.jsx:71` `_formatLateLabel` | "Late by X" string | **—** keep as formatter |
| 29 | `attendance_alerts.py:100,242-251` | Server copy for email alerts; comment says "kept in lock-step with the frontend" | **D** |
| 30 | `reports/router.py:130-131,214,695-701` `LATE_THRESHOLD_HOUR/MINUTE = 9:30` | **Global 09:30 UTC**; `_is_late()` at `:214` | **B** |
| 31 | `email_service.py:1711-1718` `shift_start` param, `f"{shift_start} IST"` | Renders the alert; hardcodes the "IST" label | **B** (label) |
| 32 | *(nothing)* | No "Very Late" exists anywhere | **D** — new |

---

## A.5 Grace period

| # | Location | What it does | Class |
|---|---|---|---|
| 33 | `models.py:82` `User.default_clock_in_grace_min` (default 20) | Per-employee grace, outside cascade | **C** (deprecate) |
| 34 | `employees/router.py:66` `SYSTEM_DEFAULT_CLOCK_IN_GRACE_MIN = 20` | Backend default | **B** |
| 35 | `attendance_alerts.py:85` `DEFAULT_GRACE_MIN = 20` | Second copy | **B** |
| 36 | `frontend/adminShared.jsx:97-98` `?? 20` | Third copy | **B** |
| 37 | `employees/router.py:4107-4114` validation `0..240` | Bounds | **A** — reuse bounds on the new key |
| 38 | `breaks/governance.py:217-230` `grace_minutes(db, break_type)` | **Break-overrun grace — a different concept** | **—** do not merge |
| 39 | `attendance_policy/resolvers.py:99` `disconnect_grace_minutes` | Tracker-silence grace | **—** different concept |

---

## A.6 Half-day

| # | Location | Class |
|---|---|---|
| 40 | **Nothing anywhere in the codebase.** `LeaveRequest` has half-day *leave*, which is unrelated to arrival classification | **D** — new |

---

## A.7 Weekend / working days — 8 backend + 2 frontend sites

| # | Location | What it does | Class |
|---|---|---|---|
| 41 | `attendance_policy/resolvers.py:242-258,328` `flexible_clock_out_days` + `_WEEKDAY_NAMES` | The only configurable weekend in the system; org-level only | **A** → becomes `schedule.working_days` (inverse) |
| 42 | `attendance_alerts.py:115` `weekday() >= 5` | Alert suppression | **D** |
| 43 | `employees/router.py:3135` `is_weekend = now_ist.weekday() >= 5` | Absent-threshold gating | **D** |
| 44 | `reports/router.py:478` `is_weekend = d.weekday() >= 5` | **Drives `working_days` / `absent_days` → payroll buckets** | **D** (highest impact) |
| 45 | `reports/router.py:1365` `weekday() < 5` | Working-day count | **D** |
| 46 | `reports/router.py:1787,1793` `weekday() in (5,6)` | Emits `is_weekend` to the client | **D** |
| 47 | `intelligence/burnout.py:154` `weekday() >= 5` | Weekend-hours burnout signal | **B** — *missed in the first plan draft* |
| 48 | `frontend/MyScheduleTab.jsx:304` `dow === 0 \|\| dow === 6` | Calendar shading | **D** (cosmetic) |
| 49 | `frontend/ReportsSection.jsx:418,611` consumes `d.is_weekend` | Server-driven already | **A** — fixed by #46 |
| 50 | `Shift.days_of_week` JSON `[0..4]` (`models.py:1241`) | Per-shift working days; **read by `/schedule/*` only** | **D** — becomes a resolver input |

---

## A.8 Holiday

| # | Location | What it does | Class |
|---|---|---|---|
| 51 | `attendance_policy/resolvers.py:261` `_is_holiday` | Org-scoped, correct | **A** |
| 52 | `attendance_alerts.py:118` | **No org filter — cross-tenant (D3)** | **D** + security fix |
| 53 | `employees/router.py:3139-3143` | Org-scoped, correct | **D** (route via resolver) |
| 54 | `reports/router.py:425-429` | Org-scoped, correct | **D** (route via resolver) |
| 55 | `holidays/router.py` CRUD + CSV import | Org-scoped throughout; `:215` / `:303` verify ownership after fetch — **not an IDOR** | **A** |
| 56 | `holidays` unique `(organization_id, date)` | Blocks multiple calendars per org | **A** — additive `calendar_key` (§4.4) |

---

## A.9 Timezone — hardcoded IST in 20 files

| # | Location | What it does | Class |
|---|---|---|---|
| 57 | `OrgSettings.default_timezone` (`models.py:1782`) | Per-org IANA tz — **stored and read by no attendance code** | **D** — becomes the resolver seed |
| 58 | 10 backend files define `timedelta(hours=5, minutes=30)`: `employees/router.py`, `reports/router.py`, `payroll/router.py`, `attendance_alerts.py`, `active_time/service.py`, `schedule/router.py`, `activity_log/router.py`, `breaks/governance.py`, `intelligence/burnout.py`, `proof_of_work/fraud_routes.py` | Day boundaries, thresholds, bucketing | **B** — bulk migration, step 9 |
| 59 | 10 frontend files hardcode `"Asia/Kolkata"`: `AdminDashboard.jsx`, `Dashboard.jsx`, `adminShared.jsx`, `ClockInWidget.jsx`, `ReportsSection.jsx`, `WorkspaceProfileTab.jsx`, `TimelapseTab.jsx`, `SettingsModule.jsx`, `Profile.jsx`, `SuperAdmin.jsx` | Display + IST-day derivation | **B** — step 9 |
| 60 | `email_service.py:1718` `f"{shift_start} IST"` | Hardcoded label in outbound mail | **B** |

**Deliberately deferred to step 9.** Because `schedule.timezone` defaults to `Asia/Kolkata`, new and legacy code agree for every current tenant. **No non-IST tenant may be onboarded before step 9.**

---

## A.10 Shift

| # | Location | What it does | Class |
|---|---|---|---|
| 61 | `Shift` model (`models.py:1232`) | Window + days + colour. **No expected-hours / grace / late fields** | **A** — extend (§4.2) |
| 62 | `ShiftAssignment` (`models.py:1256`) | Per-date assignment; exists, unused by attendance | **A** — becomes precedence 1 |
| 63 | `schedule/router.py:88-191` shift CRUD | Correctly org-scoped (verified update **and** delete) | **A** |
| 64 | `schedule/router.py:214` `assign_shift` | **Skips the shift-org check when both times are supplied → a cross-org `shift_id` persists (D13)** | **A** + security fix |
| 65 | `schedule/router.py:328` `/my-schedule`, `:620` `/day-insights`, `:780` `/team` | Read assignments; independent of attendance | **D** (align later) |
| 66 | `attendance_policy/resolvers.py:19-29,305` | Documents shift-level expected hours as **intentionally skipped** | **A** — this design fills the slot |
| 67 | `breaks/governance.py:261` `schedule_version_id(user)` | Provenance from legacy columns | **D** |
| 68 | `frontend/NotificationsModule.jsx:253` "10 min before shift" | Notification copy; no shift source behind it | **D** (later) |

---

## A.11 Attendance status — 4 derivations, 2 of them dead

| # | Location | Vocabulary | Class |
|---|---|---|---|
| 69 | `employees/router.py:3515-3546` (`/employees/team`) | `On Leave / Absent / Yet to Start / Stopped / Working / On Break` — **the canonical one** | **A** — keep untouched; add `arrival_status` alongside |
| 70 | `employees/router.py:3150` `hour >= 12` absent cutoff | Hardcoded noon | **B** |
| 71 | `employees/router.py:3152` `hour >= 18` **part-time** cutoff | Second hardcoded, employment-type-specific cutoff | **B** — *not previously catalogued* |
| 72 | `attendance_alerts.py:81,215` `SHIFT_NOON_HOUR = 12` | Alert copy of the cutoff | **B** |
| 73 | `frontend/adminShared.jsx:44` `SHIFT_NOON_HOUR = 12` | Third copy | **B** |
| 74 | `frontend/adminShared.jsx:38-40` `effectiveStatus` | Stale-session → `absent` | **D** |
| 75 | `reports/router.py:1671,1712-1717` | **Different vocabulary** — `Clocked Out` instead of `Stopped`, no `Yet to Start` | **D** — reconcile |
| 76 | `employees/status.py:5` `calculate_status` | **Dead.** Only importer is `service_old.py`, which nothing imports. References `user.on_leave` — a field that **does not exist** on `User` (verified) → would raise `AttributeError` if ever called | **C** — delete |
| 77 | `employees/service_old.py` | **Dead.** No importers (verified) | **C** — delete |

---

## A.12 Early clock-out

| # | Location | What it does | Class |
|---|---|---|---|
| 78 | `employees/router.py:1322-1405` | The Phase-2 gate. Calls both resolvers, returns `EARLY_CLOCK_OUT_REASON_REQUIRED`, fail-open | **A** — **no call-site change needed**; inherits the cascade automatically |
| 79 | `employees/router.py:1214-1215` `_EARLY_REASON_MIN/MAX` | Reason length bounds | **—** |
| 80 | `employees/router.py:1333` local `_IST_gate` | Its own IST literal | **B** |
| 81 | `employees/router.py:2808` `/early-clock-outs` | Reporting endpoint | **A** |
| 82 | `frontend/EarlyClockOutModal.jsx` | Renders server-supplied seconds | **A** — **zero change**, inherits per-employee hours |
| 83 | `resolvers.py:97` `early_clockout_enabled` | Declared kill switch, **read by nothing** | **A** — wire it or remove it |

---

## A.13 Risk alerts

| # | Location | What it does | Class |
|---|---|---|---|
| 84 | `risk/router.py:83-97` `_run_risk_check` | Threshold check | **D** |
| 85 | `risk/router.py:89-91` skips `part_time` only | **`contract` / `intern` are measured against 8:45** | **D** — skip when `expected == 0` instead |
| 86 | `risk/router.py:64` `_session_worked_secs` | Worked-time calc, breaks + offline excluded | **A** — **the canonical calc; reuse, never reimplement** |
| 87 | `RiskAlert.threshold_secs` default 31500 (`models.py:1124`) | Column default | **B** |
| 88 | `employees/router.py:3863-3875` | **Second, independent deficit/risk calc** at 8h × days | **B** |
| 89 | `intelligence/risk_engine.py:123-168` | **Third** risk calc at 8.0h × days | **B** |

---

## A.14 Burnout

| # | Location | What it does | Class |
|---|---|---|---|
| 90 | `intelligence/burnout.py:154` `weekday() >= 5` | Weekend-hours signal — wrong for Mon–Sat orgs | **B** |
| 91 | `intelligence/burnout.py` `LATE_NIGHT_HOUR_LOCAL` / `EARLY_MORNING_HOUR_LOCAL` | Late-night bucket; **flags every Org C night-shift worker as burnt out** | **D** — must read the shift window |
| 92 | `intelligence/burnout.py:145-148` own worked-secs calc | Duplicates #86 and ignores `offline_seconds` | **D** — reuse #86 |
| 93 | `intelligence/burnout.py` `NO_BREAK_HOURS` | No-break stretch | **—** |

---

## A.15 Reports

| # | Location | What it does | Class |
|---|---|---|---|
| 94 | `reports/router.py:464-560` monthly attendance | `working_days` / `present_days` / `absent_days` from hardcoded weekend + org holidays | **D** — highest-impact single site |
| 95 | `reports/router.py:774` overtime | `secs - 28800` | **D** |
| 96 | `reports/router.py:1176` utilization | Type default × **raw** day count | **D** |
| 97 | `reports/router.py:1227-1247` late report | Global 09:30 UTC | **D** |
| 98 | `reports/router.py:1787-1793` calendar | Emits `is_weekend` | **D** |
| 99 | `reports/router.py:1671-1717` daily roster | Divergent status vocabulary | **D** |

---

## A.16 Dashboard metrics

| # | Location | What it does | Class |
|---|---|---|---|
| 100 | `AdminDashboard.jsx:406-436` | Derives status / late per row via #27 + `istDateKey` | **D** |
| 101 | `AdminDashboard.jsx:4395` | Second `computeShiftInfo` call site | **D** |
| 102 | `MyTeamTab.jsx:115,186,401` | Progress % from the client-side 8h/4h copy (#10) | **D** |
| 103 | `Dashboard.jsx:3974` | Employee's own expected hours from #10 | **D** |
| 104 | `employees/router.py:2932-2940` overview tiles | Counts by status | **A** — inherits #69 |
| 105 | `MyScheduleTab.jsx:152-182` | Reads `/schedule/*`; **not reconciled with attendance** | **D** |

---

## A.17 Desktop tracker

| # | Location | What it does | Class |
|---|---|---|---|
| 106 | `main.js:2328-2427` `fetchAndApplySettings` | Polls `GET /settings`, applies flat keys | **A** — schedule keys arrive free |
| 107 | `main.js:2461` 60 s re-poll | Propagation | **A** |
| 108 | `main.js:148,2372,2495,2853` `idleThresholdMins` | Idle flag | **—** |
| 109 | `main.js:2570-2660` auto clock-out paths | No shift awareness | **D** (light) |
| 110 | `tracking/router.py:610` `_get_screenshot_interval` | **Org-blind + `team_id` crash → always 30 (D9)** | **B** + fix |
| 111 | `tracking/router.py:643` `_get_active_poll_period` | Same defect → always 3 | **B** + fix |
| 112 | `active_time/config.py:219` `load_config` | Same defect → **all 20 tunables dead for every tenant (D9)** | **B** + fix |

---

## A.18 Payroll

| # | Location | What it does | Class |
|---|---|---|---|
| 113 | `payroll/service.py:104` `get_worked_hours` | Sums `total_seconds`; **no expected-hours or working-days input** | **D** — step 10 |
| 114 | `payroll/service.py:95` `compute_lwp_deduction` | **Fixed 30.0 divisor** — ignores real working days | **B** — step 10 |
| 115 | `payroll/service.py:144-159` | Hourly gross from raw worked hours | **D** — step 10 |
| 116 | `payroll/router.py` 3 IST literals | Period boundaries | **B** — step 9 |
| 117 | `reports/router.py:531` comment: "keep the day in working_days so the math stays…" | Report `working_days` already feeds payroll reasoning | **D** — fixing #94 changes payroll inputs |

**Payroll stays on legacy logic until step 10.** Stated explicitly: it is *not* on the resolver at the end of step 8.

---

## A.19 Totals

| Class | Count | Meaning |
|---|---|---|
| **A** — already resolver-backed | 22 | Keep; upgrade in place |
| **B** — hardcoded | 34 | Replace with resolver reads |
| **C** — legacy / dead | 8 | Delete (2 are fully dead modules) |
| **D** — must migrate | 49 | Wire to the Schedule Resolver |
| **—** — out of scope | 8 | Distinct concepts; leave alone |
| **Total** | **121** | |

### Duplication the single resolver eliminates

| Concept | Independent implementations today |
|---|---|
| Expected work hours | **6** (5 backend + 1 frontend), 2 divergent values |
| Clock-in time / shift start | **5** |
| Late arrival | **3** |
| Grace period | **4** |
| Absent cutoff | **4** (incl. a part-time-only 18:00 rule) |
| Weekend | **8** |
| Attendance status | **4** (2 of them dead code) |
| Worked-seconds calculation | **3** (#86 canonical; #92 and payroll diverge) |
| Timezone | **20 files** |
| Half-day | **0** — does not exist |

### Additions to the step plan from this audit

Not present in the earlier draft; folded into steps 7 and 9:

- `intelligence/burnout.py` — weekend (#90), late-night window (#91), duplicate worked-secs (#92)
- `intelligence/risk_engine.py` — third expected-hours + risk calc (#89)
- `employees/router.py:3823-3875` — second expected-hours + deficit calc (#8, #88)
- `employees/router.py:3152` — part-time 18:00 absent cutoff (#71)
- `reports/router.py:1671-1717` — divergent status vocabulary (#75)
- `frontend/src/lib/attendancePolicy.js` + its test — client-side policy duplicate (#10, #11)
- `employees/status.py` + `employees/service_old.py` — delete as dead code (#76, #77)

---

# Appendix B — Delivery Plan (one PR per phase)

**Status:** approved for execution 2026-08-02. Auditing closed. Each phase is one PR; work stops after each for review.

**Standing rules for every phase**
- No unrelated refactoring. Touch only files named in the phase.
- Backward compatible until P9. Legacy columns keep being read and written.
- Every new attendance calculation goes through the Schedule Resolver. No second source of truth.
- Every DB query filters `organization_id`. Every API validates the target belongs to the caller's org.
- Unit tests + migration notes in every PR.
- Worked-seconds is always `risk/router.py:_session_worked_secs` (#86). Never reimplemented.

---

## P1 — Isolation A: tier-blind reads + the `team_id` crash

**Fixes:** D8, D9. **Blocking** — the resolver inherits both defects otherwise.

| | |
|---|---|
| Scope | Add `team_key IS NULL AND user_id IS NULL` to org-level reads, or route them through `settings_scope`. Fix `AppSettings.team_id` → `team_key` at all 4 sites. |
| Files | `attendance_policy/resolvers.py:206`, `risk/router.py:37,51`, `employees/router.py:1858,2257`, `breaks/router.py:62,83`, `activity_log/router.py:251`, `settings/router.py:150`, `active_time/config.py:221`, `tracking/router.py:618,651` |
| Tests | Employee-scope override must not change the org value (the repro from §1.9); `PUT /settings/update` round-trips; `load_config` honours a stored org value; `_get_screenshot_interval` honours a stored value |
| Migration | None |
| ⚠️ Behavior change | Active Time tunables, `screenshot_interval` and `active_poll_period_seconds` have been silently dead. Fixing them makes **any value already saved in the DB take effect on deploy.** Query `app_settings` for these keys per org first and report the diff in the PR. If any org has non-default values, split this half into its own release. |
| Exit | Repro tests pass; diff report attached; no attendance output changes for orgs with no overrides |

`leave/` and `workspace/` tier-blind sites (22) are **deferred** — different subsystem, not in the attendance path. Logged, not fixed here.

---

## P2 — Isolation B: org-blind reads, cross-org refs, orphaning

**Fixes:** D2, D3, D10, D11, D13.

| | |
|---|---|
| Scope | Org filter + org-keyed cache in `breaks/governance.py:165`. Org filter on `attendance_alerts.py:118`. Unconditional shift-ownership check in `schedule/router.py:214` (+ audit `PUT /assign/{aid}`). `ON DELETE SET NULL` → `CASCADE` on `app_settings`, `shifts`, `shift_assignments`. Provisioning: set `organization_id` on every seeded row. |
| Files | `breaks/governance.py`, `attendance_alerts.py`, `schedule/router.py`, `super_admin/router.py:1203-1212`, `commercial/provisioning.py:307,320`, + 1 migration |
| Tests | Org A holiday does not suppress Org B alerts; Org A break settings invisible to Org B; cross-org `shift_id` rejected 404; provisioned rows carry `organization_id` |
| Migration | Sweep existing `organization_id IS NULL` rows in `app_settings`/`holidays`/`shifts` **before** the FK change; report counts. Verify `upgrade` **and** `downgrade -1` on a clone. |
| Decision needed | Whether `holidays` and `teams` also move to CASCADE, or keep SET NULL for recoverability |
| Exit | Isolation test suite green; NULL-bucket sweep report attached |

---

## P3 — Schema

| | |
|---|---|
| Scope | Migration A (extend `shifts`: expected/grace/late/very-late/half-day/min-hours/allow-flags/`crosses_midnight`/`timezone`/`is_default`/`config` JSONB), B (`shift_assignments`: `expected_work_seconds`, `is_off_day`), C (`holidays.calendar_key` + relaxed unique). All nullable or DEFAULT-backed. |
| Files | One Alembic revision. Re-derive `down_revision` with `alembic heads` — do not assume `d5e8a1c34b70`. |
| Tests | Model round-trip for every new column; `config` JSONB accepts the documented shape |
| Migration | `ADD COLUMN IF NOT EXISTS` style. Working `downgrade()`. No table rewrite. Verified on a clone. |
| Exit | `upgrade head` + `downgrade -1` clean; nothing reads the new columns yet |

---

## P4 — Keys + registration (no consumers)

| | |
|---|---|
| Scope | New `app/schedule_policy/keys.py`: the 17 `schedule.*` keys, `SETTING_DEFAULTS`, `SETTING_DESCRIPTIONS`, `KEY_META` contribution, `"Working Schedule"` group first in `GROUP_ORDER`. Register via the existing `try/except` merge. Add `contract` + `intern` to `_EMPLOYMENT_TYPE_ALIASES` and `DEFAULT_EXPECTED_WORK_DURATIONS`. |
| Files | `app/schedule_policy/{__init__,keys}.py`, `settings/router.py` (merge block only), `settings/scope.py` (`KEY_META` + `GROUP_ORDER`), `attendance_policy/resolvers.py` (aliases only) |
| Tests | Keys appear in `GET /settings/scoped` at all 3 scopes; `PUT /settings/scoped` accepts them and writes at the right tier; `DELETE` resets to parent; unknown key still 400s |
| Migration | None — keys are rows, absence means inherit |
| ⚠️ Decision needed before merge | `schedule.expected_work_hours` default: **8:45** (matches risk engine) or **8:00** (matches today's early-clock-out gate). Plan assumes 8:45. |
| Exit | Keys readable/writable; `engine_enabled=false`; **read by nothing**; no behavior change |

---

## P5 — Resolver + classifier (pure, wired into nothing)

| | |
|---|---|
| Scope | `resolve_work_schedule(db, user, on_date) -> EffectiveSchedule` with field-level precedence 1–7 and `sources` provenance. Batched `resolve_work_schedules(...)`. `classify_arrival` / `classify_departure` — pure, no DB. `app/_tz.py`: `org_timezone`, `local_now`, `local_day_bounds`. |
| Files | `app/schedule_policy/{resolver,classify}.py`, `app/_tz.py` |
| Tests | Precedence: assignment > employee > legacy user cols > team > org > shift > system, per field. Org A/B/C profiles from §15. `classify_arrival` boundary table (09:08→present, 09:20→late, 11:00→very_late). Malformed config never raises. Legacy-column fallback reproduces today's values exactly. Tenant isolation on every read. |
| Migration | None |
| Exit | 100% branch coverage on precedence + classification; **zero call sites** |

---

## P6 — Backfill (engine off)

| | |
|---|---|
| Scope | Idempotent per-org script writing only values that reproduce current behavior (tz, working_days, expected hours from `work_hours_threshold`, grace from modal `default_clock_in_grace_min`, absent hour 12, `engine_enabled=false`). One `is_default` Shift per org; `schedule.shift_id` left unset. Employee-scope rows for every deviation from the org modal. **Dry-run diff report first.** |
| Files | `backend/scripts/backfill_schedule_policy.py` |
| Tests | Idempotent (second run = zero writes); heterogeneous org produces per-employee rows; post-backfill resolver output == pre-backfill legacy output for every user |
| Migration | Data only, reversible by deleting `schedule.*` rows. Run per-org. |
| Exit | Dry-run diff reviewed and attached; live run on internal org only |

---

## P7 — Read path (shadow-logged)

| | |
|---|---|
| Scope | `resolve_expected_work_seconds` delegates to the resolver (Early Clock Out inherits the cascade with **no call-site change**). `is_flexible_clock_out_day` → `not is_working_day`. `/employees/team` gains `arrival_status`, `late_minutes`, `expected_seconds`, `shift_name`, `schedule_source` — **shadow-logged, plus `expected_seconds` consumed in P9.** Existing `status` untouched. Metric emitted when `arrival_status` disagrees with the legacy client derivation. |
| Files | `attendance_policy/resolvers.py`, `employees/router.py` (`/team` response only) |
| Tests | Early Clock Out honours an employee-scope `expected_work_hours`; existing `status` values byte-identical on a fixture set; shadow-diff metric fires on a seeded mismatch |
| Migration | None |
| Exit | No user-visible change; shadow-diff dashboard live |

---

## P8 — Organization Settings UI (the screenshot)

The **Working Schedule** group renders inside the existing `OrganizationSettingsTab` panel — same layout as Working Hours / Attendance / Screenshot Monitoring: `INHERITED` / `OVERRIDDEN` pill, `Inheriting: X` hint, `↺ Reset` link, at all three scopes via the Organization / Team / Employee selector already in the page.

| | |
|---|---|
| Scope | Three new control types in `control()`: `multiselect` (working-day chips), `shift_ref` (dropdown from `GET /schedule/shifts`), `time` (`<input type="time">`). `fmt()` renders arrays + shift names in the inheriting hint. Fetch the shift list alongside the existing employees/teams fetch. New `WorkingScheduleTab.jsx` for shift **definitions** (CRUD `app_settings` can't express). |
| Row order (locked 2026-08-02) | Employment Type · Working Days · Assigned Shift · Shift Start Time · Shift End Time · Expected Daily Hours · Grace Period · Late After · **Very Late After** · Half Day After · Allow Early Clock In · Allow Late Clock Out · Minimum Required Hours |
| Time Zone | **Excluded — already editable** in Company & Workspace Profile (`WorkspaceProfileTab.jsx:1187` → `OrgSettings.default_timezone`). The resolver reads it from there. Adding a second control would duplicate a setting. |
| Hidden keys | `schedule.engine_enabled`, `schedule.absent_after_hour`, `schedule.work_mode` are registered in `SETTING_DEFAULTS` but **omitted from `KEY_META`**, so they are not rendered — the pattern `scope.py:155` already documents ("advanced engine tunables live in DEFAULTS but aren't shown in this admin UI"). `engine_enabled` is an internal rollout switch, not a customer setting. |
| New screen | **Shift Management** only (shifts need CRUD). Styled as an existing Workspace Settings tab. |
| Employee scope | Assign Shift / Override Shift / Override Expected Hours / Override Employment Type are ordinary employee-scope rows — no bespoke UI |
| Files | `OrganizationSettingsTab.jsx`, new `WorkingScheduleTab.jsx`, `SettingsPage.jsx:183` (nav entry) |
| Tests | Component: each control type renders + emits the right serialized value; override → `OVERRIDDEN` + Reset appears; reset → `INHERITED` + `Inheriting: X`; non-admin read-only |
| Exit | Admin can configure Org A / B / C profiles end-to-end through the UI; engine still off |

---

## P9 — Single source of truth on the client

| | |
|---|---|
| Scope | `computeShiftInfo` stops deriving and reads `arrival_status` / `late_minutes` from the API row (keep `_formatLateLabel` as a formatter). Delete `frontend/src/lib/attendancePolicy.js` + its test; consumers use server `expected_seconds`. Add the `Very Late` pill. Delete dead `employees/status.py` + `employees/service_old.py`. |
| Files | `adminShared.jsx`, `AdminDashboard.jsx:406-436,4395`, `MyTeamTab.jsx:115,186,401`, `Dashboard.jsx:3974`, `attendancePolicy.js` (+test), `employees/status.py`, `employees/service_old.py` |
| Tests | Progress bars match server `expected_seconds` for a part-time fixture; Very Late renders; no remaining import of the deleted helper (lint rule) |
| ⚠️ First user-visible change | Late/very-late pills and progress bars now reflect org policy. Ship behind the same per-org `engine_enabled` read. |
| Exit | Grep proves zero client-side schedule derivation remains |

---

## P10 — Write path (gated on `engine_enabled`)

| | |
|---|---|
| Scope | `attendance_alerts` on the resolver (fixes weekend + noon + grace copies). Soft early-clock-in guard (structured 200, never 4xx). `risk/router.py` on the resolver; part-time skip → skip when `expected == 0`. `reports/router.py` working-days/overtime/utilization/late/calendar/status-vocabulary. Plus the three consumers found in Appendix A: `intelligence/risk_engine.py`, `intelligence/burnout.py` (weekend + late-night window + duplicate worked-secs), `employees/router.py:3823-3875` and the part-time 18:00 cutoff at `:3152`. |
| Files | `attendance_alerts.py`, `employees/router.py`, `risk/router.py`, `reports/router.py`, `intelligence/{risk_engine,burnout}.py` |
| Tests | Org B (Mon–Sat) working-days/absent-days correct; contract/intern no longer measured against 8:45; alert fires at org grace not 20 min; report status vocabulary matches `/employees/team` |
| Exit | Engine on for internal org only; all six subsystems on one resolver |

---

## P11 — Rollout

R1 internal (3-day shadow diff, zero unexplained) → R2 two design partners, one Mon–Fri one Mon–Sat (full-week manual reconciliation) → R3 UI visible to all admins → R4 on by default for new orgs → R5 backfilled orgs in batches with per-batch diff review. Per-org `engine_enabled` is both the rollout and the rollback unit.

**Deliverable:** rollout log + diff reviews. No code beyond flag flips and fixes found in review.

---

## P12 — Night shift + timezone sweep

Unlocks Org C and non-IST tenants. Work-day attribution (a session belongs to its shift's logical date): fix `employees/router.py:1423` truncation and every `midnight IST` bucket in `employees/router.py`, `reports/`, `active_time/service.py`. Then the tz sweep: 10 backend files defining the IST offset, 10 frontend files hardcoding `Asia/Kolkata`, and the `"IST"` label in `email_service.py:1718`.

**Gate:** no non-IST tenant and no 24×7 customer may be onboarded before this merges.

---

## P13 — Payroll adoption

`payroll/service.py` consumes `expected_work_seconds` + resolver working-days, replacing the fixed 30.0 LWP divisor. Overtime/night rules read `shift.config`. **Payroll stays on legacy logic through P12 — stated explicitly, not an oversight.**

---

## Sequencing summary

| Phase | Behavior change | Gate |
|---|---|---|
| P1 | ⚠️ dormant settings become live | diff report |
| P2 | none | isolation tests |
| P3 | none | migration verified |
| P4 | none | — |
| P5 | none | coverage |
| P6 | none | dry-run diff |
| P7 | none (shadow) | shadow-diff clean |
| P8 | none (config only) | — |
| P9 | ⚠️ first visible | per-org flag |
| P10 | ⚠️ engine live | per-org flag |
| P11 | rollout | diff reviews |
| P12 | Org C + non-IST | onboarding gate |
| P13 | payroll | — |

Blockers to raise immediately if hit: an Alembic head that doesn't match, an org with non-default Active Time values in P1, or any resolver precedence case that can't reproduce legacy output in P6.

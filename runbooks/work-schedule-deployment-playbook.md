# Work Schedule Policies — Production Deployment Playbook

**Covers:** commits `9aee1b2` … `83a7f53` on `fix/p1-settings-tier-isolation` (phases P1, P2, P2.5, D11, P3, P4, P5, P6).
**Status:** nothing has been applied to production. `alembic_version` is `e7ebc3f1fdb5`; code head is `f1a9d4c72e86`.
**Audience:** whoever runs the deploy. Every command and every check is copy-pasteable; no step requires judgement about what "should" happen.

---

## 0. THE ONE THING THAT WILL BREAK PRODUCTION

**`Dockerfile:58` migrates automatically on every container start, and swallows the failure:**

```
CMD ["sh", "-c", "alembic upgrade heads || echo '[startup] alembic migration failed …'; exec gunicorn …"]
```

The `||` is deliberate — it exists because gating gunicorn on the migration caused the 2026-06-23 Cloud Run outage (see the internal Cloud Run startup-failure note, not included in this repository). But it means a failed migration is **invisible**: the app starts anyway.

**If this branch is deployed without stamping first, the following happens automatically:**

1. `alembic upgrade heads` runs from `e7ebc3f1fdb5`.
2. It reaches `a1b2c3d4e5f7`, which has no existence guard and whose table already exists → **`DuplicateTable`**.
3. Alembic aborts. **Zero migrations apply.**
4. `||` swallows it. Gunicorn starts. The revision looks healthy.
5. The new `models.py` declares `holidays.calendar_key`, `shifts.is_default`, `shifts.config`, `shift_assignments.is_off_day`. **Every ORM query touching those tables fails with `UndefinedColumn`.**

There is no self-healing: the lifespan's `ADD COLUMN IF NOT EXISTS` blocks in `main.py` are commented out, and `create_all` only creates *missing tables* — it never adds a column to an existing one.

**Blast radius, verified by reading each call site:**

| Surface | Behaviour |
|---|---|
`GET/POST/PUT/DELETE /holidays` | **500** — admin Public Holidays page dead |
`/reports/*` monthly attendance (`reports/router.py:425`) | **500** |
`/schedule/shifts`, `/my-schedule`, `/team`, `/day-insights` | **500** — My Schedule tab dead |
`/employees/team` (`router.py:3139`) | Degrades silently — the holiday query is wrapped in `try/except`, so every day looks like a non-holiday |
`attendance_policy._is_holiday`, `resolver._holiday_dates` | Silently return "no holiday" — wrapped, fail-open |

**Therefore: the stamp is executed out of band, BEFORE the code deploy. This is not optional and not reorderable.**

> Making this failure automatically detectable is tracked separately as
> [ZT-OPS-01](../docs/ZT-OPS-01-deployment-migration-health.md) — deliberately outside
> this roadmap, and **not a prerequisite for this deployment**: Block C's
> "startup logs contain no `alembic migration failed`" is the manual equivalent.
> ZT-OPS-01's Layer 1 is two Cloud Logging alert policies with zero code, and is
> worth creating before this deploy runs.

---

## 1. Deployment order

Strictly sequential. Do not parallelise, do not skip a verification.

| # | Step | Where | Reversible? |
|---|---|---|---|
| 1 | Take a PITR checkpoint / snapshot | Supabase console | n/a |
| 2 | Record pre-state (§10 checklist, block A) | psql, read-only | n/a |
| 3 | `alembic stamp a1b2c3d4e5f7` | operator shell | yes (re-stamp) |
| 4 | `alembic upgrade head` | operator shell | yes (§3) |
| 5 | Verify schema + data (§10 block B) | psql, read-only | n/a |
| 6 | Deploy the code | merge → GitHub Actions | yes (Cloud Run revision rollback) |
| 7 | Verify the app (§10 block C) | HTTP + startup logs | n/a |
| 8 | 24h observation at engine OFF (§6) | monitoring | n/a |

**Why migration before code — both directions matter:**

- **New code + old schema = broken** (§0).
- **Old code + new schema = safe.** Every migration is additive; SQLAlchemy ignores columns its models don't declare. So step 6 is independently revertible without touching the database, which is what makes the whole plan recoverable.

**Steps 3 and 4 are run by a human against production, not by the container.** After step 4 succeeds, the container's automatic `alembic upgrade heads` becomes a no-op, which is the desired steady state.

---

## 2. Migration order

Five revisions. Two are skipped by stamping; three actually run.

```bash
cd backend

# Confirm the starting point. If this is not e7ebc3f1fdb5, STOP.
python -m alembic current

# Skip the two revisions that are already applied out-of-band.
python -m alembic stamp a1b2c3d4e5f7

# Apply the remaining three.
python -m alembic upgrade head

# Must print f1a9d4c72e86.
python -m alembic current
```

| Revision | Action | Why | Evidence |
|---|---|---|---|
`f0e1d2c3b4a5` | **stamped** | 8 commercial tables already exist and are **column-exact** against the models | P2.5 audit |
`a1b2c3d4e5f7` | **stamped** | `commercial_idempotency_keys` + 3 indexes + unique constraint already exist, column-exact. **This is the revision that would otherwise fail** — it has no existence guard | P2.5 audit |
`b3c6d9e2f4a8` | **runs** | `commercial_price_quotes` and `checkout_sessions.quote_id` genuinely do **not** exist | P2.5 audit |
`d5e8a1c34b70` | **runs** | Objects exist; `IF NOT EXISTS` throughout, so it no-ops. Will add a redundant `ix_offline_intervals_org` alongside the existing `ix_offline_intervals_organization_id` — same column, harmless | P2.5 audit |
`c4a7e2b915d3` | **runs** | Backfills the 6 NULL-org `app_settings` rows. Rehearsed: 6 updated, 2nd run 0, 0 dangling, 0 collisions | P2.5 rehearsal |
`e8b3c6d1f472` | **runs** | Adds 4 `ON DELETE RESTRICT` FKs. Rehearsed: added+validated, idempotent, org DELETE blocked, downgrade clean | D11 rehearsal |
`f1a9d4c72e86` | **runs** | Schedule schema: 7 columns, 2 indexes, holiday uniqueness swap. Rehearsed: idempotent, duplicate still rejected, downgrade restores original constraint | P3 rehearsal |

**Why stamping and not editing the two migrations:** their objects exist and match the models exactly, so the revisions are factually applied — the stamp is simply wrong. Editing them would leave the same false stamp behind and touch two migrations unrelated to this work. This is the same class of drift documented in the internal holidays Alembic-drift note (not included in this repository): **`alembic current` is not a reliable record of this database's schema — always verify `pg_constraint` / `pg_indexes` directly.**

---

## 3. Rollback procedure

### 3a. Code only (preferred first move)

Roll the Cloud Run revision back. **No database action needed** — old code tolerates the new schema. Use this for anything that is not provably a schema problem.

```bash
gcloud run services update-traffic <service> --to-revisions=<previous-healthy>=100
```

### 3b. Database

Roll back **code first**, then the schema, in reverse order. Never leave new code on an old schema.

```bash
python -m alembic downgrade e8b3c6d1f472   # undo P3 schedule schema
python -m alembic downgrade c4a7e2b915d3   # undo the RESTRICT FKs
python -m alembic downgrade b3c6d9e2f4a8   # undo the backfill (see below)
```

| Revision | Downgrade behaviour | Caveat |
|---|---|---|
`f1a9d4c72e86` | Drops the 7 columns and 2 indexes; **restores** `uq_holidays_org_date` | Safe: `calendar_key` is uniformly NULL while nothing writes it, so no row can violate the narrower constraint |
`e8b3c6d1f472` | Drops the 4 FKs | Returns to today's state of no referential integrity on those columns |
`c4a7e2b915d3` | **Intentional no-op** | The repaired rows become indistinguishable from rows the commercial path always wrote correctly. A reverse UPDATE would re-expose correctly-provisioned tenants. To revert one tenant, NULL that row by id manually |
`d5e8a1c34b70` | Drops `offline_intervals` and `clock_sessions.offline_seconds` | **Do not run.** This is pre-existing shipped functionality with live data |
`a1b2c3d4e5f7` / `f0e1d2c3b4a5` | Drop commercial tables | **Never run.** Live data |

**Hard floor: do not downgrade below `b3c6d9e2f4a8`.** Anything lower destroys data that predates this work.

### 3c. Last resort

Restore the PITR checkpoint from step 1. Costs the write window since the snapshot.

---

## 4. Feature flag rollout

The flag is `schedule.engine_enabled`, an `app_settings` row, default `"false"`. Per-org, so **one org's flip is both the rollout unit and the rollback unit.**

**Be clear about what it does today: nothing.** P5's resolver is wired into no code path, and P7/P10 have not been written. Deploying this branch changes no attendance behaviour regardless of the flag. The flag becomes meaningful only when P10 lands. Documented here so the mechanism is agreed before it has teeth.

Flip for one org:

```sql
-- enable
INSERT INTO app_settings (organization_id, key, value)
VALUES (:org_id, 'schedule.engine_enabled', 'true')
ON CONFLICT DO NOTHING;

-- disable (rollback)
DELETE FROM app_settings
WHERE organization_id = :org_id AND key = 'schedule.engine_enabled'
  AND team_key IS NULL AND user_id IS NULL;
```

| Stage | Scope | Gate to advance |
|---|---|---|
R0 | Migrations + backfill applied, engine **off** everywhere | §10 block B and C pass; 24h clean |
R1 | Internal test org (org 3 — 1 user) | 3 days shadow-diff with zero unexplained differences |
R2 | One small tenant (org 6, 5 users; or org 7, 3 users) | One full week; working-days and absent-days reconcile against manual expectation |
R3 | Working Schedule UI visible to all admins (P8) | — |
R4 | On by default for **new** orgs | — |
R5 | Org 1 (59 users) — the large tenant, last | Per-batch diff review |
R6 | Night-shift / 24×7 profile | **Blocked on P12.** Do not enable a rotating or overnight shift before then |

**Never flip org 1 first.** It holds 59 of 69 users, all 7 legacy clock-in configs, and all 11 part-timers.

---

## 5. Shadow mode verification

**Shadow mode does not exist yet — P7 must build it.** Recording the contract now so P7 is not designed ad hoc.

P7 adds `arrival_status`, `late_minutes`, `expected_seconds`, `shift_name` and `schedule_source` to the `/employees/team` response, **computed and logged but not rendered**. It must emit a metric whenever the resolver's answer differs from the legacy derivation, tagged `org_id` + `field`.

Required properties:

1. **Never affects the response the UI reads.** The existing `status` field keeps its exact values. A shadow computation that throws must be swallowed and counted, never surfaced.
2. **Compares against the live legacy path**, not a reimplementation — `attendance_policy.resolve_expected_work_seconds`, `attendance_alerts._parse_shift_start`, and the frontend's `computeShiftInfo` contract.
3. **Emits per-field difference counts**, so a regression is attributable rather than just "something changed".
4. **Runs with the engine OFF.** Shadow mode is how R1/R2 are earned; it must not require the flag.

Offline equivalence is already proven — 69 users × 5 dates, **345/345 user-days identical, zero differences**, before and after the backfill (`tests/test_schedule_backfill.py`). Shadow mode exists to confirm that on live traffic, against real clock-in times, which the offline proof cannot cover.

---

## 6. Monitoring

### 6a. Immediately after the migration (steps 4–5)

```sql
-- must be f1a9d4c72e86
SELECT version_num FROM alembic_version;

-- must be 0
SELECT COUNT(*) FROM app_settings WHERE organization_id IS NULL;

-- must be 4
SELECT COUNT(*) FROM pg_constraint
WHERE conname IN ('fk_app_settings_organization_id','fk_shifts_organization_id',
                  'fk_shift_assignments_organization_id','fk_holidays_organization_id');

-- must be 7
SELECT COUNT(*) FROM information_schema.columns
WHERE (table_name='shifts' AND column_name IN
        ('expected_work_seconds','crosses_midnight','is_default','config'))
   OR (table_name='shift_assignments' AND column_name IN
        ('expected_work_seconds','is_off_day'))
   OR (table_name='holidays' AND column_name='calendar_key');

-- must exist
SELECT indexname FROM pg_indexes WHERE indexname='uq_holidays_org_calendar_date';
```

### 6b. Cloud Run startup logs — the only place a migration failure is visible

Open the revision → **Open Cloud Logging** → **container startup logs**, not the request panel. Search for:

- `[startup] alembic migration failed` — the swallowed failure. **Treat as sev-1 even though the app is up.**
- `DuplicateTable`, `UndefinedColumn`, `psycopg2`, `Worker failed to boot`.

### 6c. First 24 hours, engine OFF

| Watch | Expected | Where |
|---|---|---|
5xx rate on `/holidays`, `/reports/*`, `/schedule/*` | **unchanged from baseline** | Cloud Run metrics |
5xx on `/employees/team`, `/employees/clock-out` | unchanged | Cloud Run metrics |
Late/absent alert volume | see §6d | `audit_logs` where `action IN ('EmployeeMarkedLate','EmployeeMarkedAbsent')` |
Early-clock-out prompts | unchanged | `audit_logs` / the early-clock-out email family |
DB connection errors | unchanged | Supabase logs |
`schedule.*` rows | exactly the 4 backfilled grace rows + 7 employee rows | `app_settings` |

### 6d. One expected alert-volume change — needs comms before deploy

The P2 fix to `attendance_alerts` (D3) is a **behaviour change, not a no-op**. Previously the monitor asked *"does ANY holiday exist today?"* with no org filter and aborted the whole tick — so **one tenant's holiday silenced late/absent alerts for every tenant**. It is now per-org.

Consequence: on a date where only some orgs observe a holiday, the others now receive alerts they previously did not. Production has exactly this case — **2026-09-04 is a holiday for orgs 3 and 6 only**, so orgs 1 and 7 will start getting Late/Absent alerts on that date.

Two other intentional changes in this deploy:

- **`POST /schedule/assign` now returns 404** for an unknown or cross-org `shift_id` where it previously succeeded silently (and could create an assignment with null times).
- **Active Time tunables, `screenshot_interval` and `active_poll_period_seconds` become functional** for the first time (they were dead behind the `AppSettings.team_id` crash). Production had **zero rows** for all 21 keys at audit time, so this is inert — **re-verify at step 2** with:

```sql
SELECT organization_id, key, value FROM app_settings
WHERE key LIKE 'active_time.%' OR key = 'screenshot_interval';
```

If that returns anything non-default, **stop and review** before deploying: those values will start taking effect.

---

## 7. Success metrics

The deploy is successful when **all** hold:

| # | Metric | Target |
|---|---|---|
1 | `alembic current` | `f1a9d4c72e86` |
2 | Startup logs contain `alembic migration failed` | **0 occurrences** |
3 | NULL-org `app_settings` rows | 0 (was 6) |
4 | Org-FK constraints present | 4 |
5 | New schedule columns present | 7 |
6 | 5xx rate on `/holidays`, `/reports/*`, `/schedule/*` | within baseline noise |
7 | `PUT /settings/update` | 200 (was a 500 — the `team_id` bug) |
8 | Attendance behaviour | unchanged, except the three documented changes in §6d |
9 | `schedule.*` rows | 11 total (4 grace + 7 employee start times) |
10 | Deleting an org | blocked with a FK violation, not silently cascading |

Metric 7 is the only *user-visible improvement* in this deploy. Everything else is isolation hardening and dormant foundation.

---

## 8. Failure conditions

Stop and roll back if any of these appear.

| Signature | Meaning | Action |
|---|---|---|
`alembic current` ≠ `f1a9d4c72e86` after step 4 | Migration partially applied | §9 recovery A. **Do not deploy code** |
`DuplicateTable` during step 4 | The stamp was skipped or wrong | Re-check `alembic current`, re-stamp, retry. Do not edit migrations under pressure |
`[startup] alembic migration failed` in startup logs | Swallowed failure; app is serving on an unknown schema | §9 recovery B — sev-1 despite the app being up |
`UndefinedColumn: holidays.calendar_key` (or `shifts.is_default`, `shift_assignments.is_off_day`) | New code on old schema — the §0 scenario | §9 recovery B **immediately** |
5xx spike on `/holidays`, `/reports/*`, `/schedule/*` | Same as above | §9 recovery B |
`RuntimeError: Cannot add organization foreign keys` | Dangling `organization_id` appeared since the audit | §9 recovery C |
Duplicate-holiday inserts now accepted | Uniqueness swap went wrong | §9 recovery A, then re-verify `uq_holidays_org_calendar_date` |
Any org's `work_hours_threshold` behaviour changes | Expected-hours regression | Roll back code (§3a); the resolver is unwired, so this should be impossible — investigate before retrying |
Alert volume changes beyond §6d | Unmodelled `attendance_alerts` change | Roll back code (§3a) |

---

## 9. Recovery procedure

### Recovery A — migration partially applied

1. **Do not deploy code.** Old code on a partial new schema is safe; new code is not.
2. Capture the real state — never trust the version number:
   ```sql
   SELECT version_num FROM alembic_version;
   SELECT conname FROM pg_constraint WHERE conname LIKE 'fk_%_organization_id';
   SELECT indexname FROM pg_indexes WHERE tablename IN ('shifts','holidays','offline_intervals');
   SELECT column_name FROM information_schema.columns WHERE table_name='shifts';
   ```
3. Every one of the three migrations that runs is idempotent (`IF NOT EXISTS`, catalog guards, `organization_id IS NULL` predicates). **Re-running `alembic upgrade head` is the first remedy.**
4. If it fails again, downgrade to `b3c6d9e2f4a8` (§3b) and escalate. Do not go lower.

### Recovery B — new code on an old schema

1. **Roll back the Cloud Run revision immediately** (§3a). This restores service without touching the database, because the old code does not reference the new columns.
2. Confirm 5xx returns to baseline on `/holidays`, `/reports/*`, `/schedule/*`.
3. Then fix the migration (Recovery A) and re-deploy. Order is non-negotiable: schema first.

### Recovery C — dangling `organization_id` blocks the FK migration

`e8b3c6d1f472` pre-flights this and aborts with per-table counts rather than failing inside `VALIDATE`. Find them:

```sql
SELECT 'app_settings' t, COUNT(*) FROM app_settings x
 WHERE x.organization_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM organizations o WHERE o.id = x.organization_id)
UNION ALL SELECT 'shifts', COUNT(*) FROM shifts x
 WHERE x.organization_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM organizations o WHERE o.id = x.organization_id)
UNION ALL SELECT 'shift_assignments', COUNT(*) FROM shift_assignments x
 WHERE x.organization_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM organizations o WHERE o.id = x.organization_id)
UNION ALL SELECT 'holidays', COUNT(*) FROM holidays x
 WHERE x.organization_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM organizations o WHERE o.id = x.organization_id);
```

Re-point or delete those rows deliberately. **The migration will not guess an owner, and neither should you.**

### Recovery D — the backfill wrote a wrong `organization_id`

It cannot invent a tenant (`EXISTS` guard) and cannot collide (`NOT EXISTS` guard), and only touches the 6 keys matching `^(shared_pool_enabled|leave_waiting_period_months):[0-9]+$`. To revert one row: `UPDATE app_settings SET organization_id = NULL WHERE id = :id;`

---

## 10. Production checklist

### Block A — before touching anything

- [ ] PITR checkpoint / snapshot taken, timestamp recorded
- [ ] `python -m alembic current` → **`e7ebc3f1fdb5`** (if not, STOP)
- [ ] `python -m alembic heads` → **exactly one head**, `f1a9d4c72e86`
- [ ] Record: `SELECT COUNT(*) FROM app_settings` → expect **98**
- [ ] Record: NULL-org rows → expect **6**
- [ ] Record: `shifts` / `shift_assignments` / `holidays` → expect **0 / 0 / 52**
- [ ] Record: users with `default_clock_in_time` → expect **7**; total users → **69**
- [ ] **Re-verify Active Time / screenshot keys return 0 rows** (§6d). If not, stop and review
- [ ] Dangling-`organization_id` check (Recovery C query) → all **0**
- [ ] Baseline 5xx rates captured for `/holidays`, `/reports/*`, `/schedule/*`, `/employees/team`
- [ ] Deploy window agreed; someone with Supabase + Cloud Run access is on call
- [ ] §6d behaviour changes communicated to whoever fields HR/admin questions
- [ ] Confirmed: the person running this is **not** using a local `.env` pointed at prod for anything else concurrently (see the internal note on a local `.env` pointing at production, not included in this repository)

### Block B — after the migration, before the code deploy

- [ ] `alembic stamp a1b2c3d4e5f7` completed
- [ ] `alembic upgrade head` completed with **no error output**
- [ ] `alembic current` → **`f1a9d4c72e86`**
- [ ] All five §6a verification queries return their expected values
- [ ] `commercial_price_quotes` table exists; `commercial_checkout_sessions.quote_id` exists
- [ ] Duplicate-holiday probe still rejected (insert a duplicate `(organization_id, date)` in a transaction and **roll back**)
- [ ] Row counts otherwise unchanged: `app_settings` 98, `holidays` 52
- [ ] Existing app still healthy on the **old** revision (old code + new schema is a supported state)

### Block C — after the code deploy

- [ ] Cloud Run revision serving traffic; `/health` 200
- [ ] **Startup logs contain no `alembic migration failed`** ← the highest-value check in this document
- [ ] Startup logs contain no `UndefinedColumn` / `DuplicateTable`
- [ ] `GET /holidays` 200 as an admin
- [ ] Admin Public Holidays page renders
- [ ] `GET /schedule/shifts` 200; My Schedule tab renders
- [ ] Monthly attendance report renders
- [ ] `PUT /settings/update` 200 (the fixed `team_id` bug)
- [ ] Organization Settings page renders **11 rows across 5 groups** — no "Working Schedule" section (P4 keys are registered but deliberately not in `KEY_META` until P8)
- [ ] `GET /settings` includes the 16 `schedule.*` keys
- [ ] Clock in and clock out once as a test user; early-clock-out prompt behaves as before
- [ ] `/employees/team` returns the same statuses as before the deploy
- [ ] Engine flag confirmed OFF everywhere:
      `SELECT COUNT(*) FROM app_settings WHERE key='schedule.engine_enabled'` → **0**

### Block D — 24 hours later

- [ ] 5xx rates within baseline on all four surfaces
- [ ] Late/absent alert volume explained by §6d, not by anything else
- [ ] No new `UndefinedColumn` / `IntegrityError` in logs
- [ ] Deleting a test org is blocked by a FK violation (verify in a rolled-back transaction)
- [ ] Sign-off recorded → R1 (internal org) may proceed once P7 shadow mode exists

---

## Appendix — known issues carried into this deploy

| Issue | Impact | Owner phase |
|---|---|---|
`tzdata` not in `requirements.txt`; runtime is `python:3.12-slim` | `ZoneInfo` may raise, so **every org resolves to IST regardless of config**. Harmless now (fallback is +05:30, identical to the hardcoded literals); blocks any non-IST tenant | P12 |
`OrgSettings.default_timezone` holds free text — org 1 is `'California, Sacramento'`, orgs 6 and 7 are NULL | Only org 3 has a valid IANA value. All fall back to IST. Needs a validated picker before the field means anything | P12 |
`schedule.shift_end_time` defaults to `18:00` while `shift_start_time` is `""` | Incoherent "end without a beginning". No effect today; latent risk if a P10 `allow_late_clock_out` guard reads `end_local` without checking `start_local` | P10 (or a one-line P4 fix) |
`teams.organization_id` still `ON DELETE SET NULL`, and 2 rows have NULL `organization_id` | Out of D11's scope — it already had an FK, so changing it is a separate decision | unassigned |
5 org-blind `app_settings` reads remain in `leave/router.py` | Different tenancy convention (org id embedded in the key). Functionally isolated but bypasses the cascade | D12, unassigned |
4 pre-existing test failures (`test_break_governance`, `test_kairos`, `test_qa_matrix`, `test_reports_new_joiners`) | Present on unmodified code; not caused by this work. All are test-isolation/ordering issues in the shared in-memory DB | unassigned |
`ix_offline_intervals_org` will be created alongside `ix_offline_intervals_organization_id` | Two indexes on the same column of a small table. Harmless; optional cleanup | unassigned |

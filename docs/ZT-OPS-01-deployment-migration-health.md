# ZT-OPS-01 — Deployment migration health: detection, reporting, monitoring

**Type:** Infrastructure / operational hardening
**Status:** Design approved for documentation. **No implementation.**
**Scope boundary:** Standalone ticket. **Outside the Work Schedule Policies roadmap** — that roadmap must not depend on this, and this must not be sequenced inside it.
**Origin:** Discovered while writing [runbooks/work-schedule-deployment-playbook.md](../runbooks/work-schedule-deployment-playbook.md) §0.

---

## Non-negotiable constraints

These are requirements of the design, not preferences. Any implementation that violates one is wrong.

1. **Deployment behaviour does not change.** No change to `Dockerfile`, `deploy.yml`'s deploy step, or startup sequencing as part of the detection work.
2. **Startup is never gated on migrations.** `alembic upgrade heads || echo …; exec gunicorn …` stays exactly as it is. The `||` exists because gating gunicorn on the migration caused the 2026-06-23 Cloud Run outage — see the internal Cloud Run startup-failure note (not included in this repository). Re-introducing a gate would trade a silent-failure problem for a total-outage problem.
3. **`GET /health` stays static and DB-free.** It currently returns `{status, version, timestamp}` and touches nothing. That is load-bearing: the runbook's primary diagnostic is *"a static handler 500ing in 0 ms = nothing is serving traffic at all."* Adding a DB read to `/health` would destroy that signal and make liveness depend on the pooler. Migration health goes on a **separate** endpoint.
4. **Nothing in this ticket may 5xx or raise.** A health surface that errors is indistinguishable from the app being down.
5. **No new load path.** Detection reads at most one tiny row, cached.

---

## Problem statement

`Dockerfile:58`:

```
CMD ["sh", "-c", "alembic upgrade heads || echo '[startup] alembic migration failed …'; exec gunicorn …"]
```

A failed migration is currently **undetectable without a human opening Cloud Logging and reading container startup logs.** The revision reports healthy, `/health` returns 200, and the app serves traffic against whatever schema happens to exist.

### Two distinct failure modes

Both must be detected. They are not the same problem and a check for one does not cover the other.

| | **Mode A — upgrade aborted** | **Mode B — silent drift** |
|---|---|---|
What happens | `alembic upgrade heads` exits non-zero; **zero** revisions apply | `alembic upgrade` exits **0**, but the schema does not match what the revision claims |
Cause | A migration raises — e.g. `DuplicateTable` on a revision with no existence guard; or the pooler drops the connection at boot | `alembic_version` was stamped past revisions that never ran; tables came from `create_all`, which creates missing tables but **never alters existing ones** |
Signal today | The `[startup] alembic migration failed` log line | **None whatsoever** |
Precedent | The `a1b2c3d4e5f7` scenario in the playbook §0 | the internal holidays Alembic-drift note (not included in this repository) — version stamped ahead, the global `ix_holidays_date` index survived, and adding a holiday 500'd |

**Mode B is the more dangerous of the two** because `alembic current` reports success. The governing lesson from that incident: *the Alembic version number is not a reliable record of this database's schema.*

### Blast radius when undetected

From the playbook, verified per call site: `/holidays`, `/reports/*` and `/schedule/*` return 500, while `/employees/team` and the attendance policy resolvers **degrade silently** because their reads sit inside `try/except`. Silent degradation is worse than a 500 — it produces wrong attendance data with no error anywhere.

---

## Part 1 — Startup migration failure detection

Three layers, ordered by cost. **Layer 1 requires no code and no deploy and should ship first, on its own.**

### Layer 1 — Log-based alert (zero code, available today)

Cloud Logging alert policy on the literal marker the `||` branch already emits:

```
resource.type="cloud_run_revision"
textPayload:"[startup] alembic migration failed"
```

- Severity **sev-1**, paging, even though the service is up.
- Detection latency: log-ingest time, typically < 1 min.
- **Requires nothing from this repo.** It is a console-side change.

**Honest limitation: Layer 1 detects Mode A only.** It cannot see Mode B, because in Mode B alembic exits 0 and prints nothing unusual.

Recommended refinement, still zero-code: also alert on `UndefinedColumn`, `DuplicateTable`, `UndefinedTable` and `psycopg2.errors` appearing in startup logs — these are the *consequences* of both modes and catch Mode B indirectly, at first request rather than at boot.

### Layer 2 — Explicit structured startup log line

One additional log emission, observational only, in the FastAPI lifespan:

- Compare the DB's `alembic_version.version_num` against the code's head revision (obtained in-process from Alembic's `ScriptDirectory`, no DB access).
- Emit exactly one line: `INFO` when they match, **`ERROR`** when they do not, `WARNING` when the DB is unreachable.
- Include `db_revision`, `code_head`, and the count of pending revisions.
- **Wrapped so it can never raise or delay startup.** A failure to check is logged and ignored.

This makes Mode A *and* the version-level part of Mode B visible in a single greppable line, rather than requiring someone to notice the absence of a success message.

**It does not gate anything.** It logs and returns.

### Layer 3 — Post-deploy pipeline check (gates the pipeline, never the container)

After `gcloud run deploy` in `deploy.yml`, curl the new revision's migration-health endpoint and fail the **workflow** if the revision is behind.

This is explicitly *not* startup gating: the container has already started and is serving. What fails is the deploy job, so a human is told immediately rather than discovering it days later. The service keeps running either way.

Needs a decision on whether a mismatch should also **halt traffic migration** to the new revision. Default recommendation: **no** — report loudly, do not auto-rollback. Auto-rollback on a pooler blip would be its own outage.

---

## Part 2 — Migration health reporting

### New endpoint: `GET /health/migrations`

Separate from `/health` (constraint 3). Response is always **HTTP 200** — status lives in the body, because a 5xx here is ambiguous with the app being down (constraint 4).

```jsonc
{
  "status": "ok",                    // ok | behind | ahead | unknown
  "db_revision": "f1a9d4c72e86",
  "code_head":   "f1a9d4c72e86",
  "pending_count": 0,
  "pending": [],                     // revision ids the DB has not applied
  "checked_at": "2026-08-04T…Z",
  "cache_age_seconds": 12
}
```

| `status` | Meaning | Alert |
|---|---|---|
`ok` | `db_revision == code_head` | none |
`behind` | Code expects revisions the DB has not applied — **Mode A** | sev-1 |
`ahead` | DB is at a revision this code does not know — a rollback left the schema forward | sev-2, informational; old code on a new schema is a supported state |
`unknown` | DB unreachable, or `alembic_version` missing/unreadable | sev-3 after sustained duration only — a single pooler blip must not page |

**Implementation constraints for whoever picks this up:**

- One query: `SELECT version_num FROM alembic_version`. Nothing else.
- Cache the result **60 s**, process-local. This endpoint may be polled by uptime checks; it must not become a load source. Follow the existing TTL-cache pattern, and **key nothing on a mutable global** — see the P2 finding where a process-global settings cache keyed without `organization_id` leaked across tenants.
- `code_head` comes from `ScriptDirectory.from_config(...).get_heads()` — in-process, no DB.
- Every failure path returns `unknown`. No exception escapes.
- Exempt from rate limiting, as `/health` already is.
- Revision ids are public repo contents, so exposing them is not a disclosure concern. No auth required, which keeps it usable by uptime checks.

### `status: "ahead"` is not an error

Worth stating because it will occur during a rollback and should not page anyone. Old code tolerates a newer additive schema — SQLAlchemy ignores columns its models do not declare. That property is what makes code-only rollback safe, and the playbook depends on it.

### Deliberately out of scope: object-level drift verification

`/health/migrations` compares **version numbers**. It cannot detect Mode B in general, because Mode B is precisely the case where the number is right and the schema is wrong.

Real Mode B detection requires asserting that each revision's *objects* exist — the approach the P2.5 audit used (querying `information_schema.columns`, `pg_constraint`, `pg_indexes` per revision). That is a meaningfully larger piece of work and belongs in a follow-up ticket, **ZT-OPS-02**, not here. Proposed shape: promote the P2.5 audit script into a maintained read-only checker with a per-revision object manifest, runnable from CI against a restored snapshot.

Scoping it out is a conscious trade: this ticket makes Mode A reliably visible and Mode B *partially* visible via consequence-alerting (Layer 1 refinement), which is a large improvement over nothing, without pretending to solve version-vs-schema divergence.

---

## Part 3 — Operational monitoring

### Alert policies

| # | Signal | Condition | Severity | Detects |
|---|---|---|---|---|
1 | Startup log marker | `[startup] alembic migration failed` present | sev-1, page | Mode A |
2 | Startup log level | The Layer 2 line emitted at `ERROR` | sev-1, page | Mode A + version-level B |
3 | Migration health | `status == "behind"` for > 5 min | sev-1, page | Mode A |
4 | Schema errors | `UndefinedColumn` / `UndefinedTable` / `DuplicateTable` in any log | sev-1, page | consequences of A and B |
5 | Migration health | `status == "unknown"` for > 15 min | sev-3, ticket | DB reachability |
6 | Migration health | `status == "ahead"` | sev-4, informational | rollback in progress |
7 | 5xx rate | `/holidays`, `/reports/*`, `/schedule/*` above baseline | sev-2 | schema mismatch surfacing to users |

Alerts 1 and 4 are console-only and can be created immediately, independent of any code.

### Dashboard

- `db_revision` vs `code_head`, as text, side by side.
- Time since the last successful migration.
- Count of startup-marker occurrences, last 24 h (should be flat zero).
- 5xx rate for the four schema-sensitive surfaces from the playbook.

### CI guard (extends what already exists)

`ci.yml` already has an **Alembic single-head check**. Extend that job rather than adding a new one:

- Assert every revision file declares a `down_revision` reachable from head — no orphans. (`tests/test_migration_baseline.py::test_every_revision_is_reachable_from_head` already does this; wire it into CI.)
- Assert each new revision defines a non-trivial `downgrade()`, or documents in its docstring why it is intentionally a no-op — the `c4a7e2b915d3` backfill is a legitimate example of the latter.
- **Assert every `op.create_table` is existence-guarded.** `a1b2c3d4e5f7` is the exact revision that would have taken production down, and its only defect was a missing guard. A lint rule would have caught it at review time.

That last check is the highest-value item in this ticket. It prevents the class of defect rather than detecting its consequences.

### Runbook linkage

On alert 1, 2, 3 or 4 firing, the responder goes to the playbook's Recovery A (migration partially applied) or Recovery B (new code on an old schema). No new runbook is needed; this ticket only adds the alerts that route there.

---

## Success criteria

1. A deliberately failed migration in a **staging or snapshot** environment pages within 5 minutes.
2. `GET /health/migrations` reports `behind` in that scenario and `ok` in steady state.
3. `GET /health` behaviour, latency and DB-independence are **byte-for-byte unchanged**.
4. `Dockerfile` CMD unchanged; `alembic upgrade heads || …` intact; startup never gated.
5. A migration containing an unguarded `op.create_table` fails CI.
6. No measurable added load: `/health/migrations` issues at most one query per 60 s per process.

Criteria 3 and 4 are regression guards on the constraints, and should be verified explicitly rather than assumed.

---

## Explicitly not in this ticket

- Changing, wrapping or replacing the startup migration command.
- Gating the container, the health check, or traffic migration on schema state.
- Auto-rollback on a detected mismatch.
- Object-level schema verification → **ZT-OPS-02**.
- Resolving the current 5-revision backlog → that is the playbook's job, executed separately.
- Anything in the Work Schedule Policies roadmap.

---

## Relationship to the Work Schedule roadmap

**None, deliberately.** The roadmap's next phase (P7) is not blocked by this ticket, and this ticket must not be scheduled inside it.

The playbook already handles the current deployment safely by hand: the stamp runs out of band before the code deploy, and Block C's *"startup logs contain no `alembic migration failed`"* is the manual equivalent of alert 1. This ticket replaces that manual check with an automatic one — valuable, but not a prerequisite.

**Recommendation:** ship Layer 1 (two console alert policies, zero code) before the Work Schedule deployment. It costs nothing, requires no release, and converts the playbook's most important manual check into an automatic one for that very deployment.

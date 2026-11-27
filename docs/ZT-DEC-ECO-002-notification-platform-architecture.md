# ECO V2 Notification Platform Architecture Decision

## Status

**ARCHITECTURE DECIDED — IMPLEMENTATION NOT YET AUTHORIZED**

Recorded 2026-09-22. Two architecture decisions, supplied by the owner and
reproduced verbatim below.

This is an **architecture decision record only**. It does not authorize
implementation, deployment or rollout. See *Implementation Authorization*.

---

## Context

ECO v2 Stages 1–5 are complete. Stage 6 (notification handoff) is BLOCKED
because this repository contains no central notification service boundary
satisfying §34 / D-5.1.

The notification **contract** was closed on 2026-09-22 and is recorded in
[`ZT-DEC-ECO-002-notification-platform-owner-rulings.md`](ZT-DEC-ECO-002-notification-platform-owner-rulings.md)
(20,075 bytes; four rulings, each APPROVED — A). Verified intact before this
record was written.

Two questions remained open after that document, and the implementation-
readiness audit determined that neither was answerable from repository
evidence. They are the subject of this record.

Current golden state: **25 executable / 3 blocked** (GV-07 `ALERT-DELIVERY`,
GV-18 `ALERT`, GV-26 platform incident).

### Ruling identifier namespaces

Three namespaces are now in play and must not be conflated:

| Prefix | Document | Subject |
|---|---|---|
| `D-<n>.<m>` | `ZT-DEC-ECO-002-owner-rulings.md` | The main ECO ruling set (D-1.1, D-3.1, D-5.1, D-5.5, …) |
| `D-1` … `D-4` | `ZT-DEC-ECO-002-notification-platform-owner-rulings.md` | The four notification platform rulings |
| **`D-A`, `D-B`** | **this document** | **The two architecture decisions** |

Nothing here renumbers, supersedes or amends any ruling in either other
document.

---

## Decision A — Service Boundary

### Owner ruling

**D-A: A**

### Selected option

> **A — Keep notification dispatch as an internal platform module within the
> existing FastAPI application/process.**

### Rationale

Recorded verbatim as supplied:

> Keep the notification dispatch boundary inside the existing Temveris
> application for the current ECO V2 implementation. This satisfies the
> synchronous §34 handoff contract while avoiding introducing a separately
> deployable service, service authentication, distributed tracing, or other
> infrastructure that the repository does not currently provide. The module
> should still have a strict platform-owned boundary so ECO owns the
> event/handoff and the platform owns channel selection, preference
> resolution, suppression, retry, transport, and dispatch outcome.

### Alternatives considered

| Option | Outcome |
|---|---|
| **A — internal platform module** | **SELECTED** |
| B — separate service boundary, same deployment | Viable; not selected |
| C — separate internal service | Ruled out by the readiness audit: requires service authentication and tracing infrastructure that do not exist |
| D — existing platform service outside this repository | Ruled out: no such service exists in this repository, its siblings, or its deployment configuration |
| E — message/event-based asynchronous handoff | **Incompatible.** D-2, D-3 and D-4 all require `dispatch()` to return `outcome`, `channel`, `reason_code` and `correlation_id` **to the caller**. An asynchronous handoff returns none of them at call time, and D-5.6's record identity includes a channel that would not yet exist |

Supporting repository evidence gathered during the readiness audit:

* `docker-compose.yml` defines **one** service, `app` — backend and frontend in
  a single image. There is no second deployable.
* `infrastructure/` contains only `.gitkeep`.
* The root `package.json` declares no workspaces.
* `redis` and `celery` are declared in `requirements.txt` under "OPTIONAL"
  headers and are **never imported anywhere** — no broker exists in practice.
* No tracing or correlation library is present (no OpenTelemetry, Sentry,
  Jaeger, ddtrace, elastic-apm).
* Authentication is user-facing JWT only; there is no service-to-service
  mechanism.

### §34 compatibility

**Compatible.** §34 states that *"Delivery is handled by the central Temveris
notification service. This control hands off; it does not send. Channel
selection, preference resolution, retry and suppression are that service's
concern and MUST NOT be reimplemented here."*

§34 separates **responsibility**, not processes. It requires that ECO does not
own channel selection, preference resolution, retry or suppression; it does not
require that the owner of those concerns run in a different process. The
selected option keeps every one of those responsibilities outside ECO, inside a
platform-owned module with a strict boundary.

The owner's rationale makes that explicit: *"The module should still have a
strict platform-owned boundary."* The boundary is an architectural obligation
of this decision, not an optional refinement.

### D-5.1 compatibility

**Compatible.** D-5.1 requires that ECO hands off to the central notification
service and records the dispatch outcome, and that ECO does **not** select
channels, resolve preferences, retry, or suppress. An in-process module
satisfies all of it, provided the boundary is enforced rather than assumed.

The readiness audit noted that this repository already has a precedent for
enforcing such a boundary without a process split: `app.eco_view_authority`
ships as a self-contained primitive whose isolation is pinned by AST import
guards in its test suite. The same technique applies here.

### Operational consequences

* **No new deployable.** `docker-compose.yml` is unchanged; the single `app`
  service continues to be the only one.
* **No service authentication required.** The SERVICE AUTHENTICATION GAP that
  options C, D and E would have opened does not arise.
* **No distributed tracing required** for P0. `correlation_id` is generated and
  persisted by the platform module; integration with a future tracing system is
  P1.
* **Tenant isolation is enforced by ordinary parameter validation.** The module
  must verify that `recipient_user_id` belongs to `organization_id` — the same
  check `app/recipient_authority/service.py` already performs.
* **In-process failure semantics.** A platform fault surfaces as an exception
  rather than a network error. The ECO failure model must treat it as a
  `failed` dispatch outcome and must never let it corrupt the ECO record, per
  §34's *"the record is authoritative; delivery is best-effort."*
* **Future extraction stays open.** Because the boundary is explicit, moving
  the module behind a network boundary later is a deployment change rather
  than a redesign. Nothing in this decision forecloses option B or C later.

---

## Decision B — Administrator Operational Notification

### Owner ruling

**D-B: A**

### Selected option

> **A — Create a distinct non-suppressible notification class for ECO
> administrator/operational notifications.**

### Rationale

Recorded verbatim as supplied:

> The existing OPERATIONAL class is suppressible and quiet-hours eligible,
> while §33.1 requires PRIMARY ADMIN notifications to be "Always
> (operational)". A distinct non-suppressible class preserves the locked
> "Always" requirement without introducing an ECO-specific bypass into the
> existing suppressible OPERATIONAL semantics. The new class must remain
> platform-owned and must not allow ECO to bypass preference, suppression, or
> transport rules for other notification types.

### Alternatives considered

| Option | Outcome |
|---|---|
| **A — distinct non-suppressible class** | **SELECTED** |
| B — keep `OPERATIONAL`, add a platform-owned exemption for the ECO administrator notification type | Viable; not selected |

### The finding that forced the decision

`app/notifications/classification.py`:

```
line 41   OPERATIONAL = "operational"   # workforce / attendance / timesheet routine
line 51   SUPPRESSIBLE = {OPERATIONAL, CUSTOMER_SUCCESS, MARKETING}
line 59   QUIET_HOURS_ELIGIBLE = SUPPRESSIBLE
```

The repository's existing `OPERATIONAL` class is **suppressible and quiet-hours
eligible**. §33.1's "Requirement unresolved" row marks PRIMARY ADMIN
*"Always (operational)"*. The two meanings of "operational" are opposites, and
reusing the existing class would make a notification the specification calls
"Always" droppable by a quiet-hours window or a posture setting.

A nuance recorded for precision: because `evaluate_send` resolves the
**recipient's** preferences, and the recipient of an administrator alert is the
administrator, the hazard is not that a Worker's preferences suppress an admin
alert. It is that an admin alert classified `OPERATIONAL` is **suppressible at
all**, which contradicts "Always".

### §33.1 compatibility

**Compatible, and the word "Always" is not reinterpreted.** §33.1's language is
untouched. The decision changes how the platform honours it: a class that is
structurally outside `SUPPRESSIBLE` and outside `QUIET_HOURS_ELIGIBLE` cannot
be dropped by preference policy, which is what "Always" requires.

Selecting A rather than B means the guarantee is expressed in the **class
vocabulary** rather than in a per-type conditional. A class that is not a member
of `SUPPRESSIBLE` cannot be suppressed by construction; an exemption is a branch
that a future edit can remove.

### GV-18 impact

GV-18's locked contract is:

```
state               = REQUIREMENT_UNRESOLVED
administrator_alert = true
shown_to_worker     = false
shortfall_computed  = false
```

This decision removes one of the two blockers the readiness audit identified
for GV-18 — the operational-class collision. It does **not** make GV-18
executable. The following remain, and none is authorized by this document:

1. The platform itself does not exist (Decision A settles its shape, not its
   existence).
2. **NOTIF-A-01 (§33.3)** is absent from `app/early_clock_out/copy_register.py`.
3. `AUTHORIZED_AUDIENCES` in `app/alert/outcomes.py` is
   `frozenset({"WORKER"})` and must be widened.
4. `app/alert/rendering.py` refuses every audience other than WORKER and needs
   administrator rendering.
5. Where a tenant has no PRIMARY ADMIN designation, **D-1** governs: record
   `RECIPIENT_UNRESOLVED` and continue. Unchanged by this decision.

**GV-18 remains blocked on `ALERT`.**

### Suppression semantics

The new class changes **nothing** about D-2's ordering, which remains:

```
channel selection  ->  preference / suppression evaluation  ->  DispatchResult
```

A notification in the new class still passes through the same sequence; what
differs is that suppression policy cannot select it for suppression, because it
is not a member of `SUPPRESSIBLE`.

Three constraints from the owner's rationale are binding:

1. The new class is **platform-owned**. ECO does not define it, does not
   validate it and cannot extend it.
2. ECO **must not** be able to use it to bypass preference, suppression or
   transport rules for any other notification type.
3. The existing `OPERATIONAL` semantics are **unchanged**. No existing family
   moves class, and no existing notification logic was modified in recording
   this decision.

Worker-facing ECO notifications are unaffected by this decision and are not
placed in the new class.

---

## Locked Contract

Unchanged by this record. **D-1 through D-4 remain closed and were not
reopened.**

### D-1 — PRIMARY ADMIN unresolved

PRIMARY ADMIN unresolved → record `RECIPIENT_UNRESOLVED` and continue; no
recipient substitution.

### D-2 — Suppression

Channel selection → preference/suppression → `DispatchResult`. Suppression is
represented as `outcome = dispatched` plus a platform-owned suppression
`reason_code`.

### D-3 — Correlation ID

One platform-generated `correlation_id` per handoff, stable across platform
retries.

### D-4 — Idempotency

Platform request idempotency key: `(eco_event_id, recipient_user_id)`.
ECO delivery record identity: `(threshold_event_id, recipient_id, channel)`.
These are different identities and must not be collapsed.

### Ownership boundary

| ECO owns | Platform owns |
|---|---|
| event identity | channel selection |
| recipient authority | preference resolution |
| recipient snapshot | suppression |
| content inputs | retry |
| rollout | transport |
| ECO delivery record | dispatch |
| | `DispatchResult` |
| | `correlation_id` |
| | notification-platform idempotency |

No dual ownership. Decision A places the platform side **in-process**; it does
not move any responsibility across this line.

### SHADOW

**ECO must not call `dispatch()` while in SHADOW.**

Decision A does not weaken this. An in-process module is *easier* to call by
accident than a network service, so the SHADOW gate remains ECO's
responsibility and must be enforced before the call site is reached. A
platform-side rejection of a SHADOW handoff may exist **only as defence in
depth** and must never become the only protection.

---

## P0 Consequences

The two decisions narrow the P0 scope already recorded in the rulings document.
Unblocked, unchanged, or newly settled:

| P0 item | Effect of these decisions |
|---|---|
| `dispatch(handoff) -> DispatchResult` | Shape settled: an in-process module entry point |
| Channel selection | Platform-owned; may select from a set of one |
| At least one transport channel | Email is the only channel with a working transport |
| Operational notification class | **Settled by D-B: a distinct non-suppressible class** |
| Suppression after channel selection | Unchanged (D-2) |
| PII-free reason codes | Unchanged (D-5.5); vocabulary still to be designed |
| Stable per-handoff correlation ID | Unchanged (D-3) |
| Pre-channel idempotency | Unchanged (D-4) |
| Independent recipient dispatch | Unchanged |
| Platform-controlled retry | Unchanged |
| No provider details crossing the boundary | Unchanged |
| **Service authentication** | **No longer required** — removed from P0 by D-A |
| **Distributed tracing** | **No longer required** for P0 — deferred to P1 by D-A |

Remaining P0 design work, unaffected by these two decisions and **not**
authorized here: the reason-code vocabulary; the `correlation_id` format and
maximum length; `in_progress` semantics for a concurrent duplicate; whether a
`failed` idempotency key is re-dispatchable; the lease/timeout for an orphaned
`in_progress` row after restart.

---

## Deferred P1 / P2 Work

**P1** — additional channels (SMS, push, in-app) · platform-side
delivery-attempt history · per-tenant channel policy · tracing integration ·
delivery webhooks.

**P2** — receipt/open tracking · digesting and batching · templating service ·
migrating the existing senders (`app/attendance_alerts.py`,
`app/commercial/notifications.py`) onto the boundary.

Extraction of the platform module into a separate service or deployable is
**P2 and explicitly left open** by Decision A. Because the boundary is
explicit, that extraction is a deployment change rather than a redesign.

---

## Implementation Authorization

**THIS DOCUMENT AUTHORIZES NOTHING BEYOND THE RECORDED DECISIONS.**

Explicitly:

* This is an **architecture decision record only**.
* It does **NOT** authorize implementation.
* It does **NOT** authorize deployment.
* It does **NOT** authorize rollout. Rollout `early_clock_out_v2` remains
  **OFF**.
* **ECO v1 remains permanently disabled**
  (`EARLY_CLOCK_OUT_FEATURE_ENABLED = False`).
* **GV-07, GV-18 and GV-26 remain BLOCKED.** No golden-vector promotion is
  authorized by this document.

Not authorized, and not performed in recording this decision: building the
notification service · creating `dispatch()` · creating the idempotency table ·
creating reason codes · modifying email transport · adding the operational
notification class · adding exemption logic · modifying the ECO delivery
schema · modifying alert rendering · registering NOTIF-A-01 · widening
`AUTHORIZED_AUDIENCES` · promoting any golden vector.

No part of ZT-SPEC-ECO-002 was reinterpreted or amended. No existing owner
ruling was changed. No additional owner decision was invented.

Each of the above requires a **separate, explicit implementation
authorization**.

---

## Approval / Effective Date

**Decisions supplied and approved by the owner. Recorded 2026-09-22.**

Effective as the governing architecture for ECO Stage 6 from this date.

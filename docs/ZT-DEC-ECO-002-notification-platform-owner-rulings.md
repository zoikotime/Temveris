# ECO V2 Notification Platform Owner Rulings

## Status

**OWNER RULINGS RECORDED — IMPLEMENTATION NOT YET AUTHORIZED**

Recorded 2026-09-22. Four rulings, supplied by the owner and reproduced
verbatim below. This document establishes the contract only. No code, schema,
migration, golden-vector metadata or rollout state was changed to record it,
and implementation requires a separate authorization.

---

## Context

ECO v2 Stages 1–5 are complete (scheduled-end evidence, surplus caps and
retroactive horizon storage, netting fold and bank credit, retroactive horizon
evaluation, recipient authority).

**Stage 6 is BLOCKED.** The repository contains no central Temveris
notification service boundary satisfying §34 / D-5.1. No callable accepts a
notification handoff and returns an outcome, the channel used, a service reason
code and a correlation identifier. Every existing notification path terminates
in `app/email_service.py`, a single-channel mailer that returns `bool` and
conflates "suppressed" with "sent".

Current golden state: **25 executable / 3 blocked.**

| Vector | Blocking dependency |
|---|---|
| GV-07 | `ALERT-DELIVERY` |
| GV-18 | `ALERT` |
| GV-26 | platform incident (Stage 7) |

### A note on ruling identifiers

The rulings below were supplied labelled **D-1 … D-4**. Those labels are
recorded verbatim, as supplied.

Readers must not confuse them with the identically-numbered families in
`ZT-DEC-ECO-002-owner-rulings.md`, which uses a `D-<n>.<m>` scheme covering
entirely different decisions — `D-1.1` (historical scheduled-end authority),
`D-2.1` (system scheduled-end source), `D-3.1` (administrator / reviewer
authority model), `D-3.7` (deactivated reviewer after event snapshot), `D-5.1`
(notification handoff model), `D-5.5` (delivery failure-detail and PII policy).

**Within this document, an unqualified `D-1`…`D-4` means a notification
platform ruling. A reference of the form `D-<n>.<m>` always means the earlier
document.** Nothing here renumbers, supersedes or amends any ruling in that
document.

---

## Decision 1 — PRIMARY ADMIN unresolved

### Owner ruling

**APPROVED — A**

> If PRIMARY ADMIN cannot be resolved:
>
> - record `RECIPIENT_UNRESOLVED`
> - continue evaluation/event processing
> - do not fabricate or substitute a recipient
> - do not fall back to Worker
> - do not fall back to HR ADMIN
> - do not fall back to DIRECT MANAGER
> - resolved audiences may continue independently
> - the unresolved audience must remain explicitly represented in the ECO record
>
> This does not block calculation or threshold-event creation.

### Exact behaviour

An audience that cannot be resolved produces an explicit, recorded
`RECIPIENT_UNRESOLVED` result for that audience alone. Every other audience
that did resolve is dispatched independently and normally. Day and week
evaluation, threshold comparison and threshold-event creation are unaffected.

### Consequences

* **Nothing is fabricated.** The four substitutions named in the ruling are
  forbidden, which closes the same door Stage 5 already closed at resolution
  time: `app.recipient_authority` returns `UNRESOLVED / NOT_DESIGNATED` with a
  stated basis rather than a guessed identity.
* **The vocabulary already exists.** `RECIPIENT_UNRESOLVED` is an existing
  `RefusalReason` in `app/alert/outcomes.py` — *"the event's frozen recipient
  snapshot does not name a usable identity. Fail closed: no substitute
  recipient is ever chosen."* Implementation reuses it rather than minting a
  parallel term. Whether the existing member is reused as-is or a delivery-side
  analogue is added is an implementation decision, not a ruling.
* **Consistent with §34.** *"The record is authoritative; delivery is
  best-effort."* A missing recipient designation degrades delivery; it does not
  invalidate the record.
* **The specification is silent on this case.** §33.1 marks PRIMARY ADMIN
  "Always" but governs only who is notified when the audience exists; it never
  addresses a tenant that has designated nobody. This ruling fills a genuine
  gap and contradicts no clause.

### GV-18 impact

GV-18's locked `administrator_alert = true` concerns ECO **raising** the
administrator alert, not delivery succeeding. Under this ruling a tenant with
no PRIMARY ADMIN designation still raises the alert and records
`RECIPIENT_UNRESOLVED` against it.

Whether that satisfies GV-18's locked field for promotion purposes is an
**implementation-time question deliberately left open here**. Note that any
GV-18 execution harness demonstrating a delivered administrator alert will need
a designated PRIMARY ADMIN in its fixture.

---

## Decision 2 — Suppression

### Owner ruling

**APPROVED — A**

> Suppression is represented as:
>
> `outcome = dispatched`
>
> with an enumerated platform-owned suppression `reason_code`.

### Suppression timing

> Suppression occurs: **AFTER CHANNEL SELECTION.**
>
> Therefore:
>
> 1. platform selects the channel
> 2. platform evaluates preference/suppression policy
> 3. if suppressed, platform returns:
>    - outcome = dispatched
>    - selected channel
>    - suppression reason_code
>    - correlation_id
> 4. ECO records the returned result
>
> The suppression reason must remain distinguishable from actual transmission
> in the reason_code.
>
> Do NOT add a third ECO outcome.
> Do NOT modify the locked two-value ECO outcome vocabulary.

### Outcome semantics

The ECO outcome vocabulary remains exactly `dispatched | failed`. Suppression
is not a third state; it is a `dispatched` outcome whose `reason_code`
identifies it as suppressed rather than transmitted.

**Derived requirement, load-bearing.** §34 records delivery state *"so that a
Tenant asserting 'the Worker was informed' can be checked against what actually
happened."* Under this ruling the `outcome` column **alone cannot answer that
question** — a suppressed notification and a transmitted one both read
`dispatched`. The ruling's own clause ("must remain distinguishable … in the
reason_code") is therefore what preserves §34's checkability, and it follows
that:

> Any surface answering "was this recipient informed?" MUST read `outcome`
> **and** `reason_code`. Reading `outcome` alone overstates delivery.

This is a consequence of the ruling, recorded so it is not rediscovered later.
It is not an amendment and it does not reopen the decision.

### D-5.5 impact

None. D-5.5 governs the content of the transport diagnostic — service reason
code, correlation/request identifier, ≤512 characters, no Worker
name/contact/department, no shortfall or workweek figures, no notification body
text, NULL permitted when the service supplies no diagnostic. A suppression
reason code is a service reason code and sits inside that bound unchanged.

### D-5.6 impact

**Positive, and it resolves a previously-open risk.** The gap audit flagged
that if suppression occurred *before* channel selection there would be no
channel, and the ECO record identity `(threshold_event_id, recipient_id,
channel)` could not be formed for a suppressed handoff.

By ruling that suppression occurs **after** channel selection and that the
selected channel is returned in the suppressed result, a channel always exists.
The three-part ECO record identity is always formable. **D-5.6 is unchanged and
is no longer at risk.**

---

## Decision 3 — Correlation ID

### Owner ruling

**APPROVED — A**

### Scope

> - one correlation ID per notification handoff
> - stable across platform-internal retries
> - platform-generated
> - not a provider attempt ID
> - provider IDs must NOT cross the ECO/platform boundary
> - ECO stores the platform correlation ID returned for the handoff

### Retry stability

One identifier covers the whole handoff and does not change when the platform
retries internally. ECO never observes individual transport attempts — the
platform owns retry — so the stored value identifies something ECO can reason
about.

### Storage contract

> - correlation_id is required on a completed dispatch result
> - NULL is permitted only where the DispatchResult contract explicitly permits it
> - combined `reason_code + correlation_id` diagnostic must remain <=512 characters
> - reason_code is platform-owned and enumerated
> - reason_code must be PII-free
> - provider error text must never cross the boundary
>
> Do not invent a provider-ID mapping.

**Derived consequence, forced by D-3 and D-5.5 read together — not a choice.**
D-3 requires `correlation_id` on *every* completed dispatch result, including a
successful one. D-5.5 permits the diagnostic to be NULL when the service
supplies none, which is the normal case on success. Both can hold only if
`correlation_id` is stored **in its own column**, separate from
`failure_detail`.

> Composing `correlation_id` into `failure_detail` is therefore **not
> authorized**: it would make `failure_detail` non-NULL on every success and
> contradict D-5.5.

The ≤512 bound in D-3 governs the combined diagnostic **content**, regardless of
how many columns carry it.

### D-5.5 impact

Reinforcing. D-5.5 already required a service reason code and a
correlation/request identifier and forbade Worker PII. D-3 makes the guarantee
**structural** rather than aspirational: an enumerated, platform-owned reason
code cannot carry a name or an address, whereas free provider text can. The
prohibition on provider error text and provider IDs crossing the boundary is
what removes the need for ECO-side PII scrubbing entirely.

---

## Decision 4 — Idempotency

### Owner ruling

**APPROVED — A**

### Request key

> The central notification platform accepts a pre-channel request idempotency
> key based on:
>
> `(eco_event_id, recipient_user_id)`

### Replay guarantee

> The platform guarantees that repeated submission of the same logical handoff:
>
> - produces one logical dispatch
> - returns the same selected channel
> - returns the same correlation_id
> - returns the original logical result
> - does not create a duplicate dispatch

### Channel stability

The same logical handoff always returns the same selected channel. This is what
makes the ECO record identity stable across replay: a resubmission after a lost
response cannot land under a different channel and mint a second,
non-conflicting ECO record.

### D-5.6 relationship

> The distinction is mandatory:
>
> PLATFORM REQUEST IDENTITY: `(eco_event_id, recipient_user_id)`
>
> ECO DELIVERY RECORD IDENTITY: `(threshold_event_id, recipient_id, channel)`
>
> These are different identities and must not be collapsed.
>
> The ECO three-part record identity remains locked and unchanged.

A request key and a record key are different objects. **D-5.6 requires no
amendment**, and the apparent circularity — ECO cannot know the channel before
dispatch, yet the record identity includes it — is resolved: the channel is
known by the time the *record* is written, which is the only point D-5.6
governs.

**Traceability note.** D-5.6 is cited as locked but is not itself recorded in
`ZT-DEC-ECO-002-owner-rulings.md`; it reaches this programme through the Stage 6
authorization. Recorded as an observation, not a challenge to its authority.

---

## Locked NotificationHandoff Contract

`dispatch(handoff: NotificationHandoff) -> DispatchResult`

| Field | Required | Supplied by | Immutable | Notes |
|---|---|---|---|---|
| `organization_id` | Yes | ECO | Yes | Tenant scoping |
| `eco_event_id` | Yes | ECO | Yes | Half the D-4 request key |
| `recipient_user_id` | Yes | ECO | Yes | **Identity, never contact** |
| `audience` | Yes | ECO | Yes | One of the four §33.1 audiences |
| `notification_type` | Yes | ECO | Yes | `THRESHOLD_REACHED` / `THRESHOLD_WITHDRAWN` |
| `content_inputs` | Yes | ECO | Yes | Locked §31 copy keys and values |
| `recipient_snapshot` | Yes | ECO | **Permanently** (D-3.7) | Who was authoritative at event time |
| `idempotency_key` | Yes | ECO | Yes | Per Decision 4 |
| `mode` | Yes | ECO | Yes | Defence in depth only — see SHADOW |

### ECO does NOT provide

* selected channel
* email address or any Worker contact detail
* provider details or credentials
* preference result
* suppression result
* retry instructions
* channel-specific transport body

**Content vs diagnostic — a distinction the contract must hold.** §33.3's
NOTIF-A-01 legitimately names the Worker (*"Jordan Smith has reached 34
minutes…"*), bounded by §33.4 (no diagnosis, no leave-reason detail, no Worker
free text, no other Worker's records). That PII travels in `content_inputs`.
D-5.5's prohibition governs the **diagnostic**, not the content: the body may
name the Worker; `failure_detail` and `reason_code` may not.

---

## Locked DispatchResult Contract

| Field | Contract |
|---|---|
| `outcome` | `dispatched` \| `failed`. Two values, locked. No third value. |
| `channel` | Platform-selected, returned verbatim. Vocabulary owned entirely by the platform; ECO defines and validates none. |
| `reason_code` | Platform-owned, enumerated, PII-free. |
| `correlation_id` | Platform-generated, one per handoff, stable across platform-internal retries. |
| Combined diagnostic | `reason_code + correlation_id` ≤ **512 characters**. |
| Provider error text | **Prohibited** from crossing the boundary. |
| Provider IDs | **Prohibited** from crossing the boundary. |

### Suppression

1. Channel is selected **first**.
2. Suppression policy is evaluated **afterward**.
3. A suppressed result returns `outcome = dispatched`, the **selected channel**,
   a suppression `reason_code`, and a `correlation_id`.

---

## SHADOW Contract

| Mode | ECO | Platform |
|---|---|---|
| **OFF** | No operational ECO notification processing. `dispatch()` not called. | Never invoked |
| **SHADOW** | ECO may evaluate and record. **`dispatch()` MUST NOT be called.** Not "call and discard". | Never invoked. No shadow send, no transport, no preference-resolution side effect, no platform audit entry implying dispatch. |
| **ENABLED** | Dispatch permitted | Normal |

**ECO is solely responsible for preventing the call.** A platform-side
rejection of a handoff carrying SHADOW mode may exist **only as defence in
depth** and must never become ECO's only protection. A platform rejection on
those grounds must not be recorded by ECO as `failed`, because no delivery was
attempted or intended.

---

## ECO vs Platform Responsibility

No dual ownership.

| ECO | Platform |
|---|---|
| Event identity | Preference resolution |
| Recipient authority | Channel selection |
| Recipient snapshot | Suppression |
| Content inputs | Retry |
| Rollout mode | Transport |
| ECO delivery record | Dispatch |
| ECO record identity `(threshold_event_id, recipient_id, channel)` | Platform request idempotency `(eco_event_id, recipient_user_id)` |
| SHADOW enforcement (authoritative) | `reason_code` |
| | `correlation_id` |
| | SHADOW rejection (defence in depth only) |

### ECO MUST NOT

select a channel · resolve preferences · implement suppression · implement
retry · transport notifications · wrap `app/email_service.py` as the central
platform.

---

## GV-07 Platform Requirements

* Frozen recipient snapshot is the source of recipients.
* Multiple recipients per event.
* Independent per-recipient results.
* A returned channel **per recipient** — different recipients may resolve to
  different channels, and each ECO record stores its own.
* Platform request idempotency per Decision 4.
* The withdrawal is a **distinct ECO event** from the crossing it closes, so it
  never collides with it on the request key.
* The withdrawal uses the **original** recipient snapshot.
* **No current-authority re-resolution**, ever.

Already proven on the ECO side under Stage 5 and requiring nothing further from
the platform: the original event is retained; the withdrawal copies the original
snapshot verbatim; current designations are never consulted for a historical
event.

GV-07's locked `expected_verbatim` reads *"both recipients notified"*, so any
execution harness for it will need a crossing with two resolved recipients.

---

## GV-18 Platform Requirements

* Non-Worker audiences supported.
* **Operational** notification class, so worker-level preference suppression
  cannot silently drop an administrator alert. §33.1's row for "Requirement
  unresolved" reads WORKER *Never* / PRIMARY ADMIN *Always (operational)* /
  DIRECT MANAGER *No* / HR ADMIN *No*.
* No Worker fallback (`shown_to_worker = false`).
* No recipient inference.
* PRIMARY ADMIN unresolved follows **Decision 1**.

### Known ECO-side prerequisites — NOT implemented, NOT authorized here

1. Register **NOTIF-A-01** (§33.3) in `app/early_clock_out/copy_register.py`.
   It exists in the specification and is currently absent from the register.
2. Widen `AUTHORIZED_AUDIENCES` in `app/alert/outcomes.py`, currently
   `frozenset({"WORKER"})`.
3. Add administrator rendering in `app/alert/rendering.py`, which currently
   refuses every audience other than WORKER.

None of the three was implemented in recording these rulings, and GV-18 was not
promoted.

---

## ECO Delivery Record Requirements

Deferred schema changes on `eco_notification_delivery_attempt`, **authorized in
contract but NOT authorized for implementation**. No migration was created.

| Current | Required | Why |
|---|---|---|
| `outcome VARCHAR(32)`, no CHECK | CHECK constraining to `dispatched \| failed` | Decision 2 keeps the vocabulary at two values |
| `channel VARCHAR(32) NOT NULL` | Remains compatible with post-channel-selection suppression | Decision 2 guarantees a channel always exists by the time the record is written, including for suppressed results |
| `failure_detail TEXT`, unbounded | CHECK `length ≤ 512` | D-5.5 |
| *(absent)* | `reason_code` | D-5.5, Decisions 2 and 3 |
| *(absent)* | `correlation_id`, **own column** | Decision 3; see the derived consequence recorded there |
| UNIQUE `(notification_id, attempt_seq)` | Uniqueness expressing `(threshold_event_id, recipient_id, channel)` | D-5.6 record identity is not expressed today |
| `destination VARCHAR(320)` | **Not populated** from the platform boundary | ECO never learns the address under the handoff model |
| Append-only triggers present | **Preserved unchanged** | Existing R5 guarantee |

---

## Platform P0 Contract

The minimum platform capability required to unblock Stage 6:

1. `dispatch(handoff) -> DispatchResult`
2. Channel selection
3. At least one actual transport channel
4. Operational notification class
5. Suppression **after** channel selection
6. PII-free reason codes
7. Stable per-handoff correlation ID
8. Pre-channel idempotency
9. Independent recipient dispatch
10. Platform-controlled retry
11. No provider details crossing the ECO boundary

P0 is deliberately satisfiable by a **single-channel** service. ECO needs the
shape of the boundary, not breadth behind it.

---

## Open Questions

Decisions 1–4 are closed and must not be reopened. What remains is platform
design and implementation, not ECO owner rulings:

1. Where will the central notification service live?
2. What service / API / process boundary will expose `dispatch()`?
3. Which first transport channel will satisfy P0?
4. How will the platform internally persist its own delivery attempts?
5. What authentication / authorization mechanism protects the ECO → platform call?
6. What operational monitoring and SLAs are required?
7. Do the existing senders (`app/attendance_alerts.py`,
   `app/commercial/notifications.py`) migrate onto the boundary, or does it
   serve ECO alone at P0?

---

## Approval / Effective Date

**Rulings supplied and approved by the owner. Recorded 2026-09-22.**

Effective as the governing contract for ECO Stage 6 from this date.

**Implementation is NOT authorized by this document.** Building the platform,
changing the ECO delivery schema, registering NOTIF-A-01, widening
`AUTHORIZED_AUDIENCES`, adding administrator rendering, or promoting GV-07 or
GV-18 each require a separate, explicit implementation authorization.
---

# Addendum — ECO integration rulings (recorded 2026-09-22)

## Status

**OWNER RULINGS RECORDED — IMPLEMENTATION NOT YET AUTHORIZED**

Two further rulings, supplied after the notification-platform foundation was
built and verified. They are recorded verbatim below.

These carry the labels **`D-C`** and **`D-E`** as supplied. Read the namespace
table in `ZT-DEC-ECO-002-notification-platform-architecture.md` alongside them:
`D-<n>.<m>` is the main ECO ruling set, `D-1`…`D-4` are this document's
notification-platform rulings, `D-A`/`D-B` are the architecture decisions, and
`D-C`/`D-E` are these. Nothing here renumbers or amends any of the others.

### Context at the time of ruling

Notification platform foundation implemented and verified: 65 focused tests
pass; full backend 6566 passed with the two known baseline failures; migration
head `f8c1d47a92e3`. Golden state **25 executable / 3 blocked** — GV-07 on
`ALERT-DELIVERY`, GV-18 on `ALERT`, GV-26 independently. Rollout OFF, ECO v1
disabled, `AUTHORIZED_AUDIENCES` WORKER-only, NOTIF-A-01 unregistered, and no
ECO path calls `dispatch()`.

---

## Decision D-C — ECO dispatch trigger

### Owner ruling

**APPROVED — B (persist-first asynchronous / sweep dispatch)**

### Rationale

Recorded verbatim as supplied:

> Persist the threshold event first and dispatch it through a separate
> ECO-owned processing step so event creation remains independent of
> notification transport. This provides durable recovery and replay after
> process failure while preserving the locked D-1 through D-4 ownership
> boundaries.

### Scheduling semantics — ruled, verbatim

> **Cadence:** on demand only for the initial implementation; no recurring
> background loop is authorized by this ruling.
>
> **Selection predicate:** select committed threshold events that have a frozen
> recipient snapshot and do not have a completed ECO delivery result for the
> corresponding recipient.
>
> **Batch bound:** 50 recipient handoffs per pass.
>
> **Execution:** do not add a new `run_forever` background loop in this phase;
> invoke the processing step only from an explicitly authorized ECO trigger.

### Alternatives considered

| Option | Outcome |
|---|---|
| A — inline at threshold-event recording | Viable; **not selected**. Would couple event creation to transport, so a transport fault shares a transaction boundary with an append-only record |
| **B — persist-first asynchronous / sweep** | **SELECTED** |

### Exact behaviour

Threshold-event creation completes and commits with **no dispatch**. A separate
ECO-owned processing step later selects committed events matching the ruled
predicate and hands each recipient to the platform, at most 50 recipient
handoffs per pass.

### Consequences

* **Event creation never depends on transport.** A dispatch failure cannot roll
  back, corrupt or partially rewrite the immutable threshold event — which is
  §34's *"the record is authoritative; delivery is best-effort"* expressed
  structurally rather than by careful transaction handling.
* **Recovery is free.** An event whose dispatch never completed is simply
  selected again on the next pass. No repair path is needed.
* **Replay is safe.** A repeated handoff is a no-op under D-4: the platform
  returns the original result, the same channel and the same correlation id,
  and transports nothing.
* **The predicate is derived, not stored.** "Does not have a completed ECO
  delivery result for the corresponding recipient" is answered by reading the
  ECO delivery record, so no new dispatch-pending flag or queue column is
  introduced — and therefore no second source of truth about what has been
  dispatched.
* **No background loop.** `main.py` runs several `asyncio.create_task(...
  run_forever(...))` workers; this ruling explicitly adds none. The processing
  step is callable and inert until an ECO trigger is separately authorized.
* **SHADOW is unchanged.** The step must exclude SHADOW-mode events before
  reaching the boundary. ECO remains solely responsible for not calling
  `dispatch()`; the platform-side refusal stays defence in depth.
* **Recipient authority is never re-resolved.** The step reads the frozen
  event-time snapshot (D-3.7), never current designations.

### GV-07 impact

This ruling authorizes the *shape* of the integration GV-07 needs; it does not
build it, and GV-07 is **not promoted**. Two things remain:

1. Implementation of the processing step, under a separate authorization.
2. An explicitly authorized ECO trigger to invoke it — the ruling withholds
   that deliberately.

Note for whoever builds the execution harness: GV-07's locked
`expected_verbatim` reads *"both recipients notified"*, so its fixture needs a
crossing with two resolved recipients — that is, a designated PRIMARY ADMIN.

### Locked rulings unaffected

D-1 through D-4 are unchanged. The platform continues to own channel selection,
preference resolution, suppression, retry, transport, `correlation_id` and
request idempotency; ECO continues to own event identity, recipient authority,
the recipient snapshot, content inputs, rollout and the ECO delivery record.
D-C settles only **when ECO makes the call**, which is squarely ECO's side of
that line.

---

## Decision D-E — NOTIF-A-01 registration

### Owner ruling

**APPROVED — B (do not authorize registration yet)**

### Rationale

Recorded verbatim as supplied:

> Do not authorize registration of the §33.3 administrator notification body
> until the exact authoritative copy is supplied. The existing source does not
> provide a sufficiently unambiguous template for safe reconstruction, and the
> locked-copy requirement prohibits paraphrasing.

### Exact behaviour

`app/early_clock_out/copy_register.py` is **unchanged**. No administrator
notification body key is added. No §33.3 text is transcribed, reconstructed,
paraphrased or inferred.

### Consequences

* **GV-18 remains blocked** on `ALERT`, and this ruling keeps it so.
* The register continues to hold `eco.admin.disclaimer` — the §29.1 / §33.4
  closing statement mandatory *"in every administrator notification, in the
  body, not in a footer"* — and no body for it to close.
* §31's constraint is honoured: these strings are *"legally load-bearing…
  MUST NOT be paraphrased, shortened for layout, or rewritten by a content pass
  without Legal review"*, and the register is the comparison target for AC-20,
  so a reconstructed template would corrupt the check it exists to serve.

### What is still required to unblock GV-18

1. **The exact authoritative §33.3 body**, verbatim, with its original
   typography (U+2019 apostrophes, U+2014 em dashes) — plus the key name and
   its `OWNER_AUTHORIZED_SURFACES` mapping, and confirmation of how
   `eco.admin.disclaimer` composes into it.
2. Widening `AUTHORIZED_AUDIENCES`, currently `frozenset({"WORKER"})`.
3. Administrator rendering in `app/alert/rendering.py`, which today refuses
   every audience other than WORKER.
4. The D-C processing step, since an administrator alert that is never
   dispatched is not an alert.

D-B already discharged the fifth prerequisite — the operational-class collision
— by creating a distinct non-suppressible class.

---

## Addendum approval / effective date

**Rulings supplied and approved by the owner. Recorded 2026-09-22.**

**Implementation is NOT authorized by this addendum.** Building the D-C
processing step, adding an ECO trigger, registering NOTIF-A-01, widening
`AUTHORIZED_AUDIENCES`, adding administrator rendering, and promoting GV-07 or
GV-18 each require a separate, explicit implementation authorization.

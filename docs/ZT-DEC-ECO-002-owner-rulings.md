# ZT-DEC-ECO-002 — Early Clock-Out v2.0: approved owner rulings

**Type:** Decision record. Authoritative for implementation; subordinate to the specification.
**Status:** **APPROVED 2026-09-21.** Contract frozen. **No implementation performed under this record.**
**Scope boundary:** Governs ZT-SPEC-ECO-002 v2.0 only. Supersedes decision fields D-1 through D-9 of the owner-ruling intake, which are hereby CLOSED. Does not amend the specification.
**Origin:** Owner-ruling gate opened after the remaining-blocker readiness audit of the seven still-blocked golden vectors (GV-01, GV-07, GV-18, GV-19, GV-20, GV-23, GV-26).
**Baseline at approval:** branch `ashrafv1`, HEAD `9fb5bd1`, golden artifact `1fe74f2c38dda165ea67e1004e9e6fbb1162c7bb`, migration head `e2b6d4f89c35`, 21 executable / 7 blocked, rollout `early_clock_out_v2` OFF, ECO v1 disabled.

---

## How to read this record

Two kinds of statement appear here and they are **not** interchangeable.

* **SPEC-LOCKED** — determined by ZT-SPEC-ECO-002 v2.0. Not an owner decision, not reopenable by a later owner ruling, and any implementation contradicting one is a defect.
* **OWNER RULING** — a decision the specification leaves open, made by the project owner on 2026-09-21 and recorded here verbatim.

Where a decision has both components they are separated explicitly. No ruling below may be read as weakening a SPEC-LOCKED rule.

---

## 1. Spec-locked rules that must not be reopened

### 1.1 §18 — canonical evaluation ordering

```
q      := roundMinutes(Q, policy.rounding_profile)
bank   := bankCredit(worker, work_date, policy.netting_mode)
gross  := MAX(0, req.minutes - q - bank)
exclusions apply to gross
attributable := remaining
IF attributable <= req.grace_minutes:
    attributable := 0
variance := actual_end_local - scheduled_end_local
```

Therefore: bank is consumed **before** gross; exclusions apply **after** bank consumption; grace applies **after** exclusions; netting MUST NOT be moved onto `attributable`; grace MUST NOT return credit to the bank. Per §16 / §19.1, step 9 is the only rounding site.

### 1.2 §15.1 / INV-3 — the bank is a fold

The bank balance is a fold over immutable day records. There is **no stored bank counter and no bank balance table.** Banked surplus MUST derive from the same ledger as shortfall; manually entered surplus MUST NOT exist. Surplus MUST be capped per day **and** per period, per Tenant configuration.

### 1.3 §34 — delivery authority separation

ECO does not implement notification transport. No channel selection, preference resolution, retry policy, suppression, or email/SMS/push transport is added here. ECO hands off to the central Temveris notification service and records the outcome. The record is authoritative; delivery is best-effort.

### 1.4 §24.15 — integrity faults refuse

A ledger integrity fault causes refusal to evaluate. Nothing is repaired, clamped, skipped or fabricated.

### 1.5 §33.1 — audience semantics

WORKER: always · PRIMARY_ADMIN: always · DIRECT_MANAGER: if configured · HR_ADMIN: if configured. Withdrawal goes to all original recipients.

### 1.6 No fabricated history

Completed historical records are not backfilled merely to populate newly introduced fields. Precedent: migration `f1a6b9c4d283`, which declined to synthesise leave intervals for pre-existing requests.

---

## 2. Approved owner rulings

Each ruling is recorded verbatim as approved on **2026-09-21**.

### D-1.1 — Historical scheduled-end authority (§6.2, §10, §18 step 14)

> A — schedule resolver end_local, frozen on WorkDayLedger at seal, source tier recorded verbatim.

**Owner ruling.** The specification defines the variance quantity but names no source for the scheduled end; the owner selected the existing schedule resolver, frozen at seal under the D-18D pattern already used for `is_working_day` / `working_day_source`.

### D-2.1 — System scheduled-end source (§10, §35)

> B — source="system" is admissible for sealing; scheduled end NULL; variance unavailable and never zero.

**Owner ruling.** Grounded in §10 (*"Recorded; never drives the threshold"*) and §35 (*"signed, never used in comparisons"*): a field that drives nothing does not justify refusing to seal a day. D6-A's fail-closed rule remains scoped to `is_working_day` and is **not** extended by this ruling.

### D-3.1 — Administrator / reviewer authority model (§33.1)

> Replace the original A/B/C options with §33.1's four audiences:
> - WORKER
> - PRIMARY_ADMIN — role-based, always
> - DIRECT_MANAGER — explicit per-org designation, if configured
> - HR_ADMIN — explicit per-org designation, if configured

**Spec-locked component:** the four audiences and their always / if-configured semantics (§33.1).
**Owner ruling component:** authorising the contract amendment — the original A/B/C options could not express the spec (A is designation-only and cannot express *always*; B is role-only and cannot express *if configured*), so they are struck. Both mechanisms exist; neither replaces the other. The existing `app/notifications/recipients.py` cascade is **not** an ECO recipient authority: it resolves email addresses rather than recipient identities and takes no snapshot.

### D-3.7 — Deactivated reviewer after event snapshot (§33.1, §34)

> Event-time recipient snapshot remains authoritative permanently.
> Deactivation does not rewrite the snapshot.
> Undeliverable recipients are recorded as ECO_NOTIFICATION_FAILED.
> Referenced designation rows use ON DELETE RESTRICT.

**Owner ruling**, constrained by §33.1's *"all original recipients"* — only implementable if the original set is immutable — and by §34's *"a failed email does not undo a threshold event."*

### D-5.1 — Notification handoff model (§34)

> Strike the original option A.
> ECO hands off to the central Temveris notification service and records the dispatch outcome.
> ECO does NOT select channels, resolve preferences, retry, or suppress.
>
> For GV-07, "notified" means the central notification-service handoff was made and the outcome was recorded. It does NOT mean confirmed end-user receipt.

**Spec-locked component:** the behaviour, per §34.
**Owner ruling component:** striking the non-compliant option, and — load-bearing for GV-07 — the **interpretation of "notified"**: handoff made and outcome recorded, never confirmed receipt. GV-07's `recipients_notified_of_withdrawal` is satisfied by a recorded handoff per original recipient.

### D-5.5 — Delivery failure-detail and PII policy (INV-9, §1)

> Transport diagnostics only:
> - service reason code
> - correlation/request identifier
> - maximum 512 characters
> - no Worker name/contact/department
> - no shortfall or workweek figures
> - no notification body text
> - NULL permitted when the service supplies no diagnostic

**Owner ruling.** The specification is silent; INV-9 (*nothing here labels a Worker*) constrains it. The attempt table is append-only and therefore un-redactable, which is why the bound is normative.

### D-6.3 — Leave-excluded time and bankable surplus (§15.1, §18)

> Leave-excluded time never creates bankable surplus.
> Surplus is:
> MAX(0, q - req.minutes)
> and is ledger-derived only.

**Owner ruling ratifying a spec derivation.** §15.1 requires surplus to derive from the same ledger as shortfall, and §18 applies exclusions at step 12 — after `q` and `bank` are fixed at steps 9–11 — so excluded minutes are arithmetically incapable of reaching the surplus quantity.

### D-6.4 — Grace / netting / bank ordering (§18)

> Use §18 verbatim:
> bank → gross → exclusions → grace
>
> Bank is consumed before exclusions and before grace.
> Grace never consumes bank.
> A grace-zeroed remainder does not return to the bank.

**SPEC-LOCKED**, ratified by the owner. Recorded here so the ordering is not re-derived at implementation time. See §1.1.

### D-6.8 — Netting caps and bank representation (§15.1 / INV-3, §35)

> Two surplus caps are required:
> - surplus_cap_per_day_minutes
> - surplus_cap_per_period_minutes
>
> They participate in the governing policy content hash.
>
> There is NO stored bank counter or balance table.
> The bank balance is a fold over immutable day records.
> Each day record records bank_credit_minutes.
>
> Engineering placement may use PolicyVersion as proposed, provided the implementation preserves the above normative contract.

**Spec-locked component:** two caps, no stored counter, fold-over-day-records (§15.1 / INV-3), and the `bank_credit_minutes` day-record field (§35: *"§15, 0 when netting_mode = NONE"*).
**Owner ruling component:** the caps join the governing policy content hash, and `PolicyVersion` is permitted as the engineering placement **conditionally** — the normative contract governs, not the table.

### D-7.1 — Retroactive horizon storage (§21.5, §17)

> PolicyVersion stores retroactive_horizon_days.
> Default = 90 days.
> The value is resolved as-of the workweek being corrected.

**Owner ruling.** §21.5 fixes *Tenant-configurable, default 90 days* but names no artifact. As-of resolution against the corrected workweek — not the horizon in force today — is part of the ruling.

### D-7.2 — Horizon measurement endpoints (§21.5, §33.1)

> elapsed_days =
>     local date on which the threshold event would be raised
>     -
>     last local date of the workweek concerned
>
> Use the workweek's frozen timezone_iana.
> Count whole local calendar days.
> Beyond horizon iff:
> elapsed_days > retroactive_horizon_days
>
> Exactly equal to the horizon remains within the horizon.

**Owner ruling.** §21.5 bounds the age of the week but names neither endpoint. Anchoring on the workweek end rather than the correction instant prevents each new correction restarting the clock. §21.5 and §33.1 together are SPEC-LOCKED on the consequence: beyond the horizon the recomputation still occurs and the record is still corrected, but **no notification is sent** (§33.1: *Never*, for all four audiences).

### D-8.1 — GV-26 state contract (§24.15)

> GV-26 uses LedgerState.
> EcoDayState remains exactly seven members.
> Do NOT add LEDGER_INTEGRITY_FAULT to EcoDayState.

**Owner ruling.** §24.15 writes *"(state: LEDGER_INTEGRITY_FAULT)"* without naming a vocabulary. The ruling follows the GV-16 precedent: a ledger-only state satisfied by asserting the ledger fact beside the ECO-level refusal, without adding a day state. `EcoDayState` stays at seven members: NOT_A_WORKING_DAY, REQUIREMENT_UNRESOLVED, POLICY_UNRESOLVED, DEFERRED_PENDING_LEDGER, EVALUABLE, SHORTFALL, NO_SHORTFALL.

### D-9.1 — Platform-incident mechanism (§24.15)

> Use a persisted immutable platform-incident record in a dedicated ECO table.
> It is:
> - organization scoped
> - append-only
> - immutable
> - linked to the ledger fault
> - records integrity_fault_reason
> - operational evidence, NOT an administrator notification
> - NOT routed through app/alert

**Owner ruling.** §24.15 states the obligation (*"raise a platform incident"*) and defines no mechanism. The ruling is explicit that this is **operational evidence, not a notification**: §33.1's matrix has no row for an integrity fault, and routing it through `app/alert` would couple GV-26 to the recipient authority and leave an unconfigured tenant with no incident at all.

**Consequence for dependency metadata:** because the incident is not routed through `app/alert`, GV-26's `ALERT` blocking-dependency code becomes **stale**, not satisfied. It is removed at promotion on the grounds that it never described a real blocker for this vector, exactly as `PRODUCER` was removed from GV-01 and `EVAL-EVENT` from GV-23 on 2026-09-21.

---

## 3. Superseded

Decision fields D-1.1 through D-9.8 of the owner-ruling intake are CLOSED. The thirteen approved rulings above, together with the spec-locked rules in §1, mechanically determine the remaining 57 fields. No field of that intake remains open, and none may be reopened without a new record superseding this one.

**Decision blockers remaining: 0.** Implementation is authorised; it had not begun when this record was written.

---

## 4. What this record does not do

* It does not amend ZT-SPEC-ECO-002 v2.0. The one normative amendment ever made to the golden vectors (GV-12's rounding locus) remains the only one.
* It does not promote any golden vector. Promotion requires implementation, execution through the real production path, and a full-suite regression run.
* It does not authorise rollout. `early_clock_out_v2` remains OFF and ECO v1 remains disabled; implementation completion is not rollout authorisation.

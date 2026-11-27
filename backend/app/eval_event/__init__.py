"""
Temveris — EVAL-EVENT: the ECO threshold crossing lifecycle.
=============================================================

ZT-SPEC-ECO-002 v2.0, §21.1–§21.5. The fourth stage of the closed pipeline:

    EVAL-DAY  ->  EVAL-WEEK  ->  EVAL-EVENT  ->  NOTIF

STATUS: IMPLEMENTED, NOT WIRED. No route, scheduler or existing service calls
it. ECO v1 stays permanently disabled and the ECO v2 rollout stays OFF with
zero rows — this package neither reads nor writes rollout state.

THE PUBLIC ENTRY POINT

    evaluate_threshold_event(db, organization_id=..., user_id=...,
                             workweek_key=..., mode=...)

It consumes the authoritative ``W`` from EVAL-WEEK and the authoritative ``T``
from ``app.eco_threshold``, compares them, and appends at most one row to an
append-only history.

THE COMPARISON IS ``W >= T`` (§21.1)
------------------------------------
Equality crosses. A week that reaches exactly the threshold has reached the
threshold; implementing this as strictly-greater-than is a defect, and a test
pins the equality case specifically.

THE LIFECYCLE (§21.2 – §21.4)
-----------------------------
    W <  T, no open crossing   ->  NOT_CROSSED       nothing written
    W <  T, open crossing      ->  WITHDRAWN         new ECO_THRESHOLD_WITHDRAWN
    W >= T, no open crossing   ->  CROSSED           sequence 1
    W >= T, after a withdrawal ->  RE_CROSSED        sequence 2, names the
                                                     withdrawal it follows
    W >= T, open crossing      ->  ALREADY_CROSSED   nothing written

"Has this week already crossed?" is answered by the natural key (§21.2) — the
existing ROW is the fact. There is no "has notified" boolean, no in-memory
lock and no process-local memo, so a replay in a different process, a
different worker or a different year reaches the same answer.

The withdrawal never deletes, hides or back-dates the crossing it closes
(R-21.3.1). It cannot: database triggers make the table append-only (D1), and
they refuse UPDATE and DELETE below the ORM, where a Core statement would
otherwise slip past a listener.

WHAT THIS PACKAGE DELIBERATELY IS NOT
-------------------------------------
* **No notification, delivery or copy.** D7 makes this event generation only.
  Nothing here sends, schedules, queues, renders or words anything, and
  ``app.notifications`` is neither imported nor touched.
* **No horizon decision.** D3 leaves the retroactive NOTIFICATION horizon to
  MODEL-HORIZON and ALERT. This package records ``is_retroactive`` and stops.
  "Fail closed" governs notification eligibility, not persistence, so a
  retroactive crossing is still RECORDED. ``is_retroactive`` is a deterministic
  lineage INDICATOR — the week rests on corrected evidence — and deliberately
  not a claim that the correction caused the crossing; see
  ``RetroactiveLineage`` for the precision limit and why it is not papered
  over.
* **No reviewer.** D5's recipient snapshot is the WORKER ONLY for this
  increment. No reviewer role, permission, resolver or configuration key
  exists in the repository, and none is invented here; the designated reviewer
  is deferred to ALERT, and its absence refuses nothing.
* **No rollout read or write.** ``mode`` arrives from the already-authorized
  caller. SHADOW rows are logically isolated (D8): every read is scoped to one
  mode, so no operational lifecycle decision can be satisfied by a shadow row.
* **No endpoint, scheduler, UI or analytics.**
* **No clock-based decision.** The only timestamp taken is the audit
  ``created_at_utc`` of the row being written, which no branch reads.

THE MODULES
-----------
``outcomes``          the typed result vocabulary (§32: these names are a
                      product surface and cannot be corrected later)
``append_only_ddl``   the D1 trigger SQL, shared by the ORM and the migration
``models``            the append-only ``eco_threshold_event`` table
``service``           the lifecycle itself
"""

from .append_only_ddl import (
    EVENT_TABLE,
    SUPPORTED_DIALECTS,
    append_only_statements,
    drop_statements,
)
from .models import EcoThresholdEvent, ThresholdEventImmutable
from .outcomes import (
    EventMode,
    EventOutcome,
    EventTier,
    EventType,
    RefusalReason,
    RetroactiveLineage,
    ThresholdEventResult,
    WeekProvenance,
)
from .horizon import (
    HorizonOutcome,
    HorizonResolution,
    horizon_decision,
    resolve_retroactive_horizon,
    REASON_HORIZON_POLICY_UNRESOLVED,
    REASON_HORIZON_NOT_CONFIGURED,
    REASON_HORIZON_TIMEZONE_UNRESOLVED,
    REASON_HORIZON_MALFORMED_WORKWEEK,
)
from .service import (
    EVAL_EVENT_ACTOR,
    ProvenanceIncomplete,
    evaluate_threshold_event,
    read_events,
)

__all__ = [
    # public entry point
    "evaluate_threshold_event",
    "read_events",
    # MODEL-HORIZON (D-7.1 / D-7.2). Notification ELIGIBILITY only:
    # nothing here notifies, and §21.5 keeps persistence untouched.
    "HorizonOutcome",
    "HorizonResolution",
    "horizon_decision",
    "resolve_retroactive_horizon",
    "REASON_HORIZON_POLICY_UNRESOLVED",
    "REASON_HORIZON_NOT_CONFIGURED",
    "REASON_HORIZON_TIMEZONE_UNRESOLVED",
    "REASON_HORIZON_MALFORMED_WORKWEEK",
    "EVAL_EVENT_ACTOR",
    # result vocabulary
    "EventMode",
    "EventTier",
    "EventType",
    "EventOutcome",
    "RefusalReason",
    "WeekProvenance",
    "RetroactiveLineage",
    "ThresholdEventResult",
    # storage
    "EcoThresholdEvent",
    "ThresholdEventImmutable",
    "ProvenanceIncomplete",
    "EVENT_TABLE",
    # append-only enforcement
    "SUPPORTED_DIALECTS",
    "append_only_statements",
    "drop_statements",
]

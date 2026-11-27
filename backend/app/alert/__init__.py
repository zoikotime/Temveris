"""
Temveris — ALERT: the durable ECO notification record/render layer.
====================================================================

ZT-SPEC-ECO-002 v2.0. The fifth stage of the closed pipeline:

    EVAL-DAY -> EVAL-WEEK -> EVAL-EVENT -> ALERT -> (delivery, not built)

STATUS: IMPLEMENTED, NOT WIRED, NOTHING DELIVERED. No route, scheduler or
existing service calls it. ECO v1 stays permanently disabled, and the ECO v2
rollout stays OFF — under which this package records nothing.

THE PUBLIC ENTRY POINT

    record_threshold_notification(db, event_id=..., audience=WORKER)

It takes an ``eco_threshold_event.id`` — a fact EVAL-EVENT already established
— and appends at most one IMMUTABLE notification row for one audience.

WHAT IT IS
----------
``outcomes``          the typed vocabulary (§32: these names are a product
                      surface and cannot be corrected later)
``append_only_ddl``   the R5 trigger SQL, shared by the ORM and the migration
``models``            the immutable ``eco_notification`` and its append-only
                      ``eco_notification_delivery_attempt`` history
``rendering``         locked-copy composition and the §31.1 gate
``service``           eligibility, recipient resolution, and the append

THE IDENTITY — TWO AXES, DELIBERATELY SEPARATE
-----------------------------------------------
    logical episode (R3, UNCHANGED):
        (organization_id, user_id, workweek_key, tier, mode, sequence)

    physical notification identity:
        episode + notification_type + audience

``notification_type`` is PURPOSE; ``audience`` is WHO. Neither is folded into
the other, and neither is folded into the episode: a re-crossing at
``sequence = 2`` is a NEW episode, while a second audience is NOT.

WORKER ROWS ONLY, TODAY
-----------------------
No designated-reviewer model, role, setting or resolver existed anywhere in the
repository when this package shipped, and owner ruling R2 deferred it rather
than permitting one to be inferred from an admin/HR/owner cascade.
``app.recipient_authority`` supplied that authority under Stage 5 (ruling D-3),
so the ORIGINAL reason is spent — but ALERT still creates WORKER rows and
refuses every other audience, because being able to name an administrator and
being authorized to notify one are different questions. The second is
ALERT-DELIVERY, which is still open. The ``audience`` column is nevertheless open
text, so an authorized future audience needs no migration and no change to any
existing row — the database permits what the service declines to invent.

WHAT THIS PACKAGE DELIBERATELY IS NOT
-------------------------------------
* **No delivery HERE.** No email, SMTP, Resend, websocket, push, inbox or UI.
  Ruling D-C: B authorised ``dispatch_processing``, which hands committed
  notifications to ``app.notification_platform`` and records what comes back —
  so the delivery-attempt table is no longer inert. What remains true, and is
  the point, is that this package still transports NOTHING: the platform owns
  channel selection, preference resolution, suppression, retry and transport,
  and an AST guard pins that ALERT imports no mailer.
* **No retry policy.** R6 leaves attempt limits, windows and backoff
  unspecified on purpose, so none is invented here. D-C did not change that:
  a pass re-hands a recipient whose last result was ``failed`` and lets the
  platform decide, which under D-4 means replaying the original result. ECO
  has no retry policy of its own and must never acquire one.

* **No scheduler.** ``run_eco_dispatch_pass`` is the one authorized trigger
  and it is ON DEMAND ONLY: one invocation is one pass. There is no route, no
  scheduler, no startup task, no ``run_forever`` loop and no drain loop, and
  nothing calls it from threshold-event creation. Its whole content is the
  rollout gate the processor deliberately does not contain.
* **No horizon.** D3 leaves the retroactive notification horizon to
  MODEL-HORIZON. Rendering happens at record creation, BEFORE any delivery
  decision, which is what keeps GV-22 reachable without one. GV-23 stays
  blocked.
* **No reviewer or administrator recipient resolution HERE.** Stage 5's
  ``app.recipient_authority`` resolves the four §33.1 audiences; this package
  imports it nowhere and still reads the Worker from the frozen snapshot.
* **No threshold evaluation.** ALERT cannot create an event: its entry point
  takes an id, not a week.
* **No legacy notification reuse.** ``attendance.early_clockout``,
  ``email_service``, ``app.notifications``, ``AuditLog`` and ``risk_alerts``
  are all outside this package, and an AST guard pins that.

ROLLOUT (D8) — THE FIRST CONSUMER OF THE DURABLE CONTROL
---------------------------------------------------------
    OFF      no operational ECO notification processing; nothing is recorded
    SHADOW   records MAY be created and retained, identifiable as SHADOW; an
             OPERATIONAL-mode event is refused, because a SHADOW rollout may
             have no operational effect
    ENABLED  normal recording

The control key is ``app.rollout.CONTROL_EARLY_CLOCK_OUT_V2`` — already defined
by the rollout package. No new rollout system, no new key, no state change.
"""

from .append_only_ddl import (
    ATTEMPT_TABLE,
    NOTIFICATION_TABLE,
    SUPPORTED_DIALECTS,
    append_only_statements,
    attempt_append_only_statements,
    attempt_drop_statements,
    drop_statements,
    guard_drop_statements,
)
from .models import (
    EcoNotification,
    EcoNotificationDeliveryAttempt,
    NotificationImmutable,
)
from .outcomes import (
    AUTHORIZED_AUDIENCES,
    NotificationAudience,
    NotificationOutcome,
    NotificationResult,
    NotificationType,
    RecipientIdentity,
    RecipientKind,
    RefusalReason,
)
from .rendering import (
    ProhibitedTerminology,
    WORKER_REACHED_COPY_KEYS,
    WORKER_WITHDRAWN_COPY_KEYS,
    assert_clean,
    render_body,
)
from .dispatch_processing import (
    MAX_HANDOFFS_PER_PASS,
    DispatchPassOutcome,
    DispatchPassResult,
    process_pending_notification_dispatches,
)
from .dispatch_trigger import TriggerResult, run_eco_dispatch_pass
from .service import (
    ALERT_ACTOR,
    NOTIFICATION_TYPE_FOR_EVENT,
    NotificationContentConflict,
    read_notifications,
    record_threshold_notification,
)

__all__ = [
    # public entry points
    "record_threshold_notification",
    "read_notifications",
    # ALERT-DELIVERY (ruling D-C: B). Callable, and called by NOTHING: the
    # trigger that invokes a pass is a separate authorization.
    "process_pending_notification_dispatches",
    # The authorized D-C trigger: the rollout gate in front of a pass. On
    # demand only — no scheduler, no loop, no startup task.
    "run_eco_dispatch_pass",
    "TriggerResult",
    "MAX_HANDOFFS_PER_PASS",
    "DispatchPassResult",
    "DispatchPassOutcome",
    "ALERT_ACTOR",
    "NOTIFICATION_TYPE_FOR_EVENT",
    # vocabulary
    "NotificationAudience",
    "AUTHORIZED_AUDIENCES",
    "NotificationType",
    "RecipientKind",
    "RecipientIdentity",
    "NotificationOutcome",
    "NotificationResult",
    "RefusalReason",
    # storage
    "EcoNotification",
    "EcoNotificationDeliveryAttempt",
    "NotificationImmutable",
    "NOTIFICATION_TABLE",
    "ATTEMPT_TABLE",
    # rendering
    "render_body",
    "assert_clean",
    "ProhibitedTerminology",
    "WORKER_REACHED_COPY_KEYS",
    "WORKER_WITHDRAWN_COPY_KEYS",
    # errors
    "NotificationContentConflict",
    # append-only enforcement
    "SUPPORTED_DIALECTS",
    "append_only_statements",
    "attempt_append_only_statements",
    "drop_statements",
    "attempt_drop_statements",
    "guard_drop_statements",
]

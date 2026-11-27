"""
Temveris — the §24.3 day-scoped administrator alert.
=====================================================

ZT-SPEC-ECO-002 v2.0 §24.3 and §33.1; owner rulings **D-C: B** (a separate
day-scoped primitive alongside ALERT), **D-B** (the non-suppressible
administrator class), **D-1** (unresolved PRIMARY ADMIN is recorded, never
substituted) and **Ruling 3: B** (§31's locked-copy gate does not apply here).

STATUS: IMPLEMENTED, NOT WIRED TO EVALUATION. Nothing calls
``raise_day_administrator_alert`` automatically; both tables ship empty.

PUBLIC ENTRY POINTS

    raise_day_administrator_alert(db, day_record=..., mode=...)
    process_pending_day_alert_dispatches(db, organization_id=..., limit=...)

WHAT §24.3 REQUIRES
-------------------
For a day whose requirement resolves at no rank: *"no shortfall, no advisory,
no notification to the Worker, and an operational alert to the Tenant
administrator"* — and *"The Worker MUST NOT be shown anything about this — it
is a Tenant configuration defect, and surfacing it to the Worker as an
attendance condition would be a false statement about them."*

§33.1's matching row: WORKER *Never* · PRIMARY ADMIN *Always (operational)* ·
DIRECT MANAGER *No* · HR ADMIN *No*.

WHY THIS IS A SEPARATE PRIMITIVE
---------------------------------
A requirement-unresolved day produces **no threshold event**: EVAL-WEEK blocks
the week and EVAL-EVENT refuses anything that is not COMPLETE. The existing
``eco_notification`` is week-shaped and FK-bound to ``eco_threshold_event``, so
carrying this alert there would mean fabricating an event or discarding the day
identity. Ruling D-C: B forbade the first; this package avoids the second by
being anchored to ``EcoDayRecord`` and its exact evaluation generation.

**``app/alert`` is untouched**, which is what keeps GV-07 isolated: no shared
row, no shared constraint, no shared code path.

WHAT IS REUSED RATHER THAN REBUILT
-----------------------------------
* ``app.recipient_authority`` — PRIMARY ADMIN resolution and the frozen
  snapshot (D-3.7). No parallel recipient resolution exists here.
* ``app.notification_platform`` — channel selection, preference resolution,
  suppression, transport, retry, correlation ids and request idempotency
  (D-2, D-3, D-4). This package selects no channel and sends nothing.
* ``ECO_ADMIN_OPERATIONAL`` — the non-suppressible class from D-B, applied by
  the platform to the PRIMARY_ADMIN audience.

WHAT IS DELIBERATELY ABSENT
---------------------------
* **No threshold event, ever.** Nothing here creates or mutates one, and a
  guard asserts the package imports ``app.eval_event`` nowhere.
* **No Worker path.** The audience vocabulary contains PRIMARY_ADMIN alone and
  the database CHECK enforces it, so a Worker row is unrepresentable.
* **No §31 locked copy.** Ruling 3: B. The body is implementation copy in
  ``copy.py``, clearly marked as such and kept out of the locked register —
  though it is still held to §31.1's prohibited terminology and §4.1's neutral
  vocabulary, and a test scans it.
* **No retry, no scheduler, no loop.** One pass per invocation, ceiling 50.
* **No HTTP surface.**
* **No automatic invocation.** Evaluation does not raise these alerts; doing
  so is a separate authorization.
"""

from .copy import (
    COPY_REGIME,
    IMPLEMENTATION_COPY,
    REQUIREMENT_UNRESOLVED_BODY,
    REQUIREMENT_UNRESOLVED_BODY_ID,
    render_requirement_unresolved_body,
)
from .dispatch import (
    MAX_HANDOFFS_PER_PASS,
    DayAlertDispatchOutcome,
    DayAlertDispatchResult,
    process_pending_day_alert_dispatches,
)
from .models import EcoDayAdministratorAlert, EcoDayAlertDeliveryAttempt
from .outcomes import (
    AUTHORIZED_AUDIENCES,
    FORBIDDEN_AUDIENCES,
    DayAlertOutcome,
    DayAlertResult,
    DayAlertType,
    RefusalReason,
)
from .service import (
    ALERTABLE_STATE,
    RECIPIENT_RESOLVED,
    RECIPIENT_UNRESOLVED,
    raise_day_administrator_alert,
)

__all__ = [
    # public entry points
    "raise_day_administrator_alert",
    "process_pending_day_alert_dispatches",
    # vocabulary
    "DayAlertType",
    "DayAlertOutcome",
    "DayAlertResult",
    "RefusalReason",
    "AUTHORIZED_AUDIENCES",
    "FORBIDDEN_AUDIENCES",
    "ALERTABLE_STATE",
    "RECIPIENT_RESOLVED",
    "RECIPIENT_UNRESOLVED",
    "MAX_HANDOFFS_PER_PASS",
    "DayAlertDispatchResult",
    "DayAlertDispatchOutcome",
    # implementation copy (Ruling 3: B — NOT §31 locked)
    "COPY_REGIME",
    "IMPLEMENTATION_COPY",
    "REQUIREMENT_UNRESOLVED_BODY",
    "REQUIREMENT_UNRESOLVED_BODY_ID",
    "render_requirement_unresolved_body",
    # persistence
    "EcoDayAdministratorAlert",
    "EcoDayAlertDeliveryAttempt",
]

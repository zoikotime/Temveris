"""
Temveris — ECO weekly threshold capture (write path)
=====================================================

ZT-SPEC-ECO-002 v2.0, W5. The counterpart to ``app.eco_threshold``: that
package reads the threshold that governed a workweek, this one records the
organization's decision about what it should be.

STATUS: IMPLEMENTED, NOT WIRED. No route, service or scheduler calls it.

WHY A SEPARATE PACKAGE
----------------------
The reader is forbidden to ask what week it is now — its as-of date is the
persisted ``workweek_key``, and computing a week would read current
configuration and undo W4. Capture must ask exactly that question, to refuse an
effective date earlier than the open workweek. Rather than loosen the reader's
guard, the two live apart and the guard stays as strict as it was.

WHAT IT ENFORCES
----------------
* **No default, ever.** A value is captured because somebody chose it. Nothing
  here seeds, infers or falls back — not from a golden vector, not from
  required hours, breaks, grace or idle settings.
* **Organization scope only**, held by the signature: there is no per-Worker
  parameter to supply.
* **Role authority** — ``admin`` or ``hr`` for their own organization, or an
  explicit ``SYSTEM`` actor. No capability is consulted, so no plan entitlement
  can stop an organization recording its own configuration decision.
* **An actor, always**, with a label that outlives deletion of the user row.
* **Ruling H**, checked twice: on the requested effective date, and on the
  configuration version that actually receives the row.
* **A complete configuration snapshot.** A capture creates a NEW version
  carrying the whole snapshot currently in force forward, unchanged, plus the
  threshold. Nothing is appended to an existing version, no value is updated
  and no ``content_hash`` is recomputed in place, so no other configuration
  domain is left unresolved by a threshold being set.
* **An audit record for every attempt**, captured or refused.

WHAT IT DELIBERATELY IS NOT
---------------------------
* No HTTP route, no API schema, no UI, no scheduler.
* No RBAC or capability change; it reads a role, it grants nothing.
* No threshold comparison, no crossing, no event, no notification — all
  EVAL-EVENT, all still blocked.
* No new table and no second versioning mechanism: a threshold is a row in the
  existing immutable ``config_version``/``config_value`` tables.
* No mutation of historical configuration. Corrections are later versions.

TWO THINGS IT CANNOT DO
-----------------------
It cannot capture onto a date where a configuration version already starts.
``(organization_id, effective_from)`` is unique so a second version cannot
begin there, and a version is immutable so the existing one cannot receive a
new value. Refused with ``EXISTING_VERSION_CANNOT_RECEIVE_THRESHOLD`` (or
``THRESHOLD_ALREADY_CAPTURED`` when that version already carries one); the
administrator picks a later eligible date. Permanent behaviour, ruling 2.

It cannot capture for an organization with no configuration version to
supersede. Refused with ``NO_CONFIGURATION_TO_CARRY_FORWARD`` rather than
inventing a baseline or writing a threshold-only version. Establishing an
organization's first configuration version is a Step 6 capture-path question
and is not yet answered anywhere, so this path is unreachable in production
until it is — reported, not worked around.
"""

from .boundary import current_open_workweek_start
from .outcomes import (
    AUTHORIZED_ROLES,
    SYSTEM_ACTOR_LABEL,
    ActorContext,
    CaptureOutcome,
    RefusalReason,
    ThresholdCaptureResult,
)
from .service import (
    AUDIT_ACTION_CAPTURED,
    AUDIT_ACTION_REFUSED,
    AUDIT_ENTITY,
    capture_threshold,
)

__all__ = [
    "capture_threshold",
    "current_open_workweek_start",
    "ActorContext",
    "CaptureOutcome",
    "RefusalReason",
    "ThresholdCaptureResult",
    "AUTHORIZED_ROLES",
    "SYSTEM_ACTOR_LABEL",
    "AUDIT_ENTITY",
    "AUDIT_ACTION_CAPTURED",
    "AUDIT_ACTION_REFUSED",
]

"""
Temveris — an organization's first configuration version.
==========================================================

ZT-SPEC-ECO-002 v2.0. The missing precondition of every other configuration
path: until an organization holds one immutable configuration version, there is
nothing for a later capture to supersede and nothing for a historical date to
resolve against.

STATUS: IMPLEMENTED, NOT WIRED. No route, service or scheduler calls it.

WHAT IT ESTABLISHES
-------------------
One ``ConfigVersion``, carrying:

* ``required_minutes`` — mandatory, administrator-supplied, written at the
  §10.1 ladder's last-resort position (``TENANT_FALLBACK``, rank 6);
* the ECO weekly threshold — only when the administrator supplied one in the
  same authorized action.

Both rows are organization-scoped. There is no per-Worker parameter, no tier
parameter, no rank parameter and no key parameter: the service controls them,
so a mis-scoped or mis-tiered baseline is unexpressible rather than validated.

WHAT IT WILL NOT DO
-------------------
* **Invent a value.** Nothing here reads a live setting, an employment-type
  policy, a schedule default, a risk threshold, or the 8-hour runtime
  fallback. A number the product fell back to is not a number an organization
  chose, and capturing one would fabricate the evidence this layer exists to
  make trustworthy.
* **Choose a date.** ``effective_from`` is supplied explicitly and is never
  derived from the organization's creation date, the current date or the
  earliest attendance record. The first version fixes the historical floor —
  everything before it resolves to no configuration, forever.
* **Backdate.** The date must be at or after the current open workweek start,
  using the one canonical workweek calculation. There is no second one.
* **Act on its own.** An explicit ``admin`` or ``hr`` actor of that
  organization is required. ``super_admin`` is refused, and so is a SYSTEM
  actor — an implicit background process is exactly what an
  administrator-initiated action is not.
* **Become the second capture.** If the organization already holds any
  configuration version the attempt is refused and nothing is written. The
  complete-snapshot supersession mechanism belongs to threshold capture and is
  not invoked here.
* **Write an incomplete snapshot.** An empty version and a threshold-only
  version are prohibited, and neither is expressible: ``required_minutes`` is
  a mandatory parameter with no default.

WHAT IT REUSES RATHER THAN REBUILDS
-----------------------------------
``compute_content_hash`` unchanged, both immutability listeners unchanged,
every constraint and unique index unchanged, the canonical ``REQUIREMENT_KEY``,
the ECO threshold's own key/tier/rank constants, the approved workweek-boundary
helper and the approved ``ActorContext``. Step 6's generic ``capture_version()``
is deliberately NOT modified: it accepts an empty snapshot, consults no role,
sets no durable label, accepts any date and raises instead of returning an
auditable outcome — four conflicts with the rulings, and 39 call sites that
must not change behaviour.

KNOWN COUPLING, TO BE CLEANED UP LATER
--------------------------------------
This package imports the workweek-boundary helper and the actor type from
``eco_threshold_capture``, so a Step 6 concern depends on an ECO package. That
inversion is the accepted trade for touching no already-verified code;
promoting the two into a neutral module is a later cleanup, not a change made
here.
"""

from .outcomes import (
    MAX_REQUIRED_MINUTES,
    MIN_REQUIRED_MINUTES,
    REQUIREMENT_RANK,
    REQUIREMENT_TIER,
    InitialCaptureOutcome,
    InitialCaptureRefusal,
    InitialCaptureResult,
    SnapshotShape,
)
from .service import (
    AUDIT_ACTION_CAPTURED,
    AUDIT_ACTION_REFUSED,
    AUDIT_ENTITY,
    capture_initial_configuration,
)

__all__ = [
    "capture_initial_configuration",
    "InitialCaptureOutcome",
    "InitialCaptureRefusal",
    "InitialCaptureResult",
    "SnapshotShape",
    "REQUIREMENT_TIER",
    "REQUIREMENT_RANK",
    "MIN_REQUIRED_MINUTES",
    "MAX_REQUIRED_MINUTES",
    "AUDIT_ENTITY",
    "AUDIT_ACTION_CAPTURED",
    "AUDIT_ACTION_REFUSED",
]

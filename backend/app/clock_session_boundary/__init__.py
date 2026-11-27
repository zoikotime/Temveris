"""
Temveris — session-boundary provenance (§45.D-15).
===================================================

Records, immutably and at the moment of the write, which of the five
owner-approved origins each ``clock_sessions`` boundary represents.

Owner decisions this package implements:

* **D15-1-B** — ``app/timesheets/time_tracking.py`` is dead, unmounted code and
  is NOT a production writer. It gets no provenance; a repository guard
  (``tests/test_d15_dead_writer_guard.py``) fails if anything imports or mounts
  it. This package deliberately does not reference it.
* **D15-2-B** — both boundaries carry an origin. ``clock_in`` admits only
  ``WORKER`` and ``ADMIN_OVERRIDE``; ``clock_out`` admits all five. Enforced by
  a database CHECK, not by convention.
* **D15-3-A** — append-only history in ``clock_session_boundary_event``. No
  provenance column is added to ``clock_sessions``.
* **D15-4-A** — the table is protected history: both foreign keys are RESTRICT
  and it participates in ``_PROTECTED_HISTORY_TABLES``, so deleting a Worker
  with boundary history is refused with ``EMPLOYEE_HAS_PROTECTED_HISTORY``.
  ``actor_label`` carries no foreign key at all, so deleting an actor can
  neither destroy nor mutate an event.

WHAT THIS PACKAGE IS NOT
------------------------
Not the Work Ledger producer. It records origins; it does not read them, fold
them into intervals, decide a day's state, or select which boundary is
authoritative when several exist. Those are producer questions, gated
separately. Nothing in the ECO pipeline consumes this table yet.

No backfill. The table starts empty and is only ever appended to by live
writers. Sessions that predate it hold zero events and are therefore
permanently unattributable — by ruling, not by omission.
"""

from .errors import (
    BoundaryEventError,
    BoundaryEventImmutable,
    InvalidBoundary,
    InvalidBoundaryProvenance,
    MissingActorLabel,
    NaiveBoundaryTimestamp,
)
from .models import (
    ALLOWED_PROVENANCE,
    Provenance,
    BOUNDARIES,
    BOUNDARY_CLOCK_IN,
    BOUNDARY_CLOCK_OUT,
    CLOSE_BOUNDARY_PROVENANCE,
    OPEN_BOUNDARY_PROVENANCE,
    ClockSessionBoundaryEvent,
)
from .service import (
    ACTOR_LABEL_MAX,
    SYSTEM_ACTOR_LABEL,
    actor_label_for_user,
    actor_label_for_user_id,
    append_boundary_event,
)

__all__ = [
    # model + vocabulary
    "ClockSessionBoundaryEvent",
    # Re-exported, NOT redefined: the identical Work Ledger enum object. The
    # boundary writers import their vocabulary from here so each one depends on
    # a single module, and so `app/employees/router.py` keeps its existing
    # freedom from direct ECO-package imports (see
    # tests/test_worker_history_retention.py). The transitive reach is pure
    # vocabulary — app.work_ledger.states holds enums and frozensets, no models
    # and no database — and is asserted in tests/test_clock_session_boundary.py.
    "Provenance",
    "BOUNDARY_CLOCK_IN",
    "BOUNDARY_CLOCK_OUT",
    "BOUNDARIES",
    "OPEN_BOUNDARY_PROVENANCE",
    "CLOSE_BOUNDARY_PROVENANCE",
    "ALLOWED_PROVENANCE",
    # the one write operation
    "append_boundary_event",
    "actor_label_for_user",
    "actor_label_for_user_id",
    "SYSTEM_ACTOR_LABEL",
    "ACTOR_LABEL_MAX",
    # refusals
    "BoundaryEventError",
    "InvalidBoundary",
    "InvalidBoundaryProvenance",
    "MissingActorLabel",
    "NaiveBoundaryTimestamp",
    "BoundaryEventImmutable",
]

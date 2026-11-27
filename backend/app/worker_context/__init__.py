"""
Temveris — Worker Context History
==================================

STATUS: IMPLEMENTED, NOT WIRED. No route, scheduler or existing service calls
into this package. Creating, inviting and updating employees all work exactly as
they did, and every existing consumer of ``User.employment_type`` is untouched.

WHY IT EXISTS
-------------
GV-27 requires the requirement for a past day to be resolvable as it stood on
that day: Part-Time Monday and Tuesday, Full-Time Wednesday onward, giving
``[240, 240, 525, 525, 525]``. The platform held worker classification in one
mutable column, overwritten with no audit row, so re-evaluating Monday after a
Wednesday change returned Full-Time — violating §17 historical reproducibility
and INV-8.

This package supplies the missing temporal dimension.

ONE FACT, TWO ACCESS PATHS, ONE WRITER
--------------------------------------
    "What is this Worker NOW?"        User.employment_type  (unchanged)
    "What were they on 14 March?"     worker_context_as_of(...)

They are not competing sources. One service writes both in the same
transaction, and ``worker_context_as_of(today)`` must equal the projection — a
test pins that. ``User.employment_type`` is NOT replaced and NOT migrated.

WHAT MUST NEVER BE ADDED HERE
-----------------------------
* **Any reconstruction of destroyed history.** H-1 is approved and locked:
  before a Worker's earliest captured period the answer is UNKNOWN. Never
  today's value, never the latest value, never ``full_time``, and never
  inferred from WEC, ShiftAssignment, payroll, audit text or schedule policy —
  none of which is an authoritative employment history. An import-graph test
  enforces the absence of those dependencies.
* **An attestation mechanism.** ``ATTESTED`` is deliberately not a provenance
  value; H-2 was declined. Adding it must require an owner decision, not an
  edit.
* **Any mapping between product employment type and a legal worker category.**
  D11 is open. ``context_vocabulary`` keeps them separate, and nothing here
  translates one into the other.
* **Any requirement, minutes or worked-time calculation.** This records WHAT a
  Worker was, never what they owed. Step 5 is unchanged and the problem sits
  below its existing seam.
* **Any mutation.** The table is append-only; a correction appends a complete
  new timeline version and leaves the prior version byte-identical.

PROVENANCE HONESTY
------------------
Three of the five live worker-creation paths never set ``employment_type``, so
the column default applies and the Worker is Full-Time because a schema said so.
Such a value is recorded as ``CREATION_DEFAULT`` — permanently distinguishable
from ``CAPTURED`` — and nothing may infer historical Full-Time status from it.
"""

from .errors import (
    InvalidWorkerContext,
    StaleWorkerContextVersion,
    WorkerContextError,
    WorkerContextImmutable,
    WorkerContextOrganizationRequired,
)
from .models import WorkerContextTimeline
from .service import (
    ContextOutcome,
    ContextPeriod,
    RecordedTimeline,
    UNKNOWN_OUTCOMES,
    WorkerContextResolution,
    current_timeline_version,
    read_timeline,
    record_timeline,
    set_worker_context,
    worker_context_as_of,
)
from .vocabulary import (
    DELIBERATE_PROVENANCE,
    KNOWN_VOCABULARIES,
    VOCABULARY_PRODUCT_EMPLOYMENT_TYPE,
    WorkerContextProvenance,
    is_known_vocabulary,
    is_valid_provenance,
    parse_provenance,
)

__all__ = [
    # model
    "WorkerContextTimeline",
    # vocabulary
    "WorkerContextProvenance",
    "DELIBERATE_PROVENANCE",
    "VOCABULARY_PRODUCT_EMPLOYMENT_TYPE",
    "KNOWN_VOCABULARIES",
    "is_valid_provenance",
    "parse_provenance",
    "is_known_vocabulary",
    # service
    "ContextOutcome",
    "UNKNOWN_OUTCOMES",
    "WorkerContextResolution",
    "ContextPeriod",
    "RecordedTimeline",
    "worker_context_as_of",
    "current_timeline_version",
    "read_timeline",
    "record_timeline",
    "set_worker_context",
    # errors
    "WorkerContextError",
    "InvalidWorkerContext",
    "WorkerContextOrganizationRequired",
    "StaleWorkerContextVersion",
    "WorkerContextImmutable",
]

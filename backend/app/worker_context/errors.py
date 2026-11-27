"""
Worker-context history failures.
================================

WORKER CONTEXT HISTORY foundation.

Every one of these refuses a WRITE. None is raised by the read path:
``worker_context_as_of`` never raises, because a resolver that can throw while
being consulted is a resolver that can fail an evaluation for the wrong reason.
It returns UNKNOWN instead, and UNKNOWN means the day is not evaluated.
"""

from __future__ import annotations


class WorkerContextError(Exception):
    """Base for worker-context write failures."""


class InvalidWorkerContext(WorkerContextError):
    """A period is not storable as recorded evidence.

    An empty value, an unapproved provenance, an undeclared vocabulary, or two
    periods claiming the same effective date. Refused whole rather than
    partially stored: a timeline silently trimmed to fit is worse than one that
    was rejected loudly.
    """


class WorkerContextOrganizationRequired(WorkerContextError):
    """The worker carries no organization.

    ``User.organization_id`` is nullable. Worker context refuses to inherit
    that: a period whose tenant is unknown cannot be isolated, filtered or
    audited by organization. Fixing the worker record is an operational action,
    not something this layer guesses at.
    """


class StaleWorkerContextVersion(WorkerContextError):
    """The write was computed against a timeline version that is no longer
    current.

    Two administrators correcting the same worker must not have their edits
    interleaved. Merging two independently-authored timelines would produce a
    third that neither of them wrote, so the later writer is rejected and
    re-runs against what is now current.
    """


class WorkerContextImmutable(WorkerContextError):
    """A recorded period would be altered or removed.

    Worker context is append-only. A correction records a NEW timeline version;
    the prior version stays byte-identical so the classification as it stood on
    any past date can always be reproduced.
    """


__all__ = [
    "WorkerContextError",
    "InvalidWorkerContext",
    "WorkerContextOrganizationRequired",
    "StaleWorkerContextVersion",
    "WorkerContextImmutable",
]

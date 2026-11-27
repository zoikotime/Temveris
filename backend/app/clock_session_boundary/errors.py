"""
Errors raised by the session-boundary provenance recorder (§45.D-15).
=====================================================================

Every one of these is a REFUSAL. None of them has a fallback that guesses:
§45.D-15 states that the producer must not infer, default or guess provenance,
so a caller that cannot say where a boundary came from gets an exception, never
a synthesized origin.
"""

from __future__ import annotations


class BoundaryEventError(Exception):
    """Base class for every session-boundary provenance refusal."""


class InvalidBoundary(BoundaryEventError):
    """The boundary is not ``clock_in`` or ``clock_out``."""


class InvalidBoundaryProvenance(BoundaryEventError):
    """The provenance is not approved for this boundary.

    Raised for an unapproved value outright, and for a closure-only value
    (``STALE_AUTOCLOSE``, ``ABANDONED_AUTOCLOSE``, ``OFFBOARDING``) offered for
    the ``clock_in`` boundary — a clock-in is not a closure, so a closure origin
    would be an attribution of an event that has not happened.
    """


class MissingActorLabel(BoundaryEventError):
    """No actor label was supplied.

    ``SYSTEM`` is the explicit label for an unattended closure. It has to be
    passed deliberately; a blank label is an actorless write, and this recorder
    does not have one.
    """


class NaiveBoundaryTimestamp(BoundaryEventError):
    """A boundary or write instant arrived without a timezone.

    §16.4 prohibits naive instants. A naive value would silently be read back as
    UTC and could move which day a session is attributed to.
    """


class BoundaryEventImmutable(BoundaryEventError):
    """An attempt to update or delete a boundary event that already exists.

    Boundary events are append-only by owner ruling D15-3-A. A later boundary
    write is recorded as a NEW event; an earlier one is never rewritten.
    """


__all__ = [
    "BoundaryEventError",
    "InvalidBoundary",
    "InvalidBoundaryProvenance",
    "MissingActorLabel",
    "NaiveBoundaryTimestamp",
    "BoundaryEventImmutable",
]

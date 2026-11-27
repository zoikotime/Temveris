"""
Temveris — Departure Resolution (§24.6).
=========================================

    resolve_departure(db, organization_id=..., user_id=..., context=...,
                      day_closed=...)  ->  DepartureResolution

The dedicated primitive owner Ruling 4 authorizes: it decides whether an
already-attributable Worker-day has a verified exit, and nothing else.

WHY IT EXISTS
-------------
§24.6 requires a day with no clock-out to resolve to MISSING_CLOCK_OUT, compute
no shortfall, show no advisory and contribute nothing — and forbids assuming a
departure time. Nothing could produce that condition, because:

* the **producer** may transport a verdict but never originate one
  (D2-A/D13-A, reaffirmed by Ruling 2);
* the **working-day classifier** answers a different question and its
  vocabulary is closed (Ruling 3);
* **EVAL-DAY** cannot hold the state at all — R4 fixes ``EcoDayState`` at six
  values and Ruling 1 keeps MISSING_CLOCK_OUT out of them.

So the verdict had no owner. This package is that owner, and only that.

WHAT IT IS NOT
--------------
Not a classifier of working days. Not a scheduler — it starts nothing, polls
nothing and reads no clock. Not a timeout: the closure point arrives as the
``day_closed`` argument, derived from the producer's existing D18-F3 gate, so
no new cutoff, grace period or wall-clock threshold is introduced (Ruling 5).
Not a writer: it returns a verdict and touches no table.

Above all it never produces a departure instant. ``DepartureResolution`` has no
field that could carry one — §24.6's "MUST NEVER assume a departure time" is
enforced by the type, not by discipline.
"""

from .outcomes import DepartureOutcome, DepartureReason, DepartureResolution
from .resolver import resolve_departure

__all__ = [
    "DepartureOutcome",
    "DepartureReason",
    "DepartureResolution",
    "resolve_departure",
]

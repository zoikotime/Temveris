"""
Temveris — the ECO day read contract and visibility surface (GV-17).
=====================================================================

ZT-SPEC-ECO-002 v2.0. The read side of §24.7's visibility requirement.

STATUS: IMPLEMENTED, NOT WIRED. No route, serializer or UI consults it.

    read_eco_day(db, caller=..., user_id=..., work_date=...)   -> EcoDayView
    render_eco_day(view, audience=...)                         -> RenderedEcoDay

WHAT §24.7 ASKS FOR, AND WHAT THIS PACKAGE IS
----------------------------------------------
    "The deferred state MUST be visible to both Worker and administrator so
     that a week which appears artificially low is explainable."

The ALERT/visibility audit established that this is discharged by the
WF-ECO-03 / WF-ECO-04 explanation surfaces (§28.1, §29), not by a notification:
§33.1's matrix has no deferral row, and §34 separates the explanation surface
from delivery. Those surfaces need three things:

    1. an authority saying WHO may read      -> app.eco_view_authority  (built)
    2. a contract saying WHAT is readable    -> this package, `service`
    3. approved copy saying HOW it reads     -> this package, `rendering`

WHERE THE COPY CAME FROM
------------------------
§28.1 locks the per-day Status vocabulary to
``Complete · Counted · Excluded · Under review · Deferred · Not a working day``
and cites §31 as its register — but §31 carries production copy for NONE of the
six, and no explanatory sentence for a deferred day exists anywhere in the
specification. That mismatch is a recorded specification defect.

Two strings were therefore authorized OUTSIDE §31 and are registered under the
authority they actually have:

    eco.status.deferred   "Deferred"       §28.1 owner authorization
    eco.deferred.body     the explanation  owner-authored production copy

Neither is §31 copy and neither is described as such. The remaining five §28.1
labels were explicitly NOT amended, so only a DEFERRED day is renderable today;
every other state renders ``status=None, body=None`` rather than an invented
label.

WHAT IT DELIBERATELY IS NOT
---------------------------
* **No HTTP surface.** No route, no FastAPI dependency, no schema.
* **No authored copy.** Both strings are quoted verbatim from the registers;
  nothing here composes, shortens or interpolates approved wording.
* **No second authorization rule.** It calls the B2 primitive and nothing else.
* **No new deferral vocabulary.** §24.6 and §24.7 stay distinguished by the
  ledger evidence that already distinguishes them.
* **No notification, recipient or reviewer.** ``Audience`` here selects which
  approved copy a SURFACE shows; it is not a notification audience, resolves
  nobody, and sends nothing. §34 keeps explanation and delivery separate.
* **No writes, no recomputation, no clock.**
"""

from .outcomes import EcoDayView
from .rendering import (
    Audience,
    ProhibitedTerminology,
    RenderedEcoDay,
    render_eco_day,
)
from .service import read_eco_day

__all__ = [
    "read_eco_day",
    "EcoDayView",
    # GV-17 visibility surface (WF-ECO-03 / WF-ECO-04)
    "render_eco_day",
    "Audience",
    "RenderedEcoDay",
    "ProhibitedTerminology",
]

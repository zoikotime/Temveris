"""
The departure-evidence vocabulary.
==================================

Owner Ruling 4. A dedicated primitive, deliberately NOT the working-day
classifier: that one answers *"was work expected?"* and its vocabulary is closed
at WORKING_DAY / NOT_A_WORKING_DAY / UNCLASSIFIED (Ruling 3). This one answers a
different question about an already-attributable day — *"did the Worker's exit
leave verified evidence?"* — and the two must not be merged.

WHY A SEPARATE COMPONENT AT ALL
-------------------------------
``evidence.resolve_session_evidence`` already establishes the FACTS: the close
boundary is verified, synthesized, or unrecognised. What it deliberately does
not do is say what the DAY becomes — its own comment records the seam:

    "A synthesized closure is provenance-bearing evidence but not a verified
     departure, so the producer does not seal it — and does not relabel it.
     The classifier owns what such a day becomes."

The producer may transport a verdict but may never originate one (D2-A, D13-A,
reaffirmed by Ruling 2). So the verdict needs an owner, and this is it.

THE SMALLEST VOCABULARY THAT WORKS
----------------------------------
Four outcomes. ``SYNTHESIZED_CLOSURE`` is deliberately NOT one of them: it is a
*reason* a day is missing its clock-out, not a different verdict, and promoting
it to an outcome would force the producer to decide that a synthesized close
means MISSING_CLOCK_OUT — exactly the semantic decision Ruling 2 forbids it.
The reason is carried alongside instead, so the ledger keeps a truthful record
of WHY without the producer interpreting anything.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional


class DepartureOutcome(str, enum.Enum):
    """What the day's departure evidence establishes."""

    VERIFIED_DEPARTURE = "VERIFIED_DEPARTURE"
    """Every attributable session closed with verified departure provenance.
    The ordinary sealing path applies and nothing here changes it."""

    MISSING_CLOCK_OUT = "MISSING_CLOCK_OUT"
    """The day reached its authorized closure point and no verified departure
    exists (§24.6). The exit is unresolved — it is NOT an early clock-out, and
    no departure time may be assumed."""

    SESSION_OPEN = "SESSION_OPEN"
    """A session is still open and the day has NOT reached closure. Ruling 5
    makes this distinct from MISSING_CLOCK_OUT: a Worker who is still working
    has not failed to clock out. No verdict yet; the producer's existing
    refusal lifecycle continues to apply."""

    UNVERIFIED_EXIT = "UNVERIFIED_EXIT"
    """An exit EXISTS on the record but was not performed by the Worker or an
    administrator — §24.7 "Unverified Exit — Pending Review".

    WHY THIS IS A FIFTH OUTCOME AND NOT A REASON ON ``INADMISSIBLE``
    ---------------------------------------------------------------
    The two demand opposite handling. ``INADMISSIBLE`` means *refuse the day*;
    this means *seal the day and defer its evaluation*. Folding them together
    would force the producer to read a sub-field in order to override the
    outcome it was given — which is the producer deciding, precisely what the
    architecture forbids it. A distinct outcome lets the producer transport a
    verdict it never has to interpret.

    WHAT IT IS NOT
    --------------
    NOT a ``LedgerState`` and NOT an ``EcoDayState``. Both vocabularies are
    unchanged: the ledger records this day as ``SEALED`` carrying its
    synthesized departure provenance, and the evaluation records it as
    ``DEFERRED_PENDING_LEDGER``. This name exists only inside the resolver, to
    say which of those two paths the evidence belongs on."""

    INADMISSIBLE = "INADMISSIBLE"
    """The evidence does not establish a verified departure and no authorized
    representation can carry what it does establish.

    Two shapes reach this:

    * evidence that cannot be read at all — a cross-tenant row, a naive
      timestamp, a non-positive interval;
    * a **synthesized closure**, which is an exit that exists but is
      unverified. That is §24.7 "Unverified Exit", NOT §24.6's missing
      clock-out, and §19.1 forbids reporting the one as the other. It lands
      here rather than in its own outcome because no ledger state can carry
      §24.7 into EVAL-DAY today — see the module docstring — and Ruling 11.F
      names the typed refusal as the correct behaviour until one exists.

    Always carries the existing typed refusal, so the producer's established
    handling is preserved exactly and nothing new is invented."""


class DepartureReason(str, enum.Enum):
    """Why a day is MISSING_CLOCK_OUT. Explanation, never a second verdict."""

    SESSION_OPEN_AFTER_CLOSURE = "SESSION_OPEN_AFTER_CLOSURE"
    """The local day ended with the session never closed. The Worker did not
    clock out; the platform must not decide when they left.

    The only reason there is, and deliberately so: §24.6's missing clock-out
    now has exactly one cause. A ``SYNTHESIZED_CLOSURE_ONLY`` member existed
    here briefly and was removed — a synthesized close is an unverified exit
    (§24.7), so it is not a reason for MISSING_CLOCK_OUT at all."""


@dataclass(frozen=True)
class DepartureResolution:
    """One verdict about one Worker-day's departure evidence.

    Frozen: a verdict a caller could edit is not a verdict.
    """

    outcome: DepartureOutcome
    reason: Optional[DepartureReason] = None
    #: The typed producer refusal, for INADMISSIBLE only.
    refusal: Optional[object] = None
    #: The session that decided the verdict, for explainability.
    session_id: Optional[int] = None
    detail: str = ""

    @property
    def is_verified(self) -> bool:
        return self.outcome is DepartureOutcome.VERIFIED_DEPARTURE

    @property
    def is_missing_clock_out(self) -> bool:
        return self.outcome is DepartureOutcome.MISSING_CLOCK_OUT

    @property
    def is_unverified_exit(self) -> bool:
        return self.outcome is DepartureOutcome.UNVERIFIED_EXIT


__all__ = [
    "DepartureOutcome",
    "DepartureReason",
    "DepartureResolution",
]

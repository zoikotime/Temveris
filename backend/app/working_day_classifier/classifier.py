"""
The two classification paths, deliberately separate (owner ruling D16-A).
=========================================================================

    classify_working_day(db, ...)      current configuration, at SEAL time
    classification_from_sealed(ledger) frozen context, for HISTORICAL evaluation

WHY TWO FUNCTIONS AND NOT ONE WITH A FLAG
-----------------------------------------
D3-A: later changes to schedules, assignments, organization settings or
holidays must not move a sealed day's classification, and an unavailable
historical classification must NOT silently resolve current mutable
configuration as though it were historical truth.

A single entry point with a ``use_frozen=True`` argument would put that
guarantee one forgotten keyword away from being lost. Two functions make the
mistake unavailable: ``classification_from_sealed`` cannot reach configuration
because it takes no session. There is nothing to pass it.

WHAT THIS MODULE DOES NOT DO
----------------------------
* **No new precedence.** D5-A ratified the resolver's existing hierarchy —
  assignment overriding the weekly pattern, and the pattern's source label
  inherited from whichever tier configured ``working_days``. This module reads
  that answer and re-codes nothing. D7-A: the ratified precedence resolves
  configuration-layer disagreement, so there is no conflict outcome here.
* **No Worker Context.** D18-A: the ratified source hierarchy does not include
  WEC, so nothing here imports it. D10-B leaves WEC admissible as a future
  classifier INPUT, but the contract does not require it, so no wiring exists.
* **No work evidence.** Not ``ClockSession``, not ``BreakSession``, not
  ``OfflineInterval``, not ``WorkStateInterval``, not leave, not any ECO
  measurement. D8-A/D17-A: absence of work is never evidence of a non-working
  day, and the only way to guarantee that is to be unable to see the work. An
  accidental clock-in on a rest day therefore cannot influence the answer.
* **No writes.** Pure resolution. The producer freezes the verdict; the
  classifier persists nothing.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import FrozenSet

from sqlalchemy.orm import Session

from .outcomes import (
    ClassificationOutcome,
    ClassificationRefusal,
    WorkingDayClassification,
)

#: The resolver source labels ruling D-18D admits, verbatim, now ratified for
#: ECO v2 by D5-A. Anything else — including the resolver's own
#: ``legacy_flexible_days`` — is refused as unknown rather than mapped onto one
#: of these, because "never infer a more specific source" forbids the mapping.
PERMITTED_WORKING_DAY_SOURCES: FrozenSet[str] = frozenset({
    "assignment", "shift", "employee", "legacy_user", "team", "org", "system",
})

#: The one permitted label that still refuses: it is in the resolver's
#: vocabulary, and it means nothing was configured. Permanently inadmissible
#: under D6-A.
UNRESOLVED_WORKING_DAY_SOURCE = "system"

#: D-2.1: the same resolver default, read for the SCHEDULED END. Unlike
#: the working-day label above this one does NOT refuse the day: schedule
#: variance is recorded and never drives the threshold (§10, §35), so a
#: missing shift end withdraws one recorded field rather than the day.
UNRESOLVED_SCHEDULED_END_SOURCE = "system"

#: Ledger states that carry a classification at all. Every other state is a
#: lifecycle or fault condition and is refused rather than read as one.
_CLASSIFIED_LEDGER_STATES = ("SEALED", "NOT_A_WORKING_DAY")


def _as_date(value) -> date:
    return value.date() if isinstance(value, datetime) else value


def classify_working_day(
    db: Session, *, organization_id: int, user, work_date: date
) -> WorkingDayClassification:
    """Classify a day from the CURRENT ratified schedule hierarchy.

    For a day being sealed now. The caller freezes the result; re-resolving it
    later for the same date is not the same operation and is not this function
    (see ``classification_from_sealed``).

    Deterministic for a given configuration: pure resolution over settings,
    roster and holidays, with no clock read and no dependence on iteration
    order.

    ``organization_id`` is the tenant the classification is requested FOR. The
    resolver derives the tenant from the Worker, so this is a consistency
    guard rather than a second lookup: a mismatch is a caller bug — a
    cross-tenant read — and raises rather than returning a classification for
    the wrong organization.
    """
    subject_org = getattr(user, "organization_id", None)
    if (organization_id is not None and subject_org is not None
            and int(subject_org) != int(organization_id)):
        raise ValueError(
            f"cross-tenant classification refused: user belongs to "
            f"organization {subject_org}, not {organization_id}"
        )

    # Deferred import to keep the module graph acyclic — the same idiom the
    # ledger and producer packages use for their own cross-package reads.
    from app.schedule_policy.resolver import resolve_work_schedule

    schedule = resolve_work_schedule(db, user, _as_date(work_date))
    source = (schedule.sources or {}).get("is_working_day")

    if not isinstance(source, str) or source not in PERMITTED_WORKING_DAY_SOURCES:
        return WorkingDayClassification(
            outcome=ClassificationOutcome.UNCLASSIFIED,
            refusal=ClassificationRefusal.SOURCE_UNKNOWN,
            detail=(
                f"the resolver reported source {source!r}, which is outside "
                f"the ratified vocabulary; it is refused rather than mapped "
                f"onto a permitted label"
            ),
        )
    if source == UNRESOLVED_WORKING_DAY_SOURCE:
        return WorkingDayClassification(
            outcome=ClassificationOutcome.UNCLASSIFIED,
            refusal=ClassificationRefusal.SOURCE_NOT_CONFIGURED,
            detail=(
                "no tier configured the working week, so the resolver returned "
                "its system default; D6-A makes that permanently inadmissible "
                "as an authoritative classification"
            ),
        )

    is_working_day = bool(schedule.is_working_day)

    # D-1.1: the scheduled end rides out of THIS resolution, not a second one.
    # The tier is carried verbatim and is NOT validated against
    # PERMITTED_WORKING_DAY_SOURCES: that vocabulary gates the working-day
    # determination, and D-2.1 deliberately does not extend its fail-closed
    # rule here. A "system" tier is recorded and the day still seals.
    end_source = (schedule.sources or {}).get("end_local")
    end_local = getattr(schedule, "end_local", None)
    if not isinstance(end_source, str) or not end_source.strip():
        # No tier stated at all: there is no provenance to record, so there is
        # no scheduled end to freeze. Absence, never a guess.
        end_source, end_local = None, None
    elif end_source == UNRESOLVED_SCHEDULED_END_SOURCE:
        # D-2.1: "system" means no tier configured a shift end. The tier is
        # recorded so a reader can tell "nobody configured one" from "nobody
        # asked"; the value itself is refused because the resolver's default
        # is not evidence of anything the Tenant chose.
        end_local = None

    return WorkingDayClassification(
        outcome=(ClassificationOutcome.WORKING_DAY if is_working_day
                 else ClassificationOutcome.NOT_A_WORKING_DAY),
        is_working_day=is_working_day,
        working_day_source=source,
        scheduled_end_local=end_local,
        scheduled_end_source=end_source,
    )


def classification_from_sealed(ledger) -> WorkingDayClassification:
    """The classification a SEALED ledger already carries. No session, no I/O.

    This is the historical path (D3-A). It reads only the frozen columns
    ``state``, ``is_working_day`` and ``working_day_source``, so a later change
    to any mutable configuration cannot move it, and there is no code path by
    which it could consult one.

    THE LEDGER STATE IS THE CARRIER, NOT THE BOOLEAN
    ------------------------------------------------
    D13-A seals a non-working day with ``LedgerState.NOT_A_WORKING_DAY``, so the
    state is what promotes a day into the zeroed path. D4-A is deliberately
    one-directional: ``is_working_day=False`` does NOT imply the state, because
    a ``SEALED`` row may legitimately carry ``False`` — a day sealed before
    D13-A, or by a writer that recorded the context without classifying it.

    So the OUTCOME follows the state, not the column: a ``SEALED`` row is
    reported ``WORKING_DAY`` and evaluated normally rather than retroactively
    zeroed, even when its frozen boolean is ``False``. That disagreement is
    reported in ``detail`` rather than acted on, because acting on it would be
    exactly the reverse implication D4-A withholds.
    """
    state = getattr(ledger, "state", None)
    if state not in _CLASSIFIED_LEDGER_STATES:
        return WorkingDayClassification(
            outcome=ClassificationOutcome.UNCLASSIFIED,
            refusal=ClassificationRefusal.NOT_SEALED,
            detail=(
                f"ledger state {state!r} carries no classification; a "
                f"lifecycle or fault condition is never read as one"
            ),
        )

    source = getattr(ledger, "working_day_source", None)
    if not isinstance(source, str) or not source.strip():
        return WorkingDayClassification(
            outcome=ClassificationOutcome.UNCLASSIFIED,
            refusal=ClassificationRefusal.CONTEXT_MISSING,
            detail=(
                "the sealed row carries no working_day_source; the frozen "
                "context is the authoritative history (D3-A), so an "
                "incomplete one is refused rather than re-resolved"
            ),
        )

    if state == "NOT_A_WORKING_DAY":
        # D4-A holds in this direction, and the ledger service enforces it, so
        # a row reaching here with True is a contract violation upstream rather
        # than something to reinterpret.
        if bool(getattr(ledger, "is_working_day", False)):
            return WorkingDayClassification(
                outcome=ClassificationOutcome.UNCLASSIFIED,
                refusal=ClassificationRefusal.CONTEXT_MISSING,
                detail=(
                    "NOT_A_WORKING_DAY with is_working_day=True violates the "
                    "D4-A invariant; the row is refused rather than trusted"
                ),
            )
        return WorkingDayClassification(
            outcome=ClassificationOutcome.NOT_A_WORKING_DAY,
            is_working_day=False,
            working_day_source=source,
            scheduled_end_local=getattr(ledger, "scheduled_end_local", None),
            scheduled_end_source=getattr(ledger, "scheduled_end_source", None),
        )

    frozen = bool(getattr(ledger, "is_working_day", True))
    return WorkingDayClassification(
        outcome=ClassificationOutcome.WORKING_DAY,
        is_working_day=True,
        working_day_source=source,
        # D-1.1 / D3-A: read from the frozen columns only. A row sealed before
        # they existed carries None, and no current configuration is consulted
        # to fill it — that absence is the history, not a gap to repair.
        scheduled_end_local=getattr(ledger, "scheduled_end_local", None),
        scheduled_end_source=getattr(ledger, "scheduled_end_source", None),
        detail=(
            None if frozen else
            "SEALED row carries is_working_day=False; evaluated as a working "
            "day because D4-A withholds the reverse implication and the state "
            "is the carrier"
        ),
    )


__all__ = [
    "PERMITTED_WORKING_DAY_SOURCES",
    "UNRESOLVED_WORKING_DAY_SOURCE",
    "UNRESOLVED_SCHEDULED_END_SOURCE",
    "classify_working_day",
    "classification_from_sealed",
]

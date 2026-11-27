"""
Departure resolution — does this Worker-day have a verified exit?
=================================================================

    resolve_departure(db, organization_id=..., user_id=..., context=...,
                      day_closed=...)

Owner Ruling 4. One question, answered from authoritative boundary provenance
and nothing else.

WHAT IT NEVER DOES
------------------
No working-day classification (Ruling 3 keeps that vocabulary closed). No
schedule read, no requirement, no shortfall, no week, no notification. It never
writes. And it NEVER produces a departure instant: §24.6 — "The system MUST
NEVER assume a departure time" — and §7.2 calls inferring an end of day "the
single worst thing this control could do". There is no code path here that can
return one, because the outcome type has no field to carry one.

THE CLOSURE POINT IS BORROWED, NOT INVENTED
-------------------------------------------
Ruling 5 forbids inventing a timeout, grace period or cutoff, and none is
invented: ``day_closed`` is supplied by the caller, which derives it from the
producer's existing **D18-F3 gate 1** — ``now_utc >= context.day_end_utc``,
where ``day_end_utc`` is the exclusive next LOCAL midnight from
``_tz.local_day_bounds``. That gate is already authorized, already deterministic
and already DST-correct. Taking it as a parameter rather than reading a clock
here is what keeps this function pure over its inputs.

    day not closed + session open   ->  SESSION_OPEN        (still working)
    day closed     + session open   ->  MISSING_CLOCK_OUT   (never clocked out)

That is the whole of Ruling 5's distinction, and it is decided by one boolean
rather than by anything this module measures.

WHICH SESSIONS COUNT
--------------------
Only sessions ATTRIBUTABLE to this day — clock-in inside the day's local
bounds, the same START_DATE rule the producer seals under (§12.3). A session
opened on an earlier day and left open belongs to THAT day's departure
question, not this one; treating it as this day's missing clock-out would
re-home a defect onto an innocent date. The producer keeps its existing
``SESSION_STILL_OPEN`` refusal for that case.

HOW §24.7 IS CARRIED, WITHOUT A NEW STATE
-----------------------------------------
An unverified exit reaches ``DEFERRED_PENDING_LEDGER`` without either
vocabulary growing a member. The evidence itself carries the distinction:

    verified departure      SEALED + WORKER/ADMIN_OVERRIDE  -> normal evaluation
    unverified exit         SEALED + synthesized provenance -> DEFERRED (§24.7)
    no clock-out at all     MISSING_CLOCK_OUT, departure NULL -> DEFERRED (§24.6)

Both deferred paths end at the same ECO state, as §24.7 and §24.6 both require
— but they remain **distinguishable at the ledger layer**, which is what §19.1
protects. A day with a recorded departure instant is never described as
"missing" its clock-out, and a day with none never claims an exit it does not
have.

EVAL-DAY makes the deferral decision by reading that frozen evidence, because
§19 places step 6 inside ``evaluateDay``. This module only says which kind of
evidence the day has.

ONE BAD SESSION DECIDES THE DAY
-------------------------------
If any attributable session lacks a verified departure, the day has no verified
departure. The producer already refuses a whole day on one inadmissible session
— "a partial day would be evidence that silently omits work" — and the same
reasoning applies here: sealing the closed sessions and dropping the open one
would publish a day that silently omits the unresolved exit.
"""

from __future__ import annotations

from typing import List

from sqlalchemy.orm import Session

from database import models

from .outcomes import DepartureOutcome, DepartureReason, DepartureResolution


def _attributable_sessions(db: Session, *, organization_id, user_id, context) -> List:
    """Sessions whose clock-in falls inside this organization-local day.

    START_DATE attribution, the same rule the producer selects under, so the
    two cannot disagree about which day a session belongs to.
    """
    return (
        db.query(models.ClockSession)
        .filter(
            models.ClockSession.organization_id == organization_id,
            models.ClockSession.user_id == user_id,
            models.ClockSession.clock_in_time >= context.day_start_utc,
            models.ClockSession.clock_in_time < context.day_end_utc,
        )
        .order_by(models.ClockSession.clock_in_time, models.ClockSession.id)
        .all()
    )


def resolve_departure(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    context,
    day_closed: bool,
) -> DepartureResolution:
    """The departure-evidence verdict for one attributable Worker-day.

    Pure over its inputs: it reads rows, reads no clock, and writes nothing.
    ``day_closed`` carries the authorized closure point (D18-F3 gate 1) rather
    than being measured here.
    """
    # Deferred import — the producer imports this module, so a module-level
    # import back into it would close the cycle. Same idiom
    # ``work_ledger.workweek`` already uses for its cross-package read. The
    # evidence layer is reused rather than reimplemented so the two can never
    # disagree about what "verified" means.
    from app.work_ledger_producer.evidence import resolve_session_evidence
    from app.work_ledger_producer.outcomes import RefusalReason

    sessions = _attributable_sessions(
        db, organization_id=organization_id, user_id=user_id, context=context)

    for session in sessions:
        resolved = resolve_session_evidence(
            db, session=session, organization_id=organization_id,
            user_id=user_id)

        if not isinstance(resolved, RefusalReason):
            continue                      # verified open AND verified close

        if resolved is RefusalReason.SESSION_OPEN:
            # The one place Ruling 5's distinction is made.
            if not day_closed:
                return DepartureResolution(
                    outcome=DepartureOutcome.SESSION_OPEN,
                    session_id=int(session.id),
                    detail=(f"clock_session:{session.id} is open and the local "
                            f"day has not reached its closure point"),
                )
            return DepartureResolution(
                outcome=DepartureOutcome.MISSING_CLOCK_OUT,
                reason=DepartureReason.SESSION_OPEN_AFTER_CLOSURE,
                session_id=int(session.id),
                detail=(f"clock_session:{session.id} was never closed and the "
                        f"local day has ended; no departure is assumed (§24.6)"),
            )

        # ── §24.7, NOT §24.6 ────────────────────────────────────────────────
        # A synthesized close is an exit that EXISTS and is unverified, which
        # is §24.7 "Unverified Exit — Pending Review", not §24.6's missing
        # clock-out. §19 runs step 6 before step 7 precisely so the first
        # cannot be mislabelled as the second, and §19.1 states it outright:
        # "A deferred day is not a missing clock-out and must not be reported
        # as one."
        #
        # An earlier version returned MISSING_CLOCK_OUT here, which contradicted
        # the evidence — the day has a recorded clock-out instant and a recorded
        # boundary event. A later one returned INADMISSIBLE, which was truthful
        # but recorded nothing, because no ledger representation existed yet.
        # The owner has now authorised one, so the condition gets its own
        # verdict and the day is sealed and deferred rather than dropped.
        if resolved is RefusalReason.SESSION_SYNTHESIZED_CLOSURE:
            return DepartureResolution(
                outcome=DepartureOutcome.UNVERIFIED_EXIT,
                refusal=resolved,
                session_id=int(session.id),
                detail=(f"clock_session:{session.id} closed with synthesized "
                        f"provenance: an unverified exit (§24.7), never a "
                        f"verified departure and never a missing clock-out"),
            )

        # Everything else keeps its existing typed refusal, unchanged.
        return DepartureResolution(
            outcome=DepartureOutcome.INADMISSIBLE,
            refusal=resolved,
            session_id=int(session.id),
            detail=f"clock_session:{session.id}:{resolved.value}",
        )

    return DepartureResolution(
        outcome=DepartureOutcome.VERIFIED_DEPARTURE,
        detail=f"{len(sessions)} attributable session(s), all verified",
    )


__all__ = ["resolve_departure"]

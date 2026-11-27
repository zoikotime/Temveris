"""
The GV-17 visibility surface — HTTP read seam for WF-ECO-03 / WF-ECO-04.
=========================================================================

    GET /api/eco/day?user_id=<int>&work_date=<YYYY-MM-DD>

One read-only route. It authenticates, asks the authority, honours rollout,
reads the persisted day, renders it, and returns. It computes nothing.

THE ORDER OF THE GATES IS THE DESIGN
-------------------------------------
    1. authenticate            (existing ``get_current_user``)
    2. authority               -> 403 on any denial
    3. rollout                 -> 404 where §44 forbids the surface
    4. persisted record        -> 404 when the day has none
    5. render                  -> 200

Authority is OUTERMOST on purpose. A caller with no business reading this
Worker's day is refused before the rollout state or the existence of a record
is consulted at all, so neither can leak to them.

WHY ROLLOUT GATES A READ SURFACE, AND ASYMMETRICALLY
-----------------------------------------------------
§44.2: shadow mode is "full evaluation, full event emission, **no notifications
and no Worker-facing surfaces**. The Tenant administrator sees what would have
happened." §44.1 says the same of P0: "No Worker-facing surface. Runs in
shadow."

So the gate is not symmetric, and that asymmetry IS the specification:

    OFF       nobody                     — owner Ruling A
    SHADOW    administrator only         — §44.2, both halves of it
    ENABLED   Worker and administrator

Shadow exists so an administrator can see what would have happened before a
Worker ever does. Blocking them too would make shadow mode pointless; showing
the Worker would contradict §44.2 outright.

WHY THE UNAVAILABLE ANSWER IS 404
----------------------------------
The project has no feature-gate convention to follow — 402 is billing
suspension, 503 is a database outage, and ECO v1's disabled path silently
skips rather than answering. Rather than invent a fourth, this reuses the
"no day here" answer the authorized-but-no-record case already returns.

That is also the only answer that satisfies owner Ruling A's non-disclosure
requirement: under OFF the response must not reveal whether an ECO record
exists, and a 404 is indistinguishable from the ordinary no-record 404. A
distinct code would announce "the feature is off, and something is behind it".

WHAT IT NEVER DOES
------------------
No evaluation, no recomputation, no writes, no notifications, no rollout
mutation, no legacy-attendance fallback, and no second authorization rule. It
accepts no ``organization_id`` — the tenant comes from the authenticated
caller — and no ``audience``: that is derived from the authority decision, so
a client cannot ask to be treated as an administrator.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.auth.service import get_current_user
from app.eco_view_authority import (
    ViewAuthorityOutcome,
    require_eco_view_authority,
)
from app.rollout import CONTROL_EARLY_CLOCK_OUT_V2, RolloutState, rollout_state
from database import models
from database.database import get_db

from .rendering import Audience, render_eco_day
from .service import read_eco_day

router = APIRouter(prefix="/api/eco", tags=["eco"])

#: Which rendering audience each authority outcome earns. Derived from the
#: DECISION, never from the request — a client cannot ask to be an
#: administrator, and there is no parameter through which to try.
_AUDIENCE_FOR_OUTCOME = {
    ViewAuthorityOutcome.WORKER_SELF: Audience.WORKER,
    ViewAuthorityOutcome.TENANT_ADMINISTRATOR: Audience.ADMINISTRATOR,
}

#: §44.2 / owner Ruling A. Which audiences may see the surface in each state.
_VISIBLE_AUDIENCES = {
    RolloutState.OFF: frozenset(),
    RolloutState.SHADOW: frozenset({Audience.ADMINISTRATOR}),
    RolloutState.ENABLED: frozenset({Audience.WORKER, Audience.ADMINISTRATOR}),
}

_NOT_AVAILABLE = "No Early Clock-Out record is available for that day."


def _not_available() -> HTTPException:
    """The single unavailable answer, identical for every cause.

    Rollout OFF, shadow-blocked Worker, and a day with no record all return
    this. They are deliberately indistinguishable: owner Ruling A forbids
    revealing whether a record exists, and a per-cause message would.
    """
    return HTTPException(status_code=404, detail=_NOT_AVAILABLE)


@router.get("/day")
def get_eco_day(
    user_id: int = Query(..., description="The Worker whose day is read"),
    work_date: str = Query(..., description="Calendar date, YYYY-MM-DD"),
    db: Session = Depends(get_db),
    caller: models.User = Depends(get_current_user),
):
    """One Worker-day's recorded ECO state and its approved rendering."""
    try:
        on = datetime.strptime(work_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    # ── 2. Authority, outermost ─────────────────────────────────────────────
    # The typed denial reason is deliberately NOT returned: it distinguishes
    # "no grant" from "that Worker is in another tenant", which would confirm a
    # Worker's existence across a tenant boundary to anyone who guessed an id.
    decision = require_eco_view_authority(
        db, caller=caller, target_user_id=user_id)
    if not decision.is_authorized:
        raise HTTPException(
            status_code=403,
            detail="You do not have access to this working-time record.")

    audience = _AUDIENCE_FOR_OUTCOME[decision.outcome]

    # ── 3. Rollout, from the durable control and the caller's own tenant ────
    state = rollout_state(
        db,
        organization_id=decision.organization_id,
        control_key=CONTROL_EARLY_CLOCK_OUT_V2,
    )
    if audience not in _VISIBLE_AUDIENCES[state]:
        raise _not_available()

    # ── 4. The persisted day ────────────────────────────────────────────────
    view = read_eco_day(db, caller=caller, user_id=user_id, work_date=on)
    if view is None:
        raise _not_available()

    # ── 5. Render and answer ────────────────────────────────────────────────
    rendered = render_eco_day(view, audience=audience)
    return {
        "work_date": view.work_date.isoformat(),
        # Rendering — approved copy, quoted. Null where the state has no
        # approved label; never a placeholder.
        "status": rendered.status,
        "body": rendered.body,
        "has_explanation": rendered.has_explanation,
        "copy_keys": list(rendered.copy_keys),
        # Evaluation identity, as recorded.
        "evaluation_state": view.evaluation_state,
        "evaluation_generation": view.evaluation_generation,
        "ledger_version": view.ledger_version,
        "policy_version": view.policy_version,
        "config_version": view.config_version,
        "leave_input_snapshot_key": view.leave_input_snapshot_key,
        # Measurements, as recorded. All null together on a deferred day —
        # that is the recorded fact, not a lookup that failed.
        "required_minutes": view.required_minutes,
        "qualifying_minutes": view.qualifying_minutes,
        "gross_shortfall_minutes": view.gross_shortfall_minutes,
        "exclusion_minutes": view.exclusion_minutes,
        "attributable_minutes": view.attributable_minutes,
        "schedule_variance_minutes": view.schedule_variance_minutes,
        "contributes_to_accumulator_minutes":
            view.contributes_to_accumulator_minutes,
        "shortfall_computed": view.shortfall_computed,
    }


__all__ = ["router"]

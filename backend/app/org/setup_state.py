"""Setup-wizard state for a newly provisioned organization.

WHAT THIS IS NOT
----------------
It is deliberately *not* stored on ``OnboardingChecklist``. That table is the
commercial activation/telemetry tracker — six independent, self-attested
booleans plus time-to-value milestones, read by the funnel reporting. Wizard
progress is a different fact with different semantics (ordered, resumable,
terminal), so overloading those booleans would corrupt both meanings.

WHY NO NEW TABLE OR COLUMN
--------------------------
Schema in this repo is managed three ways — Alembic, boot-time
``ensure_tables()``/``create_all(checkfirst=True)``, and ``ADD COLUMN IF NOT
EXISTS`` ALTERs — and the migration history currently has several concurrent
heads. Introducing DDL for two small values would add drift for no benefit.

Instead this reuses ``app_settings``: the existing org-scoped key/value store,
already used for org-level state both by the settings cascade and directly by
feature code (``org/router.py`` reads ``integrations.state_json`` from it the
same way). Keys live outside the settings router's ``DEFAULTS`` allow-list on
purpose, so wizard state never appears as a user-facing toggle in the generic
Organization Settings UI.

TENANT SAFETY
-------------
Every read and write is scoped by the ``organization_id`` derived from the
authenticated user. No function here accepts an organization id from a caller.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from database import models

_log = logging.getLogger("zoiko.org.setup_state")

KEY_CURRENT_STEP = "onboarding.setup_current_step"
KEY_COMPLETED_AT = "onboarding.setup_completed_at"

STEP_ORGANIZATION = "organization"
STEP_TEAMS = "teams"
STEP_COMPLETE = "complete"

# Ordered, so the wizard can resume and so a stale client cannot walk backwards
# and re-run a completed step.
STEP_ORDER = (STEP_ORGANIZATION, STEP_TEAMS, STEP_COMPLETE)


def _row(db: Session, organization_id: int, key: str) -> Optional[models.AppSettings]:
    """Fetch an org-level row. Org level == team_key IS NULL AND user_id IS NULL."""
    return (
        db.query(models.AppSettings)
          .filter(
              models.AppSettings.organization_id == organization_id,
              models.AppSettings.key == key,
              models.AppSettings.team_key.is_(None),
              models.AppSettings.user_id.is_(None),
          )
          .first()
    )


def _put(db: Session, organization_id: int, key: str, value: str) -> None:
    row = _row(db, organization_id, key)
    if row is None:
        db.add(models.AppSettings(
            organization_id=organization_id,
            key=key,
            value=value,
            description="Self-service setup wizard state",
        ))
    else:
        row.value = value
    db.flush()


def begin(db: Session, organization_id: int) -> dict:
    """Open the wizard for a freshly provisioned tenant.

    Called by self-serve signup. The PRESENCE of a step row is what marks a
    tenant as being in setup — see :func:`get_state` for why that direction
    matters.
    """
    if _row(db, organization_id, KEY_COMPLETED_AT) is not None:
        return get_state(db, organization_id)
    if _row(db, organization_id, KEY_CURRENT_STEP) is None:
        _put(db, organization_id, KEY_CURRENT_STEP, STEP_ORGANIZATION)
    return get_state(db, organization_id)


def get_state(db: Session, organization_id: Optional[int]) -> dict:
    """Return ``{current_step, setup_required, completed, completed_at}``.

    The default direction is the important design decision here: **absence of
    state means "not in setup", never "needs setup"**.

    Every organization that already existed when this feature shipped — every
    super-admin-provisioned tenant, and every self-serve tenant created before
    the wizard — has no state row. If a missing row implied "start at step 1",
    the first-login router would drop working admins into a setup flow for an
    organization they have been running for months. Requiring an explicit
    :func:`begin` call instead means only tenants that genuinely started the
    wizard can ever be sent to it.

    An org-less caller gets ``setup_required=False``: deciding what to do with
    them is the routing layer's job, and this module must not become a second
    place that defines that policy.
    """
    if organization_id is None:
        return {
            "current_step": STEP_ORGANIZATION,
            "setup_required": False,
            "completed": False,
            "completed_at": None,
        }

    completed_row = _row(db, organization_id, KEY_COMPLETED_AT)
    completed_at = completed_row.value if completed_row else None
    if completed_at:
        return {
            "current_step": STEP_COMPLETE,
            "setup_required": False,
            "completed": True,
            "completed_at": completed_at,
        }

    step_row = _row(db, organization_id, KEY_CURRENT_STEP)
    if step_row is None:
        # Pre-existing tenant — never route it into the wizard.
        return {
            "current_step": STEP_COMPLETE,
            "setup_required": False,
            "completed": False,
            "completed_at": None,
        }

    step = step_row.value or STEP_ORGANIZATION
    if step not in STEP_ORDER:
        # Never trust a stored value blindly; fall back to the start.
        _log.warning(
            "Unknown setup step %r for org %s — treating as %s",
            step, organization_id, STEP_ORGANIZATION,
        )
        step = STEP_ORGANIZATION
    return {
        "current_step": step,
        "setup_required": True,
        "completed": False,
        "completed_at": None,
    }


def set_step(db: Session, organization_id: int, step: str) -> dict:
    """Record wizard progress. Never moves backwards, never un-completes.

    Monotonic on purpose: a refresh, a back-navigation or a duplicate submit
    must not rewind a tenant that has already progressed.
    """
    if step not in STEP_ORDER:
        raise ValueError(f"unknown setup step: {step!r}")

    state = get_state(db, organization_id)
    if state["completed"]:
        return state

    current_index = STEP_ORDER.index(state["current_step"])
    new_index = STEP_ORDER.index(step)
    if new_index <= current_index:
        return state

    if step == STEP_COMPLETE:
        return mark_complete(db, organization_id)

    _put(db, organization_id, KEY_CURRENT_STEP, step)
    return get_state(db, organization_id)


def mark_complete(db: Session, organization_id: int) -> dict:
    """Terminal. Idempotent — the first completion timestamp is kept."""
    state = get_state(db, organization_id)
    if state["completed"]:
        return state
    now = datetime.now(timezone.utc).isoformat()
    _put(db, organization_id, KEY_CURRENT_STEP, STEP_COMPLETE)
    _put(db, organization_id, KEY_COMPLETED_AT, now)
    return {
        "current_step": STEP_COMPLETE,
        "setup_required": False,
        "completed": True,
        "completed_at": now,
    }


def setup_required(db: Session, organization_id: Optional[int]) -> bool:
    return bool(get_state(db, organization_id)["setup_required"])

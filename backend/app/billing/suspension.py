"""Organization-level read-only enforcement for unpaid accounts.

This deliberately reuses the application's existing authorization mechanism rather
than adding a parallel one. Authorization in Temveris is FastAPI *dependencies*, not
middleware — ``get_current_user`` is a single choke point that all 372 authenticated
endpoints already depend on, and it already refuses a deactivated user. The billing
gate is checked in exactly that place, so every endpoint inherits it without 372
signature changes and without a second, competing rule engine.

What "read-only" means (approved 2026-08-13):

* **Reads always work.** GET / HEAD / OPTIONS are never blocked. Nobody loses sight of
  their data because of a billing problem.
* **Clock-in, clock-out, breaks and desktop tracking keep working** — explicitly
  required. Suspension must not stop an employee recording the hours they worked;
  that would turn a billing dispute into a payroll dispute.
* **Billing and auth stay open** so the customer can actually pay and recover, and so
  sessions can still be established to do it.
* **Everything else that writes is refused with 402 Payment Required** — adding or
  removing people, editing settings, administrative actions.

Nothing is deleted and the Stripe subscription is not cancelled. Paying restores
everything immediately, because suspension is *derived* from
``BillingSubscription.dunning_started_at`` rather than stored as its own flag.

Internal tenants can never be suspended: ``dunning.is_suspended`` refuses them, the
dunning sweep excludes them in SQL, and ``dunning.begin`` will not start a clock.
"""

from __future__ import annotations

import logging
import time
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from database import models

_log = logging.getLogger("zoiko.billing.suspension")

# Methods that only read. Never blocked.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Writes that must keep working while suspended. Matched as path prefixes against the
# real mounted routes (verified against main.py's include_router calls).
ALLOWED_WRITE_PREFIXES: Tuple[str, ...] = (
    # Payment recovery — the whole point of read-only is that the customer can pay.
    "/api/admin/billing",
    # Sign-in, refresh, logout, SSO. Without these nobody could log in to pay.
    "/auth",
    # Attendance and desktop tracking. Employees must be able to record worked time
    # regardless of their employer's billing status.
    "/employees/clock-in",
    "/employees/clock-out",
    "/employees/start-break",
    "/employees/resume-work",
    "/employees/network-pause",
    "/tracking",
    # Public / unauthenticated surfaces that happen to POST.
    "/notifications/unsubscribe",
)

_MESSAGE = (
    "This workspace is read-only because a subscription payment is outstanding. "
    "Viewing data, clocking in and time tracking continue to work. Update the "
    "payment method in Billing to restore full access."
)

# Suspension is looked up per request, so it is cached briefly. 30s means recovery is
# visible almost immediately while an unpaid tenant is not re-queried on every call.
_CACHE_TTL_SECONDS = 30
_cache: dict = {}


def invalidate_cache(org_id: Optional[int] = None) -> None:
    """Drop cached suspension state. Called after payment recovery so access returns
    at once rather than after the TTL."""
    if org_id is None:
        _cache.clear()
    else:
        _cache.pop(int(org_id), None)


def _cached_suspended(db: Session, org_id: int) -> bool:
    now = time.monotonic()
    hit = _cache.get(org_id)
    if hit is not None and hit[1] > now:
        return hit[0]

    from app.billing import dunning as billing_dunning        # noqa: PLC0415

    sub = (
        db.query(models.BillingSubscription)
          .filter(models.BillingSubscription.organization_id == org_id)
          .first()
    )
    suspended = bool(sub) and billing_dunning.is_suspended(db, sub)
    _cache[org_id] = (suspended, now + _CACHE_TTL_SECONDS)
    return suspended


def is_write_allowed_path(path: str) -> bool:
    return any(path.startswith(p) for p in ALLOWED_WRITE_PREFIXES)


def check_request(db: Session, user: models.User, method: str, path: str) -> None:
    """Raise 402 when a suspended organization attempts a restricted write.

    Called from ``app.auth.service.get_current_user``. Fails *open* on any internal
    error: a bug in billing enforcement must never lock a paying customer out of their
    own workspace.
    """
    try:
        if (method or "").upper() in SAFE_METHODS:
            return
        if is_write_allowed_path(path or ""):
            return
        org_id = getattr(user, "organization_id", None)
        if org_id is None:
            return
        # SAVEPOINT, matching app/schedule_policy/resolver.py. Failing open is not
        # enough on its own: a failed statement poisons the session, so on PostgreSQL
        # the transaction enters InFailedSqlTransaction and every later query in the
        # request dies with "current transaction is aborted" — a skipped billing check
        # would take the whole request down with it. That is the shape of the July
        # incident.
        #
        # begin_nested rather than db.rollback() for the reason resolver.py gives: a
        # full rollback would discard uncommitted work the CALLER owns, which is not
        # this check's to throw away. The savepoint undoes only the failed read.
        with db.begin_nested():
            suspended = _cached_suspended(db, int(org_id))
        if not suspended:
            return
    except Exception as exc:                              # pragma: no cover - safety net
        _log.error("suspension check failed (allowing request): %s", exc)
        return

    from fastapi import HTTPException                     # noqa: PLC0415
    _log.info("billing-suspended write refused: org=%s %s %s",
              getattr(user, "organization_id", None), method, path)
    raise HTTPException(status_code=402, detail=_MESSAGE)


def status_for_org(db: Session, org_id: Optional[int]) -> dict:
    """Suspension summary for the billing UI."""
    from app.billing import dunning as billing_dunning        # noqa: PLC0415

    if org_id is None:
        return {"suspended": False}
    sub = (
        db.query(models.BillingSubscription)
          .filter(models.BillingSubscription.organization_id == org_id)
          .first()
    )
    if sub is None:
        return {"suspended": False}
    expires = billing_dunning.grace_expires_at(sub)
    return {
        "suspended": billing_dunning.is_suspended(db, sub),
        "dunning_started_at": sub.dunning_started_at.isoformat() if sub.dunning_started_at else None,
        "grace_expires_at": expires.isoformat() if expires else None,
        "days_overdue": billing_dunning.age_days(sub),
        "read_only_message": _MESSAGE,
    }

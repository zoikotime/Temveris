"""Organization lifecycle — the single enforcement point for tenant access.

WHY THIS EXISTS
---------------
``CommercialOrganization.commercial_state`` has always had a ``"suspended"``
value, and the Super Admin portal has always been able to write it. Nothing
read it for access control: an audit of every authentication surface
(``login``, ``login_json``, ``refresh``, ``get_current_user``,
``get_current_organization``, SSO, the WebSocket handshake) found not one org
lookup. Suspension was a label on a row.

This module is that missing read. It is deliberately ONE module with ONE
question — "may this tenant operate right now?" — so the answer cannot drift
between call sites. Nothing here scatters ``if state == "suspended"`` through
the routers; the check is applied at the two chokepoints every request already
passes through (see ENFORCEMENT POINTS below).

NO NEW STATE FIELD
------------------
The existing schema already expresses this, so no migration is added:

  * ``CommercialOrganization.commercial_state`` — the authoritative lifecycle
    value. ``"suspended"`` is already a member of ``constants.COMMERCIAL_STATES``
    and is already written by the Super Admin portal, the commercial-admin
    console, and the dunning job.
  * ``Organization.is_active`` — the operational mirror. The column existed but
    was never read by anything; suspension now writes it and this module reads
    it, so a tenant with no commercial row (legacy/imported) is still coverable.

Either one being "off" denies access. Both are kept in sync by the Super Admin
suspend/restore handlers.

WHAT IS *NOT* A LIFECYCLE BLOCK
-------------------------------
Billing states are NOT access states. ``past_due``, ``grace_period``,
``trial_expired``, ``cancelled`` and friends live on the same column but are
the subscription state machine's business (``app.commercial.state_machines``),
and gating login on them would silently change billing behaviour and lock out
paying customers mid-dunning. Only the explicit operator action — ``suspended``
— is an access boundary. The dunning job's day-14 escalation writes exactly
that value, so non-payment enforcement now works too, but as a consequence of
the operator-visible state rather than a second, hidden rule.

ENFORCEMENT POINTS
------------------
  1. ``app.auth.service.establish_session`` — covers login, login-json,
     set-password/activation and both SSO callbacks in one place, because all
     five mint their session through it.
  2. ``app.auth.service.get_current_user`` — covers EVERY authenticated request,
     so an already-issued token stops working. This is what makes suspension
     immediate rather than "on next login".
  3. ``app.auth.service.refresh`` — stops the 30-day rotating refresh window
     from silently re-arming a suspended tenant.

WHO IS EXEMPT
-------------
  * Super admins. They are platform operators, not tenants; ``organization_id``
    is forced to NULL on promotion (``_maybe_promote_super_admin``), so they
    fall out of the check naturally. The explicit role test is belt-and-braces.
  * Impersonation sessions. A token minted by ``POST /api/super-admin/impersonate``
    carries an ``impersonated_by`` claim; support must be able to look inside a
    suspended tenant, which is most of the point of suspending it. Every action
    is still attributed to the impersonated user in the audit trail.

CACHING
-------
``get_current_user`` serves most requests from a 5-minute in-process user cache
without touching the database, so a naive lookup here would have added a query
to every request. State is cached per organization with a SHORT ttl and an
explicit invalidation hook:

  * ``_STATE_TTL`` (default 30 s) bounds how long ANOTHER process can serve a
    stale answer — in-process invalidation cannot reach sibling workers.
  * ``invalidate()`` makes the change instant in the process that performed it.

30 s was chosen over the 300 s used by the user/org caches precisely because
this value DOES drive an authorization decision, which the comment on
``_ORG_CACHE`` is careful to say its own cached fields do not.
"""

from __future__ import annotations

import logging
import os
import threading
import time as _time
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from database.models import Organization, User

_log = logging.getLogger("zoiko.org.lifecycle")


# ──────────────────────────────────────────────────────────────────────────
# States
# ──────────────────────────────────────────────────────────────────────────
STATE_ACTIVE = "active"
STATE_SUSPENDED = "suspended"

#: The only ``commercial_state`` value that denies tenant access. See the
#: "WHAT IS NOT A LIFECYCLE BLOCK" note in the module docstring before adding
#: anything here — billing states must not silently become access states.
BLOCKING_COMMERCIAL_STATES = frozenset({"suspended"})

#: Error codes returned to clients. The frontend switches on these; they are
#: part of the API contract.
ERR_ORG_SUSPENDED = "organization_suspended"
ERR_ORG_MISSING = "organization_not_found"

SUSPENDED_MESSAGE = (
    "Your organization is currently suspended. Please contact your administrator."
)


# ──────────────────────────────────────────────────────────────────────────
# Per-organization state cache
# ──────────────────────────────────────────────────────────────────────────
def _ttl_seconds() -> int:
    raw = (os.getenv("ORG_LIFECYCLE_CACHE_TTL") or "").strip()
    if not raw:
        return 30
    try:
        return max(0, int(raw))
    except ValueError:
        _log.warning("ORG_LIFECYCLE_CACHE_TTL=%r is not an integer — using 30s", raw)
        return 30


_STATE_CACHE: dict = {}
_STATE_LOCK = threading.Lock()


def _cache_get(org_id: int) -> Optional[bool]:
    entry = _STATE_CACHE.get(org_id)
    if entry and _time.monotonic() < entry["expires"]:
        return entry["suspended"]
    return None


def _cache_set(org_id: int, suspended: bool) -> None:
    ttl = _ttl_seconds()
    if ttl <= 0:
        return
    with _STATE_LOCK:
        _STATE_CACHE[org_id] = {
            "suspended": suspended,
            "expires": _time.monotonic() + ttl,
        }


def invalidate(org_id: Optional[int] = None) -> None:
    """Drop cached state so the next check re-reads the database.

    Call after any write that changes whether a tenant may operate — suspend,
    restore, delete. ``org_id=None`` clears everything (used by tests).
    """
    with _STATE_LOCK:
        if org_id is None:
            _STATE_CACHE.clear()
        else:
            _STATE_CACHE.pop(org_id, None)


# ──────────────────────────────────────────────────────────────────────────
# State resolution
# ──────────────────────────────────────────────────────────────────────────
def _read_suspended(db: Session, org_id: int) -> bool:
    """Authoritative read. True when the tenant must be denied access.

    A DELETED organization has no ``organizations`` row at all, so the operational
    lookup returning None also denies — an access token minted before the purge
    cannot outlive it.
    """
    org = (
        db.query(Organization.is_active)
          .filter(Organization.id == org_id)
          .one_or_none()
    )
    if org is None:
        # Row is gone (deleted tenant) — deny.
        return True
    if org.is_active is False:
        return True

    # The commercial row is the operator-facing source of truth. Imported or
    # legacy tenants may not have one; absence is not suspension.
    try:
        from app.commercial.models import CommercialOrganization
    except Exception:  # pragma: no cover - commercial layer not migrated
        return False

    state = (
        db.query(CommercialOrganization.commercial_state)
          .filter(CommercialOrganization.operational_org_id == org_id)
          .limit(1)
          .scalar()
    )
    if state is None:
        return False
    return str(state).strip().lower() in BLOCKING_COMMERCIAL_STATES


def is_suspended(db: Session, org_id: Optional[int]) -> bool:
    """Cached "must this tenant be denied?" check. Never raises."""
    if org_id is None:
        return False
    cached = _cache_get(org_id)
    if cached is not None:
        return cached
    try:
        suspended = _read_suspended(db, org_id)
    except Exception:
        # Fail OPEN on an infrastructure error. A transient database blip must
        # not lock every tenant out of the product; the state is re-read within
        # `_STATE_TTL`. Deliberate suspension always resolves from a healthy
        # connection, and the write path itself cannot succeed without one.
        _log.exception("Lifecycle state read failed for org %s — allowing", org_id)
        return False
    _cache_set(org_id, suspended)
    return suspended


def suspended_error(code: str = ERR_ORG_SUSPENDED) -> HTTPException:
    """The single 403 every enforcement point raises.

    ``detail`` is a dict so clients can branch on ``code`` without string
    matching, matching the shape already used by
    ``app.commercial.enforcement`` for its seat/team limits.
    """
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": code, "message": SUSPENDED_MESSAGE},
    )


# ──────────────────────────────────────────────────────────────────────────
# Enforcement helpers
# ──────────────────────────────────────────────────────────────────────────
def is_exempt(user: User, *, impersonated: bool = False) -> bool:
    """Platform operators and support impersonation bypass the tenant gate."""
    if user is None:
        return True
    if impersonated:
        return True
    if (getattr(user, "role", "") or "").lower() == "super_admin":
        return True
    return getattr(user, "organization_id", None) is None


def assert_active(db: Session, user: User, *, impersonated: bool = False) -> None:
    """Raise 403 when ``user``'s organization may not operate. Otherwise no-op."""
    if is_exempt(user, impersonated=impersonated):
        return
    if is_suspended(db, getattr(user, "organization_id", None)):
        raise suspended_error()


# The FastAPI dependency form lives in ``app.auth.service`` as
# ``require_active_organization`` — it has to compose with ``get_current_user``,
# and that module already imports this one, so defining it there avoids a
# circular import. Everything above is plain functions so it stays importable
# from anywhere (services, jobs, tests) without dragging in the auth stack.

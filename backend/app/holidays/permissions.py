"""
Temveris – Holiday permission policy (single definition)
=========================================================
Two different role lists governed the same capability:

  * ``app/holidays/router.py``   – ``("admin", "hr")``
  * ``app/workspace/router.py``  – ``("admin", "hr", "owner", "billing_admin",
                                      "super_admin")``

so a ``super_admin`` could save a country profile for a workspace but could not
add a single holiday to it, and an ``owner`` could do neither. Both surfaces now
import from here.

The unified policy
------------------
==================  ======  ======  ======  ======  ======  ==========
Role                 View    Add     Edit    Delete  Import  Profiles
==================  ======  ======  ======  ======  ======  ==========
employee             yes     no      no      no      no      no
admin                yes     yes     yes     yes     yes     yes
hr                   yes     yes     yes     yes     yes     yes
owner                yes     yes     yes     yes     yes     yes
super_admin          yes     yes     yes     yes     yes     yes
billing_admin        yes     no      no      no      no      no
==================  ======  ======  ======  ======  ======  ==========

``owner`` and ``super_admin`` are ADDED to holiday writes: they already had the
strictly greater power of configuring which country calendar the workspace
runs on, so withholding "add one holiday" was an inconsistency, not a control.

``billing_admin`` is REMOVED from holiday writes (it previously reached the
profile endpoints through the workspace list). It is a payment role; the
holiday calendar drives attendance, payroll working days and leave cost, which
is HR authority. This is the one place the unification narrows access rather
than widening it, and it is deliberate.

Reads stay open to every authenticated member of the organization — employees
need the calendar, and the employee Dashboard has always read the same endpoint.
Organization scoping is enforced separately, on every query, and is not a role
question.
"""
from __future__ import annotations

from typing import Tuple

from fastapi import Depends, HTTPException

from app.auth.service import get_current_user
from database import models

#: Roles permitted to create, update, delete or import holidays.
HOLIDAY_WRITE_ROLES: Tuple[str, ...] = ("admin", "hr", "owner", "super_admin")

#: Roles permitted to read the calendar — everyone authenticated. Kept as an
#: explicit constant so the matrix above is checkable in a test.
HOLIDAY_READ_ROLES: Tuple[str, ...] = (
    "employee", "admin", "hr", "owner", "super_admin", "billing_admin",
)

_FORBIDDEN_DETAIL = "Admin or HR access required to manage holidays"


def can_write_holidays(user) -> bool:
    return getattr(user, "role", None) in HOLIDAY_WRITE_ROLES


def require_holiday_write(user) -> None:
    """Raise 403 unless the user may modify the holiday calendar."""
    if not can_write_holidays(user):
        raise HTTPException(status_code=403, detail=_FORBIDDEN_DETAIL)


def holiday_admin(
    current_user: models.User = Depends(get_current_user),
) -> models.User:
    """FastAPI dependency: the caller must be able to modify holidays."""
    require_holiday_write(current_user)
    return current_user

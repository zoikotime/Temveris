"""
Shared helper for identifying the seeded "Administrator" (CEO) account.

The Administrator account belongs to the workspace owner / CEO. It is
explicitly NOT a regular employee — the CEO logs in occasionally to
monitor the platform and use admin features, never to track work time.
Including it in attendance math pollutes every aggregate:

  * inflates "Total Employees" by 1
  * shows up as "Absent" on the Overview grid every day
  * fires "Requires Attention" alerts that aren't real
  * skews team-wide productivity / hours / payroll numbers

This module centralises the "is this person a working seat" check so
every router (attendance, reports, payroll, risk, analytics) agrees on
exactly which user gets filtered.

There are TWO exclusions here, and they answer the same question from
different columns:

  * ``is_system_admin`` / ``exclude_system_admin`` — by DISPLAY NAME, from the
    per-organization ``attendance.non_working_account_names`` setting. This is
    the aggregate filter: it removes the seat from Total Employees, the
    Overview grid, reports and payroll.
  * ``is_non_working_role`` / ``NON_WORKING_ROLES`` — by ROLE. This is the
    ALERT filter: it decides who never gets emailed about. It deliberately does
    NOT strip anyone from the roster or the aggregates, because ``admin`` and
    ``hr`` have always appeared in Total Employees while never being emailed
    about, and quietly changing every tenant's headcount is not this fix's job.

``is_attendance_tracked`` combines both and is what the alert paths call.

Identification rule
-------------------
Strict name match, case-insensitive and trimmed, against the names in
``attendance.non_working_account_names`` — a per-organization setting whose
system default is "administrator", the display name our own seeder uses.

It is a setting because it used to be a global constant: any tenant with a
real employee named "Administrator" had that person silently dropped from
attendance, reports and payroll, and a tenant whose owner seat is named
anything else could not exclude it at all. An organization can clear the
setting (excluding nobody) or list its own non-working seats.

Anything not on the list — including "Admin", admin-role HR people, or
differently-named owner accounts — is a regular user and keeps full
attendance tracking.
"""

from __future__ import annotations

from typing import Iterable, List, TypeVar

from database import models


# The name our seeder gives the CEO seat, and therefore the system default for
# the setting below. Not a universal truth about other people's workspaces.
SYSTEM_ADMIN_NAME = "administrator"

SETTING_NON_WORKING_NAMES = "attendance.non_working_account_names"

SETTING_DEFAULTS = {SETTING_NON_WORKING_NAMES: SYSTEM_ADMIN_NAME}

SETTING_DESCRIPTIONS = {
    SETTING_NON_WORKING_NAMES:
        "Comma-separated display names of accounts that are not employees "
        "(e.g. the seeded owner/CEO seat). They are excluded from attendance, "
        "reports and payroll. Leave empty to exclude nobody.",
}


def excluded_names(db=None, org_id=None) -> set:
    """The set of lower-cased names excluded for this organization."""
    raw = SYSTEM_ADMIN_NAME
    if db is not None and org_id is not None:
        try:
            from app.settings import scope as settings_scope
            raw = settings_scope.org_settings(
                db, org_id, dict(SETTING_DEFAULTS),
            ).get(SETTING_NON_WORKING_NAMES, SYSTEM_ADMIN_NAME)
        except Exception:
            raw = SYSTEM_ADMIN_NAME
    return {n.strip().lower() for n in str(raw or "").split(",") if n.strip()}


def is_system_admin(user: models.User | None, db=None) -> bool:
    """Return True iff `user` is one of this organization's non-working seats.

    Passing ``db`` resolves the organization's configured list; without it the
    system default applies.
    """
    if user is None:
        return False
    names = excluded_names(db, getattr(user, "organization_id", None))
    return (user.name or "").strip().lower() in names


# ──────────────────────────────────────────────────────────────────────────
# Governance seats — the ROLE half of "this person does not clock in".
#
# The name list above only ever caught ONE seat: the seeded "Administrator".
# A workspace can hold several accounts that are plainly not timesheet-tracked
# workers, under any display name their owner chose — and the attendance alert
# paths carried their own two-name answer (`{"admin", "hr"}`, re-typed as a
# literal tuple in two more places), so a seat named "Super Admin" was emailed
# about as an absent employee every working day.
#
# Membership is DERIVED, not guessed. ``workspace/router.py`` ROLE_CATALOG gives
# exactly two roles the ``self.profile.edit`` / ``self.timesheet.view`` /
# ``self.leave.submit`` triple — ``people_manager`` and ``employee`` — and
# ``self.timesheet.view`` is documented there as "View own clock sessions and
# timesheets". So the catalogue itself declares who owns a timesheet, and the
# eight roles below are the complement. Corroborated independently by
# ``billing/router.py`` BILLING_VIEW_ROLES, ``workspace/router.py``
# ``_require_admin`` and ``notifications/recipients.py`` ``_FALLBACK_ROLES``,
# all of which group these names as the people who RUN a workspace.
#
# This is a DENYLIST and must stay one: an unrecognised role stays tracked.
# ``User.role`` is a free ``String(50)`` with no enum and no CHECK constraint;
# the Add Employee form writes ``manager`` (a working team lead, absent from
# ROLE_CATALOG entirely), ``commercial/projection.py`` knows the legacy
# synonyms ``user``/``worker``, and a tenant can mint custom slugs like
# ``shift_supervisor`` that land straight in the column. Inverting this into an
# allowlist of worker roles would silently stop alerting every one of them,
# which is the same bug pointed the other way — and far harder to notice,
# because nobody reports an email that never arrived.
#
# NEVER add: employee, people_manager, manager, user, worker, or any custom
# slug. If a tenant genuinely has a working employee sitting on a governance
# role, the fix is to give that person a worker role — their attendance,
# reports and payroll all key off the same column.
NON_WORKING_ROLES = frozenset({
    "owner",                      # ownership is a column; role is vestigial
    "super_admin",                # platform seat; org_id is nulled on login
    "admin",                      # pre-existing behaviour
    "hr",                         # pre-existing behaviour
    "billing_admin",
    "payroll_finance_admin",
    "compliance_audit_officer",
    "it_security_admin",
})


def is_non_working_role(user) -> bool:
    """True when this user's ROLE marks them as a non-timesheet seat.

    Case- and whitespace-insensitive, because ``User.role`` has no database
    constraint. Orthogonal to :func:`is_system_admin`, which answers the same
    question from the display name; a seat is skipped if EITHER says so.
    """
    if user is None:
        return False
    return (getattr(user, "role", "") or "").strip().lower() in NON_WORKING_ROLES


def is_attendance_tracked(user, db=None) -> bool:
    """True when this user should be held to a clock-in — the single predicate
    every attendance ALERT path asks before emailing about somebody.

    Combines both exclusions: the per-organization display-name list
    (:func:`is_system_admin`) and the role denylist
    (:func:`is_non_working_role`). Deliberately says nothing about
    ``is_active`` or ``organization_id`` — callers already filter those, and
    folding them in here would hide those checks from the call site.
    """
    if user is None:
        return False
    return not is_non_working_role(user) and not is_system_admin(user, db)


U = TypeVar("U")


def exclude_system_admin(users: Iterable[U], db=None) -> List[U]:
    """Drop this organization's non-working seats from a user iterable.

    Works on any iterable of model rows or row-shaped objects that expose
    a `.name` attribute, so it can be applied to ORM users, dataclass
    projections, or namedtuples without rework.

    The excluded-name set is resolved ONCE PER ORGANIZATION rather than once
    per user. `is_system_admin()` re-reads the org's settings row on every
    call, so filtering a 57-employee list issued 57 identical settings queries
    — a measurable slice of /employees/team. The predicate itself is unchanged,
    and users are keyed by their own organization_id so a mixed-tenant iterable
    still gets each row's own org list.
    """
    users = list(users)
    if db is None:
        return [u for u in users if not is_system_admin(u, None)]

    names_by_org: dict = {}
    kept: List[U] = []
    for u in users:
        if u is None:
            continue
        org_id = getattr(u, "organization_id", None)
        if org_id not in names_by_org:
            names_by_org[org_id] = excluded_names(db, org_id)
        if (getattr(u, "name", "") or "").strip().lower() not in names_by_org[org_id]:
            kept.append(u)
    return kept


def system_admin_ids(db) -> List[int]:
    """Return the database ids of every system Administrator row.

    Useful when an endpoint filters in SQL (e.g. `User.id.notin_(...)`)
    rather than fetching rows and stripping them in Python.
    """
    rows = db.query(models.User.id, models.User.name,
                    models.User.organization_id).all()
    by_org: dict = {}
    out = []
    for row in rows:
        names = by_org.get(row.organization_id)
        if names is None:
            names = by_org[row.organization_id] = excluded_names(db, row.organization_id)
        if (row.name or "").strip().lower() in names:
            out.append(row.id)
    return out

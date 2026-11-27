"""
The persisted ECO view grant.
=============================

ONE ROW PER (ORGANIZATION, USER). NO ROW MEANS NO AUTHORITY.
------------------------------------------------------------
Absence is the default, not a special case. A fresh database, a new
organization and a restored backup all start with nobody holding ECO view
authority, without anything having to be seeded — and at the end of this
increment the table has zero rows, which is the required final state.

WHY THIS IS NOT ``User.role``
-----------------------------
The owner ruling forbids deriving ECO authority from ``User.role``, and the
repository shows why. ``role`` is a nullable free-text ``VARCHAR(50)`` carrying
at least twelve values, and authorization against it is hand-rolled per router
in at least eight different tuples:

    role not in ("admin", "hr")                                  x18
    role not in ("admin", "hr", "owner", "billing_admin", ...)   x3
    role in ("admin", "manager")                                 x2
    ...

Selecting any subset for ECO would add a ninth, and would mean an unrelated
change to a Worker's job title silently changed who can read attendance
evidence. A grant is a decision somebody made and recorded; a role string is a
label that drifted.

WHY THIS IS NOT ``AppSettings``
-------------------------------
Same reasoning the rollout control already documents, and it applies harder
here: ``app_settings`` is written by generic key/value endpoints from many call
sites, so an authorization grant living there could be created by any code path
permitted to write settings. That is privilege escalation with extra steps.

WHY THERE IS NO ``revoked_at`` COLUMN
-------------------------------------
Revocation is the deletion of the grant, because the read path asks one
question — does a row exist — and a nullable revocation timestamp would create
a second way to answer it. Two mechanisms for "not granted" is how a resolver
ends up honouring a revoked grant. The audit trail for grant and revocation
belongs on ``AuditLog``, where every other administrative action is recorded;
this table is the live authority and nothing else.

NOT WIRED
---------
No route, scheduler or service reads this table. The primitive exists so the
future WF-ECO-03 / WF-ECO-04 surfaces have an authority to consult; those
surfaces are not authorized and are not built.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Column, DateTime, ForeignKey, Integer, String, UniqueConstraint,
)

from database.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EcoViewGrant(Base):
    """One tenant's explicit grant of ECO view authority to one user.

    TENANT-WIDE, DELIBERATELY. The owner left open whether the administrator
    grant should be tenant-wide or scoped to a subset of Workers, and directed
    that the simplest tenant-scoped representation be used rather than a
    Worker-subset model being invented. So the grant carries NO worker-subset
    column: adding one later is additive and changes no existing row, whereas
    shipping a subset column now would mean choosing the subset semantics the
    ruling withheld.
    """

    __tablename__ = "eco_view_grant"

    id = Column(Integer, primary_key=True)

    #: The tenant this grant is valid inside. RESTRICT rather than CASCADE: an
    #: organization holding live authority records is not silently deletable.
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )

    #: The user who may read ECO records for Workers in that tenant.
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )

    #: Free-text actor label, the same convention ``rollout_control`` uses: an
    #: id where one exists, "SYSTEM" otherwise. Recorded so "who granted this"
    #: is answerable without a join; the acting user id proper belongs on the
    #: AuditLog row.
    granted_by = Column(String(64), nullable=False, default="SYSTEM")
    granted_at_utc = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow,
    )

    __table_args__ = (
        # One grant per user per tenant. This is also what makes a global grant
        # unrepresentable: there is no row shape meaning "every organization",
        # because organization_id is NOT NULL and names exactly one.
        UniqueConstraint(
            "organization_id", "user_id",
            name="uq_eco_view_grant_org_user",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return (f"<EcoViewGrant org={self.organization_id} "
                f"user={self.user_id}>")


__all__ = ["EcoViewGrant"]

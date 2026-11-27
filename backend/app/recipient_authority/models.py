"""
The persisted ECO recipient designation.
========================================

ONE ROW PER (ORGANIZATION, AUDIENCE, USER). NO ROW MEANS NO AUTHORITY.
----------------------------------------------------------------------
Absence is the default. A fresh database, a new organization and a restored
backup all start with no designated PRIMARY ADMIN or HR ADMIN, without anything
having to be seeded — and at the end of this increment the table has zero rows.

WHY NOTHING THAT ALREADY EXISTS COULD BE USED
---------------------------------------------
Owner ruling D-3 requires identity-based, tenant-scoped, deterministic
authority. Four candidates were inspected and each fails a different clause:

``app.notifications.recipients``
    Returns lower-cased EMAIL ADDRESS STRINGS, and its first tier is a
    comma-separated free-text settings value. D-3 forbids inferring authority
    from an email address, and a string cannot carry the identity the permanent
    snapshot has to preserve.

``User.role``
    A nullable free-text ``VARCHAR(50)`` carrying at least twelve values, read
    through eight different hand-rolled tuples across the routers. Choosing a
    subset for ECO would mean an unrelated job-title edit silently changed who
    is notified about attendance evidence.

``OrgSettings.owner_user_id``
    The COMMERCIAL owner, nullable, and ``ondelete="SET NULL"`` — the reference
    erases itself. §33.1 names PRIMARY ADMIN, not owner, and the specification
    maps no such equivalence, so adopting it would be inventing a hierarchy.

``teams.head_user_id``
    Reaching it from a Worker means joining the free-text ``User.team`` string
    to ``Team.name``. That is inference from team membership, which the
    authorization forbids outright for DIRECT MANAGER.

WHY THE AUDIENCE VOCABULARY IS CONSTRAINED TO TWO VALUES
--------------------------------------------------------
The table is organisation-scoped, so it can only express audiences that ARE
organisation-scoped. WORKER is the event's subject — a designation row for it
would let a tenant redirect a Worker's own notification to somebody else.
DIRECT MANAGER is a per-Worker relationship; an org-wide row for it would be an
invented hierarchy. Both are resolved elsewhere and neither is designatable, so
the CHECK constraint says so in the database rather than in a comment.

WHY THERE IS NO EFFECTIVE DATING
--------------------------------
Owner ruling D-3.6 states effective dating is not required. The historical
boundary that matters is EVENT CREATION -> SNAPSHOT: once the event freezes its
recipients, the designation table may change freely and history does not move.
A second time axis here would be a second way to answer "who held this on that
date", and the snapshot already answers it authoritatively.

WHY THERE IS NO ``revoked_at``
------------------------------
Same reason ``eco_view_grant`` has none: revocation is the deletion of the row,
because the read path asks one question — does a row exist. Two mechanisms for
"not designated" is how a resolver ends up honouring a revoked designation.

NOT WIRED TO DELIVERY
---------------------
Nothing in this package sends anything. It answers who the authoritative
recipients were; §34 leaves channel selection, preferences, transport, retry
and suppression to the central notification service.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String,
    UniqueConstraint,
)

from database.database import Base

from .outcomes import DESIGNATABLE_AUDIENCES

#: Rendered into the CHECK constraint. Sorted so the emitted DDL is stable
#: across interpreter runs and the migration's text matches the model's.
_AUDIENCE_SQL_LIST = ", ".join(
    "'%s'" % value for value in sorted(DESIGNATABLE_AUDIENCES))


class EcoRecipientDesignation(Base):
    """One tenant's explicit designation of one user as one §33.1 audience.

    Several rows may share an audience: §33.1 says PRIMARY ADMIN is notified,
    not that exactly one person holds the seat, and the resolver returns ALL of
    them in a deterministic order. That is the point of the shape — it makes
    "silently choose the first user" unrepresentable, because there is no
    single-holder column to read.
    """

    __tablename__ = "eco_recipient_designation"

    id = Column(Integer, primary_key=True)

    #: The tenant this designation is valid inside. RESTRICT rather than
    #: CASCADE: an organization holding live authority records is not silently
    #: deletable.
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )

    #: One of ``DESIGNATABLE_AUDIENCES``. Text with a CHECK rather than a DB
    #: enum, the convention ``eco_threshold_event.tier`` already follows: §32
    #: warns that a value surface cannot be corrected later without a breaking
    #: change.
    audience = Column(String(32), nullable=False)

    #: The designated user, BY IDENTITY. RESTRICT, so a user holding a live
    #: designation cannot be deleted out from under it — the same protected
    #: history strategy the rest of ECO uses. The event-time snapshot is
    #: unaffected either way: it stores the id, not a reference.
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )

    #: Free-text actor label, the convention ``rollout_control`` and
    #: ``eco_view_grant`` share: an id where one exists, "SYSTEM" otherwise.
    designated_by = Column(String(64), nullable=False, default="SYSTEM")
    designated_at_utc = Column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        # One designation per user per audience per tenant. Also what makes a
        # cross-organisation designation unrepresentable as a SHAPE:
        # organization_id is NOT NULL and names exactly one tenant. Whether the
        # named USER belongs to that tenant is a different question, which the
        # resolver checks and refuses on — no FK can express it.
        UniqueConstraint(
            "organization_id", "audience", "user_id",
            name="uq_eco_recipient_designation_identity",
        ),
        # The resolver's only read: every holder of one audience in one tenant.
        Index("ix_eco_recipient_designation_lookup",
              "organization_id", "audience"),
        CheckConstraint(
            "audience IN (%s)" % _AUDIENCE_SQL_LIST,
            name="ck_eco_recipient_designation_audience",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return (f"<EcoRecipientDesignation org={self.organization_id} "
                f"{self.audience} user={self.user_id}>")


__all__ = ["EcoRecipientDesignation"]

"""
The governed jurisdiction mapping — persisted.
==============================================

Owner ruling B, B8, B9.

THREE TABLES, AND WHY NOT TWO
------------------------------
``jurisdiction_classification``
    What a country legally IS: EU, UK, consultation-requiring. Platform-
    governed and effective-dated, so a historical assessment resolves the
    classification in force on the assessment date rather than today's (B7).

``tenant_jurisdiction_assignment``
    Which jurisdictions apply to one Tenant, as of an effective date. Carries
    its own provenance: who recorded it, under what authority.

``tenant_jurisdiction_assignment_country``
    The countries in that assignment. A SEPARATE table rather than a JSON
    column or a delimited string, for one reason that decides B9: an
    assignment with ZERO child rows is a Tenant DECLARING that no governed
    jurisdiction applies, and the absence of any assignment row is nobody
    having established the answer. A nullable column could not hold that
    distinction without inventing a sentinel, and a sentinel is exactly the
    kind of thing that gets collapsed by a later reader.

EVERYTHING IS APPEND-ONLY
-------------------------
B8: "Corrections supersede; historical assignments are not mutated."
Enforced by database triggers on all three tables, because ORM listeners are
bypassed by Core statements (Ruling M / D18-F1).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean, CheckConstraint, Column, Date, DateTime, ForeignKey, Index,
    Integer, String, UniqueConstraint, event, text,
)

from database.database import Base

from .append_only_ddl import (
    ASSIGNMENT_COUNTRY_TABLE, ASSIGNMENT_TABLE, CLASSIFICATION_TABLE,
    append_only_statements,
)
from .outcomes import AssignmentSource

_SOURCE_SQL = ", ".join(f"'{m.value}'" for m in AssignmentSource)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JurisdictionClassification(Base):
    """What one country legally IS, from one effective date. Platform-governed.

    A Tenant can never write this table — ``service.py`` refuses a non-platform
    actor by signature, and the CHECKs below make a malformed code
    unrepresentable rather than merely discouraged.
    """

    __tablename__ = CLASSIFICATION_TABLE

    id = Column(Integer, primary_key=True)

    #: ISO 3166-1 alpha-2, upper case (owner ruling B.1). The CHECK is the
    #: vocabulary in the database, not a convention in a docstring.
    country_code = Column(String(2), nullable=False, index=True)

    #: The three classifications the ruling names. Separate columns rather than
    #: one enum: B2 and B3 are explicit that UK is NOT represented as an EU
    #: member, and a single "region" value would force exactly that conflation.
    is_eu = Column(Boolean, nullable=False, default=False)
    is_uk = Column(Boolean, nullable=False, default=False)
    requires_consultation = Column(Boolean, nullable=False, default=False)

    #: B7: effective-dated, so a past assessment resolves the classification in
    #: force then. Never "the current row".
    effective_from = Column(Date, nullable=False, index=True)

    recorded_at_utc = Column(DateTime(timezone=True), nullable=False,
                             default=_utcnow)
    #: Free-text platform actor label. There is no user id because a Tenant
    #: user can never be the author of a legal classification (B6).
    recorded_by = Column(String(64), nullable=False, default="PLATFORM")

    #: The classification this one replaces, where it replaces one.
    supersedes_id = Column(
        Integer, ForeignKey(f"{CLASSIFICATION_TABLE}.id", ondelete="RESTRICT"),
        nullable=True)

    __table_args__ = (
        # One classification per country per effective date. A second would
        # make "the classification in force" ambiguous, which B7 forbids.
        UniqueConstraint("country_code", "effective_from",
                         name="uq_jurisdiction_classification_effective"),
        CheckConstraint("length(country_code) = 2",
                        name="ck_jurisdiction_classification_alpha2_length"),
        CheckConstraint("country_code = upper(country_code)",
                        name="ck_jurisdiction_classification_alpha2_upper"),
        Index("ix_jurisdiction_classification_lookup",
              "country_code", "effective_from"),
    )

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (f"<JurisdictionClassification {self.country_code} "
                f"from={self.effective_from} eu={self.is_eu} uk={self.is_uk} "
                f"consult={self.requires_consultation}>")


class TenantJurisdictionAssignment(Base):
    """Which jurisdictions apply to ONE Tenant, from one effective date.

    B8: the platform establishes it; ``{admin, hr}`` may correct it. Both
    carry provenance, and a correction is a NEW row pointing at the one it
    supersedes — never an edit.
    """

    __tablename__ = ASSIGNMENT_TABLE

    id = Column(Integer, primary_key=True)

    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False, index=True)

    effective_from = Column(Date, nullable=False, index=True)

    #: PLATFORM or TENANT (B8). The distinction is evidence: an assignment the
    #: platform established and one a Tenant corrected are different facts.
    source = Column(String(16), nullable=False)

    #: Present only for a TENANT correction. RESTRICT for the reason Ruling M
    #: gives everywhere else: removing a person must never remove history.
    actor_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)
    #: The role the actor held. Frozen here because attribution that can be
    #: re-derived from today's roles is not attribution.
    actor_role = Column(String(32), nullable=True)
    actor_label = Column(String(64), nullable=False, default="PLATFORM")

    recorded_at_utc = Column(DateTime(timezone=True), nullable=False,
                             default=_utcnow)

    supersedes_id = Column(
        Integer, ForeignKey(f"{ASSIGNMENT_TABLE}.id", ondelete="RESTRICT"),
        nullable=True)

    __table_args__ = (
        UniqueConstraint("organization_id", "effective_from",
                         name="uq_tenant_jurisdiction_assignment_effective"),
        CheckConstraint("source IN (%s)" % _SOURCE_SQL,
                        name="ck_tenant_jurisdiction_assignment_source"),
        # B8: a TENANT correction is attributable or it is not a correction.
        # A PLATFORM assignment has no Tenant actor, by construction.
        CheckConstraint(
            "(source = 'TENANT' AND actor_user_id IS NOT NULL "
            " AND actor_role IS NOT NULL)"
            " OR (source = 'PLATFORM' AND actor_user_id IS NULL)",
            name="ck_tenant_jurisdiction_assignment_actor_pair"),
        Index("ix_tenant_jurisdiction_assignment_lookup",
              "organization_id", "effective_from"),
    )

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (f"<TenantJurisdictionAssignment org={self.organization_id} "
                f"from={self.effective_from} source={self.source}>")


class TenantJurisdictionAssignmentCountry(Base):
    """One country inside one assignment.

    ZERO rows for an assignment is the explicitly-declared empty set (B9), and
    is why this is a table rather than a column.
    """

    __tablename__ = ASSIGNMENT_COUNTRY_TABLE

    id = Column(Integer, primary_key=True)

    assignment_id = Column(
        Integer, ForeignKey(f"{ASSIGNMENT_TABLE}.id", ondelete="RESTRICT"),
        nullable=False, index=True)

    country_code = Column(String(2), nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("assignment_id", "country_code",
                         name="uq_tenant_jurisdiction_assignment_country"),
        CheckConstraint("length(country_code) = 2",
                        name="ck_tenant_jurisdiction_country_alpha2_length"),
        CheckConstraint("country_code = upper(country_code)",
                        name="ck_tenant_jurisdiction_country_alpha2_upper"),
    )

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (f"<TenantJurisdictionAssignmentCountry "
                f"assignment={self.assignment_id} {self.country_code}>")


# ═════════════════════════════════════════════════════════════════════════════
# Database-level append-only enforcement (owner rulings B / B8)
# ═════════════════════════════════════════════════════════════════════════════
# Attached to the TABLE so `Base.metadata.create_all()` carries it. The Alembic
# revision emits the SAME statements, because `op.create_table` builds its own
# Table object and this listener does not fire for it.

def _install(table_name: str):
    def _listener(target, connection, **kw):
        for statement in append_only_statements(connection.dialect.name,
                                                table_name):
            connection.execute(text(statement))
    return _listener


event.listens_for(JurisdictionClassification.__table__, "after_create")(
    _install(CLASSIFICATION_TABLE))
event.listens_for(TenantJurisdictionAssignment.__table__, "after_create")(
    _install(ASSIGNMENT_TABLE))
event.listens_for(TenantJurisdictionAssignmentCountry.__table__,
                  "after_create")(_install(ASSIGNMENT_COUNTRY_TABLE))


__all__ = [
    "JurisdictionClassification",
    "TenantJurisdictionAssignment",
    "TenantJurisdictionAssignmentCountry",
]

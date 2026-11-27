"""
Recording and resolving the governed jurisdiction mapping.
==========================================================

Owner ruling B, B8, B9.

FOUR FUNCTIONS, AND THE AUTHORITY EACH ONE ENFORCES
---------------------------------------------------
``record_classification``   PLATFORM ONLY. What a country legally is.
``assign_platform``         PLATFORM. Establishes a Tenant's assignment (B8).
``correct_assignment``      ``{admin, hr}`` ONLY. Supersedes it (B8).
``resolve``                 Read-only. What applies as of a date (B7 + B9).

The split is the authority model, expressed as separate entry points rather
than a role parameter on one function: a Tenant actor cannot reach
``record_classification`` at all, so "Tenants do not redefine the legal
classification of a country" is true by signature and not by a runtime check
somebody can forget.

EFFECTIVE DATING IS NOT OPTIONAL (B7)
-------------------------------------
``resolve`` takes ``as_of`` and answers with the classification and the
assignment IN FORCE ON THAT DATE. It never reads "the latest row". A gate
assessed for a past date must get the past answer, exactly as
``config_version`` and ``policy_version`` already behave.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Iterable, Optional, Sequence

import sqlalchemy as sa
from sqlalchemy.orm import Session

from .models import (
    JurisdictionClassification, TenantJurisdictionAssignment,
    TenantJurisdictionAssignmentCountry,
)
from .outcomes import (
    ISO_ALPHA2, AssignmentSource, ClassificationNotGoverned,
    InvalidCountryCode, JurisdictionResolution, UnauthorizedAssignment,
)

#: The roles that may correct a Tenant's assignment. Imported rather than
#: restated: owner ruling D fixed ONE authorization vocabulary for ECO
#: governance acts, and a second copy here could drift from it.
from app.eco_threshold_capture.outcomes import AUTHORIZED_ROLES


def _normalise(country_codes: Iterable[str]) -> Sequence[str]:
    """Validate and canonicalise an ISO 3166-1 alpha-2 set.

    Upper-cased and de-duplicated, because ``{"de", "DE"}`` is one jurisdiction
    written twice, and sorted so an assignment's meaning does not depend on the
    order a caller happened to pass.
    """
    out = set()
    for raw in country_codes:
        code = str(raw).strip().upper()
        if not ISO_ALPHA2.match(code):
            raise InvalidCountryCode(
                f"{raw!r} is not an ISO 3166-1 alpha-2 country code; the "
                f"governed mapping accepts no other vocabulary (owner ruling "
                f"B.1)")
        out.add(code)
    return tuple(sorted(out))


def _as_date(value) -> date:
    return value.date() if isinstance(value, datetime) else value


# ═════════════════════════════════════════════════════════════════════════════
# Classification — platform only
# ═════════════════════════════════════════════════════════════════════════════
def record_classification(
    db: Session, *, country_code: str, effective_from, is_eu: bool = False,
    is_uk: bool = False, requires_consultation: bool = False,
    recorded_by: str = "PLATFORM", actor=None,
) -> JurisdictionClassification:
    """Record what a country legally IS, from an effective date.

    ``actor`` exists only to be REFUSED. B6 and B8 both state that country
    classification is platform-controlled, so passing a Tenant actor here is a
    caller error worth naming rather than silently ignoring.
    """
    if actor is not None:
        raise ClassificationNotGoverned(
            "a country's legal classification is platform-governed (owner "
            "ruling B6/B8); a Tenant actor may correct WHICH jurisdictions "
            "apply to it, never what a jurisdiction legally is")

    code = _normalise([country_code])[0]
    effective = _as_date(effective_from)

    prior = db.execute(
        sa.select(JurisdictionClassification)
        .where(JurisdictionClassification.country_code == code,
               JurisdictionClassification.effective_from < effective)
        .order_by(JurisdictionClassification.effective_from.desc())
        .limit(1)).scalars().first()

    row = JurisdictionClassification(
        country_code=code, effective_from=effective, is_eu=bool(is_eu),
        is_uk=bool(is_uk), requires_consultation=bool(requires_consultation),
        recorded_by=str(recorded_by)[:64],
        supersedes_id=(prior.id if prior is not None else None))
    db.add(row)
    db.flush()
    return row


def classification_as_of(db: Session, *, country_code: str,
                         as_of) -> Optional[JurisdictionClassification]:
    """The classification in force for one country on one date, or None."""
    code = _normalise([country_code])[0]
    return db.execute(
        sa.select(JurisdictionClassification)
        .where(JurisdictionClassification.country_code == code,
               JurisdictionClassification.effective_from <= _as_date(as_of))
        .order_by(JurisdictionClassification.effective_from.desc())
        .limit(1)).scalars().first()


# ═════════════════════════════════════════════════════════════════════════════
# Assignment — platform establishes, {admin, hr} corrects (B8)
# ═════════════════════════════════════════════════════════════════════════════
def _write_assignment(
    db: Session, *, organization_id: int, country_codes: Iterable[str],
    effective_from, source: AssignmentSource, actor_user_id: Optional[int],
    actor_role: Optional[str], actor_label: str,
) -> TenantJurisdictionAssignment:
    """The one construction of an assignment. Both entry points inherit it.

    An EMPTY ``country_codes`` writes the parent row and no children, which is
    B9's explicitly-declared empty set. It is not an error and must never be
    turned into one.
    """
    codes = _normalise(country_codes)
    effective = _as_date(effective_from)

    prior = db.execute(
        sa.select(TenantJurisdictionAssignment)
        .where(TenantJurisdictionAssignment.organization_id == organization_id,
               TenantJurisdictionAssignment.effective_from < effective)
        .order_by(TenantJurisdictionAssignment.effective_from.desc())
        .limit(1)).scalars().first()

    row = TenantJurisdictionAssignment(
        organization_id=int(organization_id), effective_from=effective,
        source=source.value, actor_user_id=actor_user_id,
        actor_role=actor_role, actor_label=str(actor_label)[:64],
        supersedes_id=(prior.id if prior is not None else None))
    db.add(row)
    db.flush()

    for code in codes:
        db.add(TenantJurisdictionAssignmentCountry(
            assignment_id=row.id, country_code=code))
    db.flush()
    return row


def assign_platform(
    db: Session, *, organization_id: int, country_codes: Iterable[str],
    effective_from, actor_label: str = "PLATFORM",
) -> TenantJurisdictionAssignment:
    """Establish a Tenant's applicable jurisdictions. Platform authority (B8)."""
    return _write_assignment(
        db, organization_id=organization_id, country_codes=country_codes,
        effective_from=effective_from, source=AssignmentSource.PLATFORM,
        actor_user_id=None, actor_role=None, actor_label=actor_label)


def correct_assignment(
    db: Session, *, organization_id: int, country_codes: Iterable[str],
    effective_from, actor,
) -> TenantJurisdictionAssignment:
    """Correct a Tenant's applicable jurisdictions. ``{admin, hr}`` only (B8).

    The correction SUPERSEDES: a new row pointing at the one it replaces. The
    prior assignment stays readable, because B8 says historical assignments are
    not mutated and because an assessment of a past date must still resolve the
    set that was in force then.
    """
    role = getattr(actor, "role", None)
    user_id = getattr(actor, "user_id", None)
    organization = getattr(actor, "organization_id", None)

    if role not in AUTHORIZED_ROLES:
        raise UnauthorizedAssignment(
            f"role {role!r} may not correct a Tenant's jurisdiction "
            f"assignment; owner ruling D authorises {sorted(AUTHORIZED_ROLES)}")
    if user_id is None:
        raise UnauthorizedAssignment(
            "a Tenant correction must be attributable to a user (B8): the "
            "actor carries no user id")
    if organization is not None and int(organization) != int(organization_id):
        raise UnauthorizedAssignment(
            "an actor may only correct their own Tenant's assignment")

    return _write_assignment(
        db, organization_id=organization_id, country_codes=country_codes,
        effective_from=effective_from, source=AssignmentSource.TENANT,
        actor_user_id=int(user_id), actor_role=str(role)[:32],
        actor_label=str(getattr(actor, "label", role))[:64])


# ═════════════════════════════════════════════════════════════════════════════
# Resolution — read-only
# ═════════════════════════════════════════════════════════════════════════════
def resolve(db: Session, *, organization_id: int,
            as_of) -> JurisdictionResolution:
    """What applies to one Tenant on one date. Reads only.

    B7: the assignment AND every country's classification are resolved as of
    ``as_of``, never from today's rows. B9: ``is_assigned`` distinguishes a
    declared empty set from nobody having established the answer.
    """
    on = _as_date(as_of)

    assignment = db.execute(
        sa.select(TenantJurisdictionAssignment)
        .where(TenantJurisdictionAssignment.organization_id == organization_id,
               TenantJurisdictionAssignment.effective_from <= on)
        .order_by(TenantJurisdictionAssignment.effective_from.desc())
        .limit(1)).scalars().first()

    if assignment is None:
        # NEVER ASSIGNED. Not the same as declared-empty, and the caller must
        # be able to tell (B9).
        return JurisdictionResolution(
            organization_id=int(organization_id), as_of=on, is_assigned=False)

    codes = tuple(sorted(db.execute(
        sa.select(TenantJurisdictionAssignmentCountry.country_code)
        .where(TenantJurisdictionAssignmentCountry.assignment_id
               == assignment.id)).scalars().all()))

    is_eu = is_uk = requires_consultation = False
    unclassified = []
    for code in codes:
        row = classification_as_of(db, country_code=code, as_of=on)
        if row is None:
            # An unanswered question, REPORTED rather than skipped.
            #
            # Skipping it used to leave the three booleans False, which every
            # downstream reader takes as "not EU, no consultation" -- so
            # silence became an exemption. The country is recorded here and
            # the caller fails closed. Answering it in this function would be
            # inventing the classification the platform is meant to govern;
            # pretending the question was not asked is worse.
            unclassified.append(code)
            continue
        is_eu = is_eu or bool(row.is_eu)
        is_uk = is_uk or bool(row.is_uk)
        requires_consultation = (requires_consultation
                                 or bool(row.requires_consultation))

    return JurisdictionResolution(
        organization_id=int(organization_id), as_of=on, is_assigned=True,
        countries=codes, assignment_id=int(assignment.id),
        source=assignment.source, is_eu=is_eu, is_uk=is_uk,
        requires_consultation=requires_consultation,
        unclassified=tuple(unclassified))


__all__ = [
    "assign_platform",
    "classification_as_of",
    "correct_assignment",
    "record_classification",
    "resolve",
]

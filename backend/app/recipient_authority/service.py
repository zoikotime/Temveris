"""
RECIPIENT-AUTHORITY: resolve the §33.1 audiences, and freeze them.
==================================================================

Two entry points, and the separation between them is the whole primitive:

    resolve_eco_recipients(db, organization_id=..., worker_user_id=...)
        Reads TODAY's authority. Used exactly once per event, at creation.

    recipients_from_snapshot(snapshot)
        Reads a PAST event's frozen answer. Touches no table, so a historical
        read cannot re-resolve history even by accident.

Owner ruling D-3.7: the snapshot is authoritative permanently. A recipient who
is later deactivated or deleted, and a designation that is later changed, leave
every already-frozen snapshot exactly as it was. An undeliverable recipient is
a DELIVERY failure for the notification service to represent (§34); it is never
a reason to rewrite recipient history.

NOTHING HERE DELIVERS ANYTHING. No transport, no channel, no preference, no
retry, no suppression, no delivery attempt, no email. §34 places all of those
in the central notification service and forbids ECO reimplementing them.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from .models import EcoRecipientDesignation
from .outcomes import (
    AUDIENCE_ORDER,
    BASIS_DESIGNATION,
    BASIS_EVENT_SUBJECT,
    BASIS_NOT_DESIGNATED,
    BASIS_NO_CANONICAL_MANAGER,
    AudienceResolution,
    AudienceState,
    CrossTenantRecipient,
    EcoAudience,
    RecipientAuthorityUnavailable,
    ResolvedRecipients,
)

#: Bumped when the snapshot's SHAPE changes, never when its contents do. v1 was
#: the Worker-only ``{"worker_user_id": ..., "roles": ["WORKER"]}`` that
#: EVAL-EVENT froze before a recipient authority existed; v2 adds the four
#: §33.1 audiences and keeps both v1 keys, so every v1 reader — including
#: ``app.alert.service._resolve_worker`` — still reads a v2 snapshot correctly.
SNAPSHOT_VERSION = 2


# ═════════════════════════════════════════════════════════════════════════════
# Tenant identity
# ═════════════════════════════════════════════════════════════════════════════

def _user_organization(db: Session, user_id: int) -> Tuple[bool, Optional[int]]:
    """``(exists, organization_id)`` for one user. One column, no ORM load."""
    from database import models as db_models

    row = db.execute(
        select(db_models.User.organization_id)
        .where(db_models.User.id == int(user_id))
    ).first()
    if row is None:
        return False, None
    return True, (None if row[0] is None else int(row[0]))


def _require_same_tenant(db: Session, *, organization_id: int, user_id: int,
                         role: str, require_exists: bool) -> None:
    """Refuse a recipient who belongs to a different tenant.

    The foreign keys cannot express this: ``eco_recipient_designation`` points
    at ``organizations`` and at ``users`` independently, and no constraint ties
    the two together. So the check lives here, and it RAISES — an audience that
    is merely unconfigured is an ordinary outcome, but one that names an
    outsider is a correctness violation, and returning "unresolved" for it
    would let the caller proceed as though the tenant were unconfigured.

    ``require_exists`` separates two different questions, and the distinction
    is deliberate:

    * A DESIGNATION is an assignment somebody made. It must name a real user in
      this tenant, so an unknown id is refused. (The FK makes that branch
      defensive rather than reachable, which is the point of having both.)
    * The WORKER is the event's SUBJECT, not an assignment. EVAL-EVENT already
      owns the organisation/user pair — it is half of the D6 uniqueness
      boundary — and making recipient resolution the gatekeeper for whether an
      event may exist would move that authority into the wrong primitive. A
      Worker whose ``users`` row is absent is therefore still the subject; a
      Worker whose row exists and names ANOTHER organisation is still refused,
      which is the case that actually crosses a tenant boundary.
    """
    exists, owner = _user_organization(db, user_id)
    if not exists:
        if require_exists:
            raise CrossTenantRecipient(
                f"{role} recipient user {user_id} does not exist; "
                f"organization {organization_id} cannot designate it")
        return
    if owner is None or int(owner) != int(organization_id):
        raise CrossTenantRecipient(
            f"{role} recipient user {user_id} belongs to organization "
            f"{owner!r}, not {organization_id}")


# ═════════════════════════════════════════════════════════════════════════════
# The four audiences
# ═════════════════════════════════════════════════════════════════════════════

def _designated(db: Session, *, organization_id: int,
                audience: EcoAudience) -> Tuple[int, ...]:
    """Every holder of one audience in one tenant, ordered by user id.

    ALL of them, never the first: §5 of the authorization forbids arbitrarily
    selecting one holder, and ``ORDER BY user_id`` makes the answer stable
    across runs, dialects and insertion order rather than merely non-arbitrary.
    """
    rows = db.execute(
        select(EcoRecipientDesignation.user_id)
        .where(and_(
            EcoRecipientDesignation.organization_id == int(organization_id),
            EcoRecipientDesignation.audience == audience.value,
        ))
        .order_by(EcoRecipientDesignation.user_id)
    ).scalars().all()
    return tuple(int(uid) for uid in rows)


def _resolve_designatable(db: Session, *, organization_id: int,
                          audience: EcoAudience) -> AudienceResolution:
    holders = _designated(db, organization_id=organization_id,
                          audience=audience)
    if not holders:
        # §33.1 "If configured", and it is not. PRIMARY ADMIN is "Always" in
        # the matrix, but "always" governs DELIVERY to a recipient who exists —
        # it cannot conjure an authority the tenant never designated. The
        # absence is recorded as a fact with a reason rather than dropped, so
        # the delivery stage can refuse on it explicitly.
        return AudienceResolution(
            audience=audience, state=AudienceState.UNRESOLVED,
            user_ids=(), basis=BASIS_NOT_DESIGNATED)

    for user_id in holders:
        _require_same_tenant(db, organization_id=organization_id,
                             user_id=user_id, role=audience.value,
                             require_exists=True)

    return AudienceResolution(
        audience=audience, state=AudienceState.RESOLVED,
        user_ids=holders, basis=BASIS_DESIGNATION)


def _resolve_worker(db: Session, *, organization_id: int,
                    worker_user_id: int) -> AudienceResolution:
    """The Worker the event is about. Identity, not name or address.

    Not designatable and not inferred: it is the event's subject. The tenant
    check still runs, because an event naming a Worker from another
    organisation is the same violation from the other direction.
    """
    _require_same_tenant(db, organization_id=organization_id,
                         user_id=worker_user_id,
                         role=EcoAudience.WORKER.value,
                         require_exists=False)
    return AudienceResolution(
        audience=EcoAudience.WORKER, state=AudienceState.RESOLVED,
        user_ids=(int(worker_user_id),), basis=BASIS_EVENT_SUBJECT)


def _resolve_direct_manager(db: Session, *, organization_id: int,
                            worker_user_id: int) -> AudienceResolution:
    """UNRESOLVED, and the basis says why rather than implying misconfiguration.

    There is no reporting line in this schema — no ``manager_id``, no
    ``reports_to``, no reporting-line table — so there is nothing authoritative
    to read. §6 forbids substituting team membership, an email domain, or an
    arbitrary admin, and forbids silently falling back to PRIMARY ADMIN. So the
    audience resolves to nobody and says exactly which authority is missing.

    This is the one audience whose absence is a CAPABILITY gap rather than a
    tenant's configuration choice, and ``BASIS_NO_CANONICAL_MANAGER`` is what
    distinguishes the two in a snapshot read years later.
    """
    return AudienceResolution(
        audience=EcoAudience.DIRECT_MANAGER, state=AudienceState.UNRESOLVED,
        user_ids=(), basis=BASIS_NO_CANONICAL_MANAGER)


def resolve_eco_recipients(
    db: Session, *, organization_id: int, worker_user_id: int,
) -> ResolvedRecipients:
    """Today's authoritative §33.1 recipients for one Worker in one tenant.

    Deterministic: the same database state produces the same answer, in matrix
    order, with each audience's holders in user-id order. Failures are explicit
    — ``CrossTenantRecipient`` for a boundary violation,
    ``RecipientAuthorityUnavailable`` for an unusable argument — and no
    audience is ever silently dropped: all four are always present in the
    result, carrying either identities or a stated reason for having none.
    """
    if organization_id is None or worker_user_id is None:
        raise RecipientAuthorityUnavailable(
            "organization_id and worker_user_id are both required")
    try:
        organization_id = int(organization_id)
        worker_user_id = int(worker_user_id)
    except (TypeError, ValueError) as exc:
        raise RecipientAuthorityUnavailable(
            f"recipient authority needs integer identities: {exc}") from exc

    resolutions: List[AudienceResolution] = []
    for audience in AUDIENCE_ORDER:
        if audience is EcoAudience.WORKER:
            resolutions.append(_resolve_worker(
                db, organization_id=organization_id,
                worker_user_id=worker_user_id))
        elif audience is EcoAudience.DIRECT_MANAGER:
            resolutions.append(_resolve_direct_manager(
                db, organization_id=organization_id,
                worker_user_id=worker_user_id))
        else:
            resolutions.append(_resolve_designatable(
                db, organization_id=organization_id, audience=audience))

    return ResolvedRecipients(
        organization_id=organization_id, worker_user_id=worker_user_id,
        audiences=tuple(resolutions))


# ═════════════════════════════════════════════════════════════════════════════
# The snapshot — capture, and historical read
# ═════════════════════════════════════════════════════════════════════════════

def build_recipient_snapshot(resolved: ResolvedRecipients) -> Dict[str, Any]:
    """The immutable event-time record, as plain JSON-safe data.

    DELIBERATELY CONTAINS NO TIMESTAMP. The event already carries
    ``created_at_utc``, and a second clock read here would make two resolutions
    of one unchanged tenant differ — which would break replay (§10.G) and move
    the ALERT content hash that covers this structure.

    The v1 keys are retained verbatim, so nothing that reads a pre-Stage-5
    snapshot has to learn a new shape to read a post-Stage-5 one.
    """
    return {
        "schema_version": SNAPSHOT_VERSION,
        "organization_id": int(resolved.organization_id),
        # v1 keys, kept. ``app.alert.service._resolve_worker`` reads the first
        # and fails closed without it.
        "worker_user_id": int(resolved.worker_user_id),
        "roles": [EcoAudience.WORKER.value],
        # v2: the full §33.1 answer, including the audiences that resolved to
        # nobody and why. Unresolved audiences are RECORDED, not omitted.
        "audiences": [entry.as_snapshot() for entry in resolved.audiences],
    }


def recipients_from_snapshot(
    snapshot: Optional[Mapping[str, Any]],
) -> Tuple[Dict[str, Any], ...]:
    """Read a frozen snapshot's audience entries. NO DATABASE ARGUMENT.

    That is the guarantee, expressed in the signature: this cannot consult the
    current designation table, so a historical read cannot rewrite history even
    by mistake. Ruling D-3.7 in one function.

    A v1 (Worker-only) snapshot is projected into the v2 shape on read rather
    than being migrated in place, because rewriting a frozen snapshot is the
    exact thing the ruling forbids.
    """
    data = dict(snapshot or {})
    entries = data.get("audiences")
    if isinstance(entries, list) and entries:
        return tuple(dict(entry) for entry in entries)

    worker_id = data.get("worker_user_id")
    if worker_id is None:
        return ()
    return ({
        "audience": EcoAudience.WORKER.value,
        "state": AudienceState.RESOLVED.value,
        "user_ids": [int(worker_id)],
        "basis": BASIS_EVENT_SUBJECT,
    },)


def original_recipient_user_ids(
    snapshot: Optional[Mapping[str, Any]],
) -> Tuple[int, ...]:
    """"All original recipients" (§33.1, threshold-withdrawn row) for GV-07.

    Resolved from the ORIGINAL event's frozen snapshot, which a withdrawal
    copies verbatim from the crossing it closes. De-duplicated across
    audiences, order preserved. Consults nothing current.
    """
    out: List[int] = []
    seen = set()
    for entry in recipients_from_snapshot(snapshot):
        for raw in _as_ids(entry.get("user_ids")):
            if raw not in seen:
                seen.add(raw)
                out.append(raw)
    return tuple(out)


def _as_ids(values: Any) -> Iterable[int]:
    if not isinstance(values, (list, tuple)):
        return ()
    out = []
    for value in values:
        try:
            out.append(int(value))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            continue
    return out


__all__ = [
    "SNAPSHOT_VERSION",
    "build_recipient_snapshot",
    "original_recipient_user_ids",
    "recipients_from_snapshot",
    "resolve_eco_recipients",
]

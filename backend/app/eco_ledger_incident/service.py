"""
Raising the ECO platform incident.
==================================

ZT-SPEC-ECO-002 v2.0 §24.15, owner ruling D-9.1.

ONE PUBLIC FUNCTION. It persists the incident and returns; it does not notify,
schedule, retry, escalate, dispatch or queue. §24.15 says "raise a platform
incident", and raising is a write.

DUCK-TYPED ON PURPOSE
---------------------
``ledger`` is read for its attributes and never imported. This package depends
on nothing in ``app/work_ledger``, which is what lets ``work_ledger.service``
depend on this package without a cycle -- and what keeps the dependency
one-directional, so an incident can never become a precondition for sealing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.orm import Session

from .models import EcoLedgerIncident
from .outcomes import (
    LEDGER_INTEGRITY_FAULT, IncidentOutcome, IncidentRefused, IncidentType,
)


@dataclass(frozen=True)
class IncidentRaise:
    """What one raise call did. In both outcomes the incident exists."""

    outcome: IncidentOutcome
    incident: EcoLedgerIncident

    @property
    def was_raised(self) -> bool:
        """True only when THIS call wrote the row."""
        return self.outcome is IncidentOutcome.RAISED


def find_incident_for_ledger(
    db: Session, ledger_id: int,
) -> Optional[EcoLedgerIncident]:
    """The incident for one authoritative ledger fault version, if any."""
    return (
        db.query(EcoLedgerIncident)
        .filter(EcoLedgerIncident.ledger_id == int(ledger_id))
        .one_or_none()
    )


def raise_ledger_integrity_incident(
    db: Session, *, ledger: Any, raised_by: str = "SYSTEM",
) -> IncidentRaise:
    """Persist the §24.15 incident for one established ledger fault.

    ``ledger`` is the ``LEDGER_INTEGRITY_FAULT`` row that was just written. It
    must already carry an ``id``, which is the case for every row this
    repository's ledger writer returns.

    Exactly once per authoritative ledger fault version: the existing row is
    returned when there is one, and ``uq_eco_ledger_incident_ledger`` is the
    database-level backstop under a race.

    Refuses rather than writing a false incident when the row is not a fault,
    or carries no reason.
    """
    state = getattr(ledger, "state", None)
    if state != LEDGER_INTEGRITY_FAULT:
        raise IncidentRefused(
            f"an incident may only be raised for a ledger version in "
            f"{LEDGER_INTEGRITY_FAULT}; this row is in {state!r}"
        )

    reason = getattr(ledger, "integrity_fault_reason", None)
    if not reason or not str(reason).strip():
        raise IncidentRefused(
            "an incident may only be raised with the ledger's recorded "
            "integrity_fault_reason; this row carries none"
        )

    ledger_id = getattr(ledger, "id", None)
    if ledger_id is None:
        raise IncidentRefused(
            "the ledger fault version has no id, so the incident would have "
            "no authoritative identity; flush the ledger write first"
        )

    existing = find_incident_for_ledger(db, int(ledger_id))
    if existing is not None:
        # The §24.15 obligation was discharged when that row was written.
        # Discharging it twice would report one defect as two.
        return IncidentRaise(IncidentOutcome.ALREADY_RAISED, existing)

    incident = EcoLedgerIncident(
        ledger_id=int(ledger_id),
        organization_id=int(ledger.organization_id),
        user_id=int(ledger.user_id),
        work_date=ledger.work_date,
        ledger_version=int(ledger.ledger_version),
        incident_type=IncidentType.LEDGER_INTEGRITY_FAULT.value,
        ledger_state=LEDGER_INTEGRITY_FAULT,
        integrity_fault_reason=str(reason),
        raised_by=str(raised_by)[:64],
    )
    db.add(incident)
    db.flush()
    return IncidentRaise(IncidentOutcome.RAISED, incident)


__all__ = [
    "IncidentRaise",
    "find_incident_for_ledger",
    "raise_ledger_integrity_incident",
]

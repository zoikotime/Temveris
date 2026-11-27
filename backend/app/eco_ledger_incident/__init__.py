"""
The ECO platform-incident primitive (ZT-SPEC-ECO-002 v2.0 §24.15, D-9.1).
=========================================================================

§24.15 obliges this control to "raise a platform incident" when a sealed
interval set is inadmissible. Detection, the refusal and the ledger state were
already built; the incident was not. This package is that one missing verb.

An incident is operational evidence, not a notification. It has no audience, no
recipient, no channel and no delivery, it is not routed through ``app/alert``,
and it never reaches the notification platform. ``outcomes.py`` says why at
length, and ``tests/test_eco_ledger_incident.py`` enforces it with an import
scan rather than a promise.
"""

from .models import EcoLedgerIncident
from .outcomes import (
    LEDGER_INTEGRITY_FAULT, IncidentOutcome, IncidentRefused, IncidentType,
)
from .service import (
    IncidentRaise, find_incident_for_ledger, raise_ledger_integrity_incident,
)

__all__ = [
    "EcoLedgerIncident",
    "LEDGER_INTEGRITY_FAULT",
    "IncidentOutcome",
    "IncidentRaise",
    "IncidentRefused",
    "IncidentType",
    "find_incident_for_ledger",
    "raise_ledger_integrity_incident",
]

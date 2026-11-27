"""
Closed vocabulary for the ECO platform-incident primitive.
==========================================================

ZT-SPEC-ECO-002 v2.0 §24.15, owner ruling D-9.1.

§24.15 is one sentence with four verbs, three of them prohibitions:

    "An interval with end <= start, or overlapping intervals in the sealed
    set, indicates a ledger defect. This control MUST NOT repair it, skip it,
    or clamp it. It MUST refuse to evaluate the day (state:
    LEDGER_INTEGRITY_FAULT), raise a platform incident, and evaluate nothing."

Detection, the refusal and the ledger state were already built. "Raise a
platform incident" was not: nothing in this repository persisted the incident,
so a claim that one had been raised was unfalsifiable. This package is that
one missing verb and nothing else.

WHAT AN INCIDENT IS NOT
-----------------------
It is operational evidence that a Tenant's ledger holds a defect. It is NOT a
notification:

* it has no audience, no recipient and no recipient snapshot;
* it has no channel, no delivery attempt, no outcome and no suppression;
* it has no correlation identifier and no dispatch pass;
* it is not routed through ``app/alert`` and never reaches the notification
  platform.

§33.1 registers four audiences and §33.2/§33.3 register exactly two
notification identifiers, both of them for "threshold reached". Neither
registers anything for a ledger defect. An incident that acquired an audience
would therefore have to invent one, and inventing an audience is how an
internal defect becomes a false statement about a Worker.

WHAT AN INCIDENT IS NOT, PART TWO
---------------------------------
It is also not any of the records this repository already keeps:

``EcoThresholdEvent``     a week's threshold crossing; a faulted day produces
                          no threshold event at all, by §24.15's "evaluate
                          nothing".
``EcoNotification``       week-shaped, NOT NULL foreign key to a threshold
                          event that does not exist here.
``eco_day_administrator_alert``
                          §24.3's REQUIREMENT_UNRESOLVED alert: a Tenant
                          CONFIGURATION defect, addressed to an administrator.
                          A ledger defect is a DATA defect and is addressed to
                          nobody.
``WorkEvidenceChain``     the evidence that produced a day; the incident is
                          about the day's evidence being inadmissible.
``AuditLog``              who did what. No actor did this.
"""

from __future__ import annotations

import enum

#: The ledger state whose establishment raises an incident. One value: this
#: primitive exists for §24.15 and is not a general incident store.
LEDGER_INTEGRITY_FAULT = "LEDGER_INTEGRITY_FAULT"


class IncidentType(str, enum.Enum):
    """Closed. A second member is an owner decision, not an implementation."""

    LEDGER_INTEGRITY_FAULT = LEDGER_INTEGRITY_FAULT
    """The sealed interval set is inadmissible (§24.15)."""


class IncidentOutcome(str, enum.Enum):
    """What one raise call did. Both values mean the incident exists."""

    RAISED = "RAISED"
    """This call persisted the incident."""

    ALREADY_RAISED = "ALREADY_RAISED"
    """An incident for this authoritative ledger fact already existed. The
    §24.15 obligation was already discharged and nothing was written."""


class IncidentRefused(Exception):
    """A raise was asked for against something that is not a ledger fault.

    Refusing is the point. Writing an incident for a row that is not in
    ``LEDGER_INTEGRITY_FAULT``, or one that carries no reason, would create
    operational evidence of a defect that the ledger does not assert — which
    is the same class of falsehood §24.15 exists to prevent.
    """


__all__ = [
    "LEDGER_INTEGRITY_FAULT",
    "IncidentOutcome",
    "IncidentRefused",
    "IncidentType",
]

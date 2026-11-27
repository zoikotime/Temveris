"""
Canonical work-state and provenance vocabularies for the sealed interval ledger.
================================================================================

STEP 1 of the owner-authorised Phase 2 foundation. Pure definitions: no I/O, no
ORM, no domain logic, no worked-time calculation.

WORK STATES
-----------
The eight canonical states from ZT-SPEC-ECO-002 §19 step 8. The ledger records
the *state* of an interval as observed evidence. It deliberately does NOT record
whether that state qualifies toward a requirement, or whether it is paid or
unpaid: those are policy interpretations resolved at evaluation against the
versioned break-treatment profile. Freezing a policy interpretation into the
evidence row would break §17 historical reproducibility — a day re-evaluated
under its correct historical policy would contradict its own stored flag.

``TRV`` and ``ONC`` are defined because the specification defines them. No
source of truth for them exists in this platform today, so the classifier will
emit none until one does. They are reserved, not fabricated.

PROVENANCE
----------
The nine values approved by the owner: eight at Gate 6, plus
``PRODUCER_DERIVED`` by decision D-18A. Provenance is a required
property of EVERY canonical interval — not only of intervals derived from a
session close (correction C-02). No interval may exist without it.

Provenance is IMMUTABLE. A change occurs only by superseding the ledger version
that carries it, never by mutating the row.

VERIFIED vs SYNTHESIZED
-----------------------
Only ``WORKER`` and ``ADMIN_OVERRIDE`` are verified departure evidence. The
platform has three paths that synthesize a departure the Worker never performed
— stale auto-close at local end-of-day, abandoned auto-close at last heartbeat
(or at clock-in when no heartbeat was ever seen), and offboarding closure — and
two that derive a break boundary from one of those. Per the owner ruling, and
per §7.2 which calls inferring an end of day "the single worst thing this
control could do", a synthesized boundary MUST NOT silently acquire the
evidentiary status of a genuine Worker departure.
"""

from __future__ import annotations

import enum
from typing import FrozenSet


class WorkState(str, enum.Enum):
    """Canonical interval state. str-valued so it serialises as-is."""

    WRK = "WRK"      # Working
    TRV = "TRV"      # Travel — reserved; no source exists in this platform yet
    ONC = "ONC"      # On-call — reserved; no source exists in this platform yet
    BRK_M = "BRK_M"  # Meal break — legally distinct from BRK_R (INV-4)
    BRK_R = "BRK_R"  # Rest break — legally distinct from BRK_M (INV-4)
    OFF = "OFF"      # Off / not working (includes observed connectivity gaps)
    LVE = "LVE"      # Approved leave overlapping the working day
    UNK = "UNK"      # Unattributable. Contributes nothing. Never guessed.


#: States for which no source of truth exists in this platform today. The
#: classifier must emit none of these until a source is introduced; recording
#: the gap is honest, inventing a source is not.
RESERVED_UNSOURCED_STATES: FrozenSet[WorkState] = frozenset({
    WorkState.TRV,
    WorkState.ONC,
})

#: The two break states. INV-4 requires they stay distinct; a single generic
#: "break" category MUST NOT exist in this control.
BREAK_STATES: FrozenSet[WorkState] = frozenset({
    WorkState.BRK_M,
    WorkState.BRK_R,
})


class Provenance(str, enum.Enum):
    """How a canonical interval boundary came to exist.

    Eight values owner-approved at Gate 6; ``PRODUCER_DERIVED`` added by owner
    decision D-18A. The single canonical vocabulary — there is no second enum.
    """

    # ── Session-close provenance ────────────────────────────────────────────
    WORKER = "WORKER"
    """Genuine Worker clock-out. Verified departure evidence."""

    ADMIN_OVERRIDE = "ADMIN_OVERRIDE"
    """Administrative correction. Verified administrative departure evidence."""

    STALE_AUTOCLOSE = "STALE_AUTOCLOSE"
    """Synthesized closure at organization-local end of day."""

    ABANDONED_AUTOCLOSE = "ABANDONED_AUTOCLOSE"
    """Synthesized closure at last heartbeat, or at clock-in where no heartbeat
    was ever observed."""

    OFFBOARDING = "OFFBOARDING"
    """Synthesized closure caused by offboarding."""

    # ── Interval-level provenance (correction C-02) ─────────────────────────
    CLASSIFIER_DERIVED = "CLASSIFIER_DERIVED"
    """Boundary produced deterministically by the classifier."""

    BREAK_REPAIR = "BREAK_REPAIR"
    """Boundary or state resulting from repair of a stored source break
    representation — i.e. a source record was written to close a gap."""

    BREAK_DERIVED = "BREAK_DERIVED"
    """Boundary or state derived deterministically by classification of an
    incomplete break, WITHOUT altering the underlying source record."""

    PRODUCER_DERIVED = "PRODUCER_DERIVED"
    """A canonical interval boundary computed deterministically by the
    forward-only Work Ledger producer from authoritative source records, at a
    point that is NOT a session departure.

    It asserts no departure, no closure, and no classifier authorship. It is
    admissible only on an interval boundary the producer itself authored, and
    only where the underlying source record is complete and was read without
    alteration.

    Added by owner decision D-18A. It exists because the producer must author
    boundaries the other eight values cannot describe truthfully — the split
    point where a complete break interrupts a working session, and the edges of
    an observed connectivity gap. Every alternative was false: WORKER and
    ADMIN_OVERRIDE assert a departure that did not occur (and would enter
    ``verified_departure_intervals``); the three synthesized values assert a
    closure that did not occur; CLASSIFIER_DERIVED would attribute authorship to
    a component that is not the producer; BREAK_REPAIR claims a source record
    was written, and BREAK_DERIVED describes an INCOMPLETE break derived from a
    synthesized closure.

    NOT a session-close origin. It is inadmissible on
    ``clock_session_boundary_event``, whose D-15 pairing constraint whitelists
    the five session-close values explicitly and is unchanged by this addition.
    """


#: The only provenance values that constitute verified departure evidence.
#: Everything else is a recorded closure, not an observed departure.
VERIFIED_DEPARTURE_PROVENANCE: FrozenSet[Provenance] = frozenset({
    Provenance.WORKER,
    Provenance.ADMIN_OVERRIDE,
})

#: Provenance values that describe a departure the Worker did not perform.
SYNTHESIZED_CLOSURE_PROVENANCE: FrozenSet[Provenance] = frozenset({
    Provenance.STALE_AUTOCLOSE,
    Provenance.ABANDONED_AUTOCLOSE,
    Provenance.OFFBOARDING,
})


#: Boundaries produced by deterministic derivation rather than by an observed
#: departure. Neither verified departure evidence nor a synthesized closure:
#: the classifier, a repair of a source break record, or the forward-only
#: producer computed the boundary.
DERIVED_BOUNDARY_PROVENANCE: FrozenSet[Provenance] = frozenset({
    Provenance.CLASSIFIER_DERIVED,
    Provenance.BREAK_REPAIR,
    Provenance.BREAK_DERIVED,
    # D-18A. Kept in this set rather than given a class of its own: its
    # evidentiary weight is exactly that of the other derived boundaries —
    # deterministic, and not an observed departure.
    Provenance.PRODUCER_DERIVED,
})


class ProvenanceClass(str, enum.Enum):
    """How much evidentiary weight a provenance value carries.

    This is a classification OF the nine owner-approved values, not a new
    vocabulary. It exists so "a synthesized closure must never be represented
    as verified Worker evidence" is enforceable in code rather than only
    stated in prose.
    """

    VERIFIED = "VERIFIED"
    """A departure the Worker or an administrator actually performed."""

    SYNTHESIZED = "SYNTHESIZED"
    """A closure the system performed on the Worker's behalf. The session was
    closed; no departure was observed."""

    DERIVED = "DERIVED"
    """A boundary computed deterministically from other evidence."""


class ProvenanceImmutable(Exception):
    """Raised when provenance is mutated on an interval that already carries one.

    Provenance is immutable by owner ruling at Gate 6. A correction changes
    provenance only by superseding the ledger version that carries it
    (INV-3, §23) — never by rewriting the row in place.
    """


def is_valid_provenance(provenance: object) -> bool:
    """True only for one of the nine owner-approved values.

    Case-sensitive and exact. ``"worker"`` is not ``"WORKER"``: a provenance
    column that silently accepts case variants stops being a closed vocabulary,
    and §32 warns that field and enum values are a product surface that cannot
    be corrected later without a breaking change.
    """
    if isinstance(provenance, Provenance):
        return True
    if not isinstance(provenance, str):
        return False
    return provenance in {p.value for p in Provenance}


def parse_provenance(provenance: object) -> Provenance:
    """Coerce to :class:`Provenance`, raising on anything unapproved.

    Introducing a FURTHER provenance value is a governance decision, not a
    code change (owner ruling: "Do not introduce new provenance values
    without stopping and reporting the governance conflict"). This function
    is where that rule is enforced. The ninth value, ``PRODUCER_DERIVED``,
    reached the vocabulary through exactly that route: the producer audit
    stopped, reported that no existing value could describe a
    producer-authored non-departure boundary truthfully, and the owner ruled
    (D-18A). A tenth requires the same.
    """
    if isinstance(provenance, Provenance):
        return provenance
    if isinstance(provenance, str) and provenance in {p.value for p in Provenance}:
        return Provenance(provenance)
    approved = ", ".join(sorted(p.value for p in Provenance))
    raise ValueError(
        f"{provenance!r} is not an owner-approved provenance value. "
        f"Approved values are: {approved}. Adding a new value requires an "
        f"owner ruling, not a code change."
    )


def classify_provenance(provenance: Provenance | str) -> ProvenanceClass:
    """The evidentiary class of an approved provenance value.

    Raises on an unapproved value rather than defaulting: guessing the class of
    an unknown provenance is exactly how synthesized evidence would acquire
    verified status by accident.
    """
    p = parse_provenance(provenance)
    if p in VERIFIED_DEPARTURE_PROVENANCE:
        return ProvenanceClass.VERIFIED
    if p in SYNTHESIZED_CLOSURE_PROVENANCE:
        return ProvenanceClass.SYNTHESIZED
    return ProvenanceClass.DERIVED


def is_verified_departure(provenance: Provenance | str) -> bool:
    """True only for a genuine Worker clock-out or an administrative override.

    A synthesized or derived boundary is never verified departure evidence.
    Unknown values are treated as NOT verified — the safe direction, since the
    consequence of wrongly trusting a fabricated departure is computing a
    shortfall against a time the Worker never chose.
    """
    try:
        return Provenance(provenance) in VERIFIED_DEPARTURE_PROVENANCE
    except ValueError:
        return False


class LedgerState(str, enum.Enum):
    """State of a Worker-day ledger. §19, §24."""

    OPEN = "OPEN"
    """Accumulating; not yet sealed."""

    SEALED = "SEALED"
    """End-of-day processing complete. No further automatic recalculation
    occurs without a correction event (§4 'Sealed')."""

    MISSING_CLOCK_OUT = "MISSING_CLOCK_OUT"
    """No verified departure evidence for the day. §24.6: a missing clock-out
    is NOT an early clock-out. No departure time is ever assumed."""

    NOT_A_WORKING_DAY = "NOT_A_WORKING_DAY"
    """§24.2. A record is still written so the week fold is complete and
    explainable."""

    LEDGER_INTEGRITY_FAULT = "LEDGER_INTEGRITY_FAULT"
    """§24.15. Overlapping or impossible intervals. The day is NOT evaluated,
    NOT repaired, NOT clamped, NOT skipped. Silently coping with corrupt
    evidence is how evidentiary systems lose their standing."""


__all__ = [
    "WorkState",
    "RESERVED_UNSOURCED_STATES",
    "BREAK_STATES",
    "Provenance",
    "VERIFIED_DEPARTURE_PROVENANCE",
    "SYNTHESIZED_CLOSURE_PROVENANCE",
    "DERIVED_BOUNDARY_PROVENANCE",
    "ProvenanceClass",
    "ProvenanceImmutable",
    "is_valid_provenance",
    "parse_provenance",
    "classify_provenance",
    "is_verified_departure",
    "LedgerState",
]

"""
The typed result of one EVAL-EVENT run.
=======================================

A VALUE, not an exception, following the ``work_ledger_producer`` and
``eval_day`` precedent: an outcome the caller must inspect is harder to drop on
the floor than an exception someone catches broadly.

WHY THE VOCABULARY IS FIXED HERE
--------------------------------
§32 warns that event names, field names and enum values are a product surface
that cannot be corrected later without a breaking change. Every value below is
therefore written once, checked against the §31.1 prohibited-term scanner, and
never derived from a variable.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Mapping, Optional, Tuple


class EventMode(str, enum.Enum):
    """Whether an event is operational or a shadow observation (D8)."""

    OPERATIONAL = "OPERATIONAL"
    """A real event for this tenant. Only these may ever reach delivery."""

    SHADOW = "SHADOW"
    """Produced while the control was observing only. Logically isolated: no
    operational read returns one, and no operational lifecycle decision may be
    satisfied by one. Retained for the validation period."""


class EventTier(str, enum.Enum):
    """The ECO EVENT tier. ``PRIMARY`` is the only tier this increment uses.

    Distinct from ``config_value.tier``, which carries the CONFIGURATION-domain
    label ``ECO_THRESHOLD``. The two axes never share a column.
    """

    PRIMARY = "PRIMARY"


class EventType(str, enum.Enum):
    """What the row records. §21.1 and §21.3."""

    ECO_THRESHOLD_REACHED = "ECO_THRESHOLD_REACHED"
    """The weekly total reached the threshold: ``W >= T``, equality included."""

    ECO_THRESHOLD_WITHDRAWN = "ECO_THRESHOLD_WITHDRAWN"
    """A later evaluation put the week below the threshold. The crossing it
    withdraws is retained, never deleted, hidden or back-dated (R-21.3.1)."""


class EventOutcome(str, enum.Enum):
    """What the run did. Exactly one per call."""

    CROSSED = "CROSSED"
    """A first crossing was recorded for this week at ``sequence = 1``."""

    RE_CROSSED = "RE_CROSSED"
    """A crossing was recorded after a withdrawal, at ``sequence >= 2``,
    referencing the withdrawal that preceded it (§21.4)."""

    ALREADY_CROSSED = "ALREADY_CROSSED"
    """An open crossing already exists. Nothing written — §21.2 makes this a
    natural-key fact, so a replay or a further shortfall cannot produce a
    second primary event."""

    NOT_CROSSED = "NOT_CROSSED"
    """``W < T`` and no open crossing existed. Nothing written."""

    WITHDRAWN = "WITHDRAWN"
    """``W < T`` while a crossing was open, so a withdrawal was recorded."""

    REFUSED = "REFUSED"
    """No determination was possible. ``refusal`` says why; nothing written."""


class RefusalReason(str, enum.Enum):
    """Why a run produced no determination. Never collapsed into one reason."""

    WEEK_NOT_FINAL = "WEEK_NOT_FINAL"
    """EVAL-WEEK did not return a weekly total — a deferred day, an unresolved
    requirement, a missing or ambiguous day record, or an inconsistent
    membership. A week that is not final has no W to compare, and a partial
    week must never be compared to a threshold."""

    THRESHOLD_NOT_RESOLVED = "THRESHOLD_NOT_RESOLVED"
    """No threshold governs this organization's workweek. There is no default
    and no borrowed figure, so the comparison simply cannot be made."""

    MALFORMED_WORKWEEK_KEY = "MALFORMED_WORKWEEK_KEY"
    """The key is not the canonical ``YYYY-MM-DD`` workweek start date."""


@dataclass(frozen=True)
class WeekProvenance:
    """The frozen evidence identifying exactly which weekly evaluation ran.

    D2: ``W`` alone is insufficient. Every field is copied from the
    authoritative ``WeekEvaluation`` (or, for ``day_content_hashes``, from the
    immutable ``EcoDayRecord`` rows it names) and is never recomputed later.
    A NULL in the evaluation stays NULL here — nothing is fabricated.
    """

    canonical_dates: Tuple[str, ...]
    day_record_ids: Tuple[int, ...]
    day_content_hashes: Tuple[str, ...]
    ledger_versions: Tuple[int, ...]
    policy_versions: Tuple[Optional[str], ...]
    config_versions: Tuple[Optional[str], ...]
    observed_states: Tuple[str, ...]
    blocking_dates: Tuple[str, ...]

    def as_json(self) -> Mapping[str, object]:
        return {
            "canonical_dates": list(self.canonical_dates),
            "day_record_ids": list(self.day_record_ids),
            "day_content_hashes": list(self.day_content_hashes),
            "ledger_versions": list(self.ledger_versions),
            "policy_versions": list(self.policy_versions),
            "config_versions": list(self.config_versions),
            "observed_states": list(self.observed_states),
            "blocking_dates": list(self.blocking_dates),
        }


@dataclass(frozen=True)
class RetroactiveLineage:
    """The persisted correction lineage underlying ``is_retroactive`` (D4).

    Determined from PERSISTED state only — which day records in the week are
    themselves corrections. No clock is read, so the same inputs always produce
    the same answer, and a replay months later reproduces it exactly.

    A LINEAGE INDICATOR, NOT CAUSAL PROOF
    -------------------------------------
    This flag says: *the week this event rests on contains corrected evidence.*
    It does NOT say, and must not be read as saying, that the correction is why
    the threshold was crossed. The persisted data cannot support that stronger
    claim, for two reasons:

    * a later ``ledger_version`` or ``evaluation_generation`` records that a day
      was re-evaluated, not that the re-evaluation changed the weekly total —
      a correction that left ``contributes_to_accumulator_minutes`` unchanged
      raises the flag exactly as one that caused the crossing does;
    * ``NOT_CROSSED`` writes no row, so there is no stored record of what an
      earlier evaluation concluded. "A prior evaluation did not cross" and
      "this is the first evaluation of a week that happens to contain corrected
      evidence" are therefore indistinguishable here.

    Establishing true causal attribution would need a persisted evaluation
    history to compare against. That is deliberately not built in this
    increment, and the counterfactual is not reconstructed or guessed at.
    """

    is_retroactive: bool

    #: Day-record ids in the week that are corrections of an earlier record.
    corrected_day_record_ids: Tuple[int, ...] = ()

    #: Ledger versions above 1 in the week — evidence that was superseded.
    corrected_ledger_versions: Tuple[int, ...] = ()

    #: Evaluation generations above 1 — the day was re-evaluated.
    re_evaluated_generations: Tuple[int, ...] = ()

    def as_json(self) -> Mapping[str, object]:
        return {
            "is_retroactive": self.is_retroactive,
            "corrected_day_record_ids": list(self.corrected_day_record_ids),
            "corrected_ledger_versions": list(self.corrected_ledger_versions),
            "re_evaluated_generations": list(self.re_evaluated_generations),
        }


@dataclass(frozen=True)
class ThresholdEventResult:
    """What happened, in a form the caller cannot misread as success.

    ``event_id`` is populated only when a row was written or an existing one
    was returned; ``refusal`` only on ``REFUSED``. Enforced at construction, so
    a caller cannot read an event id off a refusal.
    """

    outcome: EventOutcome
    organization_id: int
    user_id: int
    workweek_key: str
    mode: EventMode

    event_id: Optional[int] = None
    event_type: Optional[EventType] = None
    sequence: Optional[int] = None

    weekly_total_minutes: Optional[int] = None
    threshold_minutes: Optional[int] = None

    refusal: Optional[RefusalReason] = None
    detail: Optional[str] = None

    def __post_init__(self) -> None:
        if self.outcome is EventOutcome.REFUSED:
            if self.refusal is None:
                raise ValueError("a REFUSED result must carry a refusal reason")
            if self.event_id is not None:
                raise ValueError("a refusal wrote no event, so it carries no id")
            return
        if self.refusal is not None:
            raise ValueError(
                f"{self.outcome.value} must not carry a refusal reason")
        if self.outcome is EventOutcome.NOT_CROSSED:
            if self.event_id is not None:
                raise ValueError("NOT_CROSSED wrote nothing, so it has no id")
            return
        if self.event_id is None:
            raise ValueError(
                f"{self.outcome.value} must identify the event it concerns")

    @property
    def wrote_event(self) -> bool:
        """True only when this run actually appended a row."""
        return self.outcome in (EventOutcome.CROSSED, EventOutcome.RE_CROSSED,
                                EventOutcome.WITHDRAWN)

    @property
    def crossed(self) -> bool:
        """True when an open crossing exists for the week after this run."""
        return self.outcome in (EventOutcome.CROSSED, EventOutcome.RE_CROSSED,
                                EventOutcome.ALREADY_CROSSED)


__all__ = [
    "EventMode",
    "EventTier",
    "EventType",
    "EventOutcome",
    "RefusalReason",
    "WeekProvenance",
    "RetroactiveLineage",
    "ThresholdEventResult",
]

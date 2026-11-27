"""
The typed result of a forward-only producer run.
================================================

Every refusal is a VALUE, not an exception, following the
``eco_threshold_capture`` precedent: an outcome the caller must inspect is
harder to drop on the floor than an exception someone catches broadly, and a
refused day must be auditable rather than merely absent.

No user-facing messaging lives here. These are operator/observability values:
no ECO outcome, no early-clock-out verdict, no notification.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Mapping, Optional, Sequence


class ProducerOutcome(str, enum.Enum):
    """What the producer did. Exactly one per run."""

    SEALED = "SEALED"
    """A new ledger version was sealed from verified source evidence."""

    ALREADY_SEALED = "ALREADY_SEALED"
    """An identical ledger already existed; nothing was written (idempotent)."""

    REFUSED = "REFUSED"
    """The producer declined to seal. ``refusal`` says why. Nothing written."""

    LEDGER_INTEGRITY_FAULT = "LEDGER_INTEGRITY_FAULT"
    """Canonical intervals were inadmissible (§24.15). The day is refused and
    the fault recorded; nothing is repaired, clamped, merged or skipped."""

    SUPERSEDED = "SUPERSEDED"
    """A sealed day was corrected: the SOURCE evidence changed while the frozen
    context stayed identical, so a new version supersedes the old one (owner
    ruling D18-F3-S, Choice C). The prior version is retained and readable."""

    SOURCE_UNRESOLVED = "SOURCE_UNRESOLVED"
    """The source evidence could not be established at all — no eligible
    session for the target day. Distinct from REFUSED: there is nothing to
    refuse, as opposed to evidence that exists and is inadmissible."""


class RefusalReason(str, enum.Enum):
    """Why a day was refused. Never collapsed into one generic reason: an
    operator has to be able to tell "this tenant has no cutover" from "this
    Worker's session was auto-closed"."""

    # ── forward-only gates ──────────────────────────────────────────────────
    CUTOVER_NOT_SET = "CUTOVER_NOT_SET"
    """No durable producer cutover exists for this tenant (D-18C). Refused, not
    backfilled."""

    BEFORE_CUTOVER = "BEFORE_CUTOVER"
    """The target day's organization-local day START precedes the cutover."""

    # ── timezone (D-18E) ────────────────────────────────────────────────────
    TIMEZONE_NOT_CONFIGURED = "TIMEZONE_NOT_CONFIGURED"
    """``OrgSettings.default_timezone`` is NULL, empty or whitespace. The
    attendance path may fail open to a documented default; evidence may not."""

    TIMEZONE_INVALID = "TIMEZONE_INVALID"
    """Configured but not a loadable IANA zone."""

    TIMEZONE_HELPER_DIVERGENCE = "TIMEZONE_HELPER_DIVERGENCE"
    """The configured value and the zone the platform helper actually uses
    disagree. Sealing would record an offset that was not applied."""

    # ── working day (D-18D) ─────────────────────────────────────────────────
    WORKING_DAY_UNRESOLVED = "WORKING_DAY_UNRESOLVED"
    """The resolver's own source label is ``system``: no tier configured the
    working week, so the Worker would be measured against a schedule nobody
    chose."""

    WORKING_DAY_SOURCE_UNKNOWN = "WORKING_DAY_SOURCE_UNKNOWN"
    """The resolver reported a source outside its documented vocabulary."""

    # ── day closure (D18-F3) ────────────────────────────────────────────────
    DAY_NOT_ENDED = "DAY_NOT_ENDED"
    """The organization-local calendar day has not ended. Sealing it would
    produce partial evidence for a day still able to accrue work, and the rest
    of that day could then never be sealed. Future dates land here too."""

    SESSION_STILL_OPEN = "SESSION_STILL_OPEN"
    """The Worker holds a session that could still be attributed to this day.
    The day is not resolved, so it is not evidence yet."""

    # ── frozen context divergence (D18-F3-S) ────────────────────────────────
    FROZEN_CONTEXT_DIVERGENCE = "FROZEN_CONTEXT_DIVERGENCE"
    """The currently resolved context differs from the sealed version's frozen
    context — timezone, UTC offset, working-day result or working-day source.

    Owner ruling D18-F3-S (Choice C): a context change may NEVER trigger or
    accompany a supersession, because the superseding version would carry
    today's configuration and retroactively re-attribute a historical day.
    That is the §17 defect `timezone_iana` exists to prevent. The sealed frozen
    context stays authoritative and nothing is mutated."""

    SOURCE_FINGERPRINT_UNAVAILABLE = "SOURCE_FINGERPRINT_UNAVAILABLE"
    """The sealed version carries no persisted source-evidence fingerprint, so
    the producer cannot establish whether the source evidence is the same or
    different.

    Owner ruling D18-F3-S permits supersession ONLY when the source evidence
    genuinely DIFFERS. A missing fingerprint does not establish that — it
    establishes only that the comparison is impossible. Treating it as a
    difference would let the producer supersede a version it cannot compare,
    including one it did not author. So it fails CLOSED.

    Never treated as an empty hash, never recomputed and back-dated as though
    it had been persisted, and never silently upgraded."""

    # ── source sessions and D-15 boundary provenance ────────────────────────
    SESSION_OPEN = "SESSION_OPEN"
    """A session for the day has no clock-out. Not sealed as complete, and no
    departure is inferred."""

    SESSION_SYNTHESIZED_CLOSURE = "SESSION_SYNTHESIZED_CLOSURE"
    """The close boundary is STALE_AUTOCLOSE, ABANDONED_AUTOCLOSE or
    OFFBOARDING. Provenance-bearing evidence, but not a verified departure
    (D-17). Left for future classifier handling; never relabelled WORKER."""

    BOUNDARY_PROVENANCE_MISSING = "BOUNDARY_PROVENANCE_MISSING"
    """A session boundary has no D-15 event. A timestamp without a recorded
    origin is insufficient, and the origin is never inferred."""

    BOUNDARY_PROVENANCE_AMBIGUOUS = "BOUNDARY_PROVENANCE_AMBIGUOUS"
    """Boundary events for one (session, boundary) could not be total-ordered."""

    BOUNDARY_PROVENANCE_MISMATCH = "BOUNDARY_PROVENANCE_MISMATCH"
    """A D-15 event disagrees with the session it describes — subject Worker,
    organization, session identity, or the boundary instant itself."""

    # ── source record validity ──────────────────────────────────────────────
    NAIVE_TIMESTAMP = "NAIVE_TIMESTAMP"
    NON_POSITIVE_INTERVAL = "NON_POSITIVE_INTERVAL"
    BREAK_OPEN = "BREAK_OPEN"
    """A break overlapping the session has no end. Closure cannot be
    established, so the day is refused rather than repaired."""

    BREAK_MALFORMED = "BREAK_MALFORMED"
    OFFLINE_MALFORMED = "OFFLINE_MALFORMED"
    CROSS_WORKER_MISMATCH = "CROSS_WORKER_MISMATCH"
    CROSS_ORG_MISMATCH = "CROSS_ORG_MISMATCH"
    WORK_DATE_MISMATCH = "WORK_DATE_MISMATCH"
    """A source boundary does not belong to the target canonical local day. The
    caller's ``work_date`` is a lookup target, never taken on trust."""

    EVIDENCE_CONFLICT = "EVIDENCE_CONFLICT"
    """A ledger already exists for this day carrying different evidence.
    Correcting it is a supersession decision, not a re-seal."""


#: Refusals that mean "the platform is not configured to produce evidence for
#: this tenant/day yet", as opposed to "this day's evidence is bad".
CONFIGURATION_REFUSALS = frozenset({
    RefusalReason.CUTOVER_NOT_SET,
    RefusalReason.BEFORE_CUTOVER,
    RefusalReason.TIMEZONE_NOT_CONFIGURED,
    RefusalReason.TIMEZONE_INVALID,
    RefusalReason.TIMEZONE_HELPER_DIVERGENCE,
    RefusalReason.WORKING_DAY_UNRESOLVED,
    RefusalReason.WORKING_DAY_SOURCE_UNKNOWN,
    RefusalReason.DAY_NOT_ENDED,
    RefusalReason.FROZEN_CONTEXT_DIVERGENCE,
})


@dataclass(frozen=True)
class ProducerResult:
    """What happened, in a form the caller cannot misread as success.

    ``ledger_id``/``ledger_version`` are populated only on SEALED and
    ALREADY_SEALED; ``refusal`` only on REFUSED. Enforced at construction so a
    caller cannot read a ledger id off a refusal.
    """

    outcome: ProducerOutcome
    organization_id: int
    user_id: int
    work_date: date

    ledger_id: Optional[int] = None
    ledger_version: Optional[int] = None

    #: D18-F3-S: the version this run superseded. Set only on SUPERSEDED.
    superseded_from_version: Optional[int] = None

    #: Finding 4: the LEDGER_INTEGRITY_FAULT row this run recorded, when it
    #: recorded one. A SEPARATE field from ``ledger_id`` on purpose — the
    #: guard below still refuses ``ledger_id`` on every non-sealed outcome, so
    #: no caller can mistake a fault for sealed evidence.
    integrity_fault_ledger_id: Optional[int] = None

    refusal: Optional[RefusalReason] = None
    detail: Optional[str] = None

    #: Frozen day context, present whenever it was established before the run
    #: ended — useful on a refusal too, to show how far the gates got.
    timezone_iana: Optional[str] = None
    utc_offset_seconds: Optional[int] = None
    is_working_day: Optional[bool] = None
    working_day_source: Optional[str] = None
    producer_cutover_at: Optional[datetime] = None

    #: Counts by WorkState of the intervals produced. Counts, never durations:
    #: worked-time arithmetic belongs to evaluation, not to the producer.
    interval_counts: Mapping[str, int] = field(default_factory=dict)

    #: Source rows consulted, for audit. Opaque references, not foreign keys.
    source_refs: Sequence[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.outcome is ProducerOutcome.REFUSED and self.refusal is None:
            raise ValueError("a REFUSED result must carry a refusal reason")
        if self.outcome is not ProducerOutcome.REFUSED and self.refusal is not None:
            raise ValueError(
                f"{self.outcome.value} must not carry a refusal reason"
            )
        if self.outcome in (ProducerOutcome.SEALED,
                            ProducerOutcome.ALREADY_SEALED,
                            ProducerOutcome.SUPERSEDED):
            if self.ledger_id is None or self.ledger_version is None:
                raise ValueError(
                    f"{self.outcome.value} must identify the ledger it produced"
                )
        elif self.ledger_id is not None:
            raise ValueError(
                f"{self.outcome.value} must not carry a ledger id"
            )
        if self.outcome is ProducerOutcome.SUPERSEDED:
            if self.superseded_from_version is None:
                raise ValueError(
                    "SUPERSEDED must report the version it superseded"
                )
        elif self.superseded_from_version is not None:
            raise ValueError(
                f"{self.outcome.value} must not report a superseded version"
            )
        if (self.integrity_fault_ledger_id is not None
                and self.outcome is not ProducerOutcome.LEDGER_INTEGRITY_FAULT):
            raise ValueError(
                f"{self.outcome.value} must not carry an integrity-fault "
                f"ledger id"
            )

    @property
    def sealed(self) -> bool:
        """True only when evidence was actually written by this run.

        A supersession writes evidence too, so it counts. An integrity fault
        does NOT: a fault row records a refusal, not evidence.
        """
        return self.outcome in (ProducerOutcome.SEALED,
                                ProducerOutcome.SUPERSEDED)

    @property
    def has_ledger(self) -> bool:
        """True when a current sealed ledger exists for the day after this run."""
        return self.outcome in (ProducerOutcome.SEALED,
                                ProducerOutcome.ALREADY_SEALED,
                                ProducerOutcome.SUPERSEDED)


__all__ = [
    "ProducerOutcome",
    "RefusalReason",
    "CONFIGURATION_REFUSALS",
    "ProducerResult",
]

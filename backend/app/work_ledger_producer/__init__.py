"""
Temveris — the forward-only Work Ledger producer (§45).
========================================================

Transforms authoritative production attendance evidence into ``WorkDayLedger``
+ ``WorkStateInterval`` records. It prepares canonical evidence; it does not
interpret that evidence into an ECO outcome.

OWNER DECISIONS THIS PACKAGE IMPLEMENTS
---------------------------------------
* **D-16 / D-18E** — forward-only timezone. ``OrgSettings.default_timezone`` is
  read directly and refused when absent or invalid; conversion runs through the
  existing ``_tz`` helpers; the IANA name and the offset in force for the
  attributed day are frozen at seal. No historical reconstruction.
* **D-17** — the producer supplies evidence, not day states. Only a verified
  departure (``WORKER`` / ``ADMIN_OVERRIDE``) is sealable. ``STALE_AUTOCLOSE``,
  ``ABANDONED_AUTOCLOSE`` and ``OFFBOARDING`` are refused, never relabelled.
  ``MISSING_CLOCK_OUT`` is not emitted here.
* **D-18** — breaks are GAPS. No ``BRK_M``/``BRK_R``, and the worker-selected
  ``"break"``/``"lunch"`` labels are never read as legal categories.
* **D-18A** — producer-authored non-departure boundaries carry
  ``PRODUCER_DERIVED``.
* **D-18B** — ``OFF`` comes from ``OfflineInterval`` applied windows and
  carries ``PRODUCER_DERIVED``.
* **D-18C** — a durable per-tenant ``producer_cutover_at`` watermark, monotonic
  and read by the producer itself, never supplied by a caller.
* **D-18D** — the working-day result and the resolver's own source label are
  frozen onto the ledger; a ``system`` source refuses.
* **D-19** — ``TimeAdjustment`` stays outside the evidence model; an admin
  correction reaches the producer only as the ``ADMIN_OVERRIDE`` boundary event
  D-15 already recorded.
* **D-20** — no ``LVE``. Leave remains an evaluation input.
* **D-21** — the working-day result is resolved and frozen at seal, never
  reconstructed; ``ShiftAssignment`` is a plan, never an attendance fact.

WHAT IT NEVER DOES
------------------
No historical backfill, no "rebuild all days", no startup sweep, no migration
that invokes it. No classifier, legal matrix, EVAL-DAY, EVAL-WEEK, EVAL-EVENT,
notification or threshold logic. No cached totals as worked time. No provenance
inference of any kind: absent origin means refusal.
"""

from .context import (
    PERMITTED_WORKING_DAY_SOURCES,
    UNRESOLVED_WORKING_DAY_SOURCE,
    DayContext,
    resolve_day_context,
    resolve_timezone,
    resolve_working_day,
)
from .cutover import (
    SYSTEM_ACTOR_LABEL,
    get_producer_cutover,
    set_producer_cutover,
)
from .evidence import (
    SessionEvidence,
    Window,
    build_intervals,
    final_departure,
    resolve_session_evidence,
    source_evidence_fingerprint,
)
from .append_only_ddl import (
    APPEND_ONLY_DDL_BY_DIALECT,
    CUTOVER_HISTORY_TABLE,
    SUPPORTED_DIALECTS,
    append_only_statements,
)
from .models import (
    ProducerCutoverImmutable,
    ProducerCutoverMovedBackward,
    WorkLedgerProducerCutoverHistory,
)
from .outcomes import (
    CONFIGURATION_REFUSALS,
    ProducerOutcome,
    ProducerResult,
    RefusalReason,
)
from .producer import PRODUCER_ACTOR, produce_and_seal_work_day

__all__ = [
    # the one entry point
    "produce_and_seal_work_day",
    "PRODUCER_ACTOR",
    # outcomes
    "ProducerOutcome",
    "RefusalReason",
    "ProducerResult",
    "CONFIGURATION_REFUSALS",
    # cutover (D-18C, replaced by append-only history under D18-F1)
    "WorkLedgerProducerCutoverHistory",
    "ProducerCutoverMovedBackward",
    "ProducerCutoverImmutable",
    "CUTOVER_HISTORY_TABLE",
    "APPEND_ONLY_DDL_BY_DIALECT",
    "SUPPORTED_DIALECTS",
    "append_only_statements",
    "get_producer_cutover",
    "set_producer_cutover",
    "SYSTEM_ACTOR_LABEL",
    # frozen day context (D-18D / D-18E)
    "DayContext",
    "resolve_day_context",
    "resolve_timezone",
    "resolve_working_day",
    "PERMITTED_WORKING_DAY_SOURCES",
    "UNRESOLVED_WORKING_DAY_SOURCE",
    # evidence
    "SessionEvidence",
    "Window",
    "resolve_session_evidence",
    "build_intervals",
    "source_evidence_fingerprint",
    "final_departure",
]

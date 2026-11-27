"""
The ECO threshold reader.
=========================

One question: **what threshold governed this organization's workweek?**

    resolve_threshold(organization_id=..., as_of_workweek_start=..., provider=...)

Read-only, deterministic, and fail-closed. No clock is read, no configuration is
written, no default exists to fall back to.

ORGANIZATION-SCOPED BY CONSTRUCTION
-----------------------------------
There is no ``user_id`` parameter. Not "it is ignored" — it cannot be supplied.
The ECO threshold is organization-scoped, and the requirement resolver beside it
has worker-scope REPLACEMENT semantics (a per-Worker value set replaces the
organization-wide one outright). Inheriting that here would make an
organization's threshold invisible to any Worker who happened to carry a
per-Worker requirement override. A signature that cannot express a per-Worker
lookup cannot make that mistake, which is the same reason the ledger stopped
accepting a caller-supplied ``workweek_key``.

THE AS-OF DATE IS SUPPLIED, NEVER DERIVED
-----------------------------------------
The caller passes the workweek START date — in practice
``date.fromisoformat(work_day_ledger.workweek_key)``, the persisted W4 identity
of a sealed day. This module does not call ``week_start_date()``, does not
inspect schedule configuration, does not look at today, and does not use
evaluation time. Deriving a week here would read *current* configuration and so
reintroduce the precise defect W4 closed: a sealed day's week identity is what
was persisted, not what would be computed now.

One authoritative threshold therefore governs a whole workweek, and a
configuration change effective mid-week cannot alter it — not by special-casing,
but because a version effective after the week's start date is simply never the
latest one at or before it.

FAIL-CLOSED, AND WHY IT IS NOT NEGOTIABLE HERE
----------------------------------------------
There is no default threshold anywhere in this system, by ruling. A threshold of
zero would be reached by every workweek; a borrowed one (a daily expected-hours
figure, a break allowance, a risk cutoff) would be a different quantity wearing
this one's name. So every path that cannot produce exactly one valid
organization-scoped value returns a non-resolved outcome carrying no number.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional, Sequence

from .keys import (
    ECO_THRESHOLD_KEY,
    ECO_THRESHOLD_TIER,
    MAX_THRESHOLD_MINUTES,
    MIN_THRESHOLD_MINUTES,
)
from .outcomes import ThresholdOutcome, ThresholdResolution
from .ports import ThresholdCandidate, ThresholdProvider

# ── Defect tokens ────────────────────────────────────────────────────────────
# Stable strings so an administrator-facing caller can branch without parsing
# prose, and so a test can assert WHICH defect was found rather than only that
# something was wrong.
DEFECT_TIER_NOT_CANONICAL = "TIER_NOT_CANONICAL"
DEFECT_AMBIGUOUS_ROWS = "AMBIGUOUS_THRESHOLD_ROWS"
DEFECT_VALUE_ABSENT = "VALUE_ABSENT"
DEFECT_VALUE_OUT_OF_RANGE = "VALUE_OUT_OF_RANGE"
DEFECT_WORKER_SCOPED = "WORKER_SCOPED_ROW_PRESENT"


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def resolve_threshold(
    *,
    organization_id: int,
    as_of_workweek_start: date | datetime,
    provider: ThresholdProvider,
) -> ThresholdResolution:
    """The organization's ECO threshold as of a workweek start date.

    ``as_of_workweek_start`` is the workweek START date — the persisted W4
    identity, parsed. It is not a clock reading and not derived here.

    Deterministic: the same organization, date and stored configuration always
    produce the same result, and nothing about the order rows were stored in can
    change it.
    """
    on = _as_date(as_of_workweek_start)

    # ── 1. Which configuration version governed that workweek's start? ───────
    # Step 6's algorithm, unmodified and not reimplemented. Below the historical
    # floor this is None, and there is no live fallback to reach for.
    version = provider.version_as_of(organization_id=organization_id, as_of=on)
    if version is None:
        return ThresholdResolution(
            outcome=ThresholdOutcome.NO_CONFIG_VERSION,
            organization_id=organization_id,
            as_of=on,
        )

    supplied: Sequence[ThresholdCandidate] = provider.threshold_candidates(
        organization_id=organization_id, version=version,
    )
    # The port promises canonical-key rows only; this narrows to them anyway, so
    # that a provider returning more than it promised cannot change the answer.
    # A row under any other key is not threshold configuration at all, so it is
    # ignored here rather than reported as a threshold defect.
    rows: List[ThresholdCandidate] = [
        r for r in supplied if r.key == ECO_THRESHOLD_KEY
    ]

    def unresolved(outcome: ThresholdOutcome, defect: str) -> ThresholdResolution:
        return ThresholdResolution(
            outcome=outcome,
            organization_id=organization_id,
            as_of=on,
            config_version_id=version.config_version_id,
            effective_from=version.effective_from,
            defect=defect,
        )

    # ── 2. A per-Worker threshold row is prohibited configuration ────────────
    # Checked before "not configured", per the ruled definition of that outcome,
    # and checked unconditionally: its presence fails the whole resolution even
    # when a valid organization row sits beside it. Silently preferring the
    # organization row would leave someone believing a per-Worker threshold was
    # in force when it never was, and this layer's whole purpose is that a
    # figure can be explained years later.
    worker_scoped: List[ThresholdCandidate] = [
        r for r in rows if r.scope_user_id is not None
    ]
    if worker_scoped:
        return unresolved(
            ThresholdOutcome.WORKER_SCOPED_THRESHOLD_PROHIBITED,
            DEFECT_WORKER_SCOPED,
        )

    # ── 3. Narrow to organization scope ──────────────────────────────────────
    # The narrowing happens here rather than in a WHERE clause so that a
    # prohibited or mis-tiered row stays observable on the way in. A filter that
    # hides malformed configuration turns a reportable defect into silence.
    org_scoped = [r for r in rows if r.scope_user_id is None]

    if not org_scoped:
        return ThresholdResolution(
            outcome=ThresholdOutcome.THRESHOLD_NOT_CONFIGURED,
            organization_id=organization_id,
            as_of=on,
            config_version_id=version.config_version_id,
            effective_from=version.effective_from,
        )

    # ── 4. Exactly one, or nothing ───────────────────────────────────────────
    # The unique index already makes two rows identical in (version, scope,
    # tier, key) unstorable; two rows differing only in TIER are storable, and
    # that is the ambiguity guarded here. Not resolved by rank — the candidate
    # type has no rank to sort by — and not resolved by preferring the
    # correctly-tiered one, because "which of these did the administrator mean"
    # has no answer this code is entitled to invent.
    if len(org_scoped) > 1:
        return unresolved(ThresholdOutcome.THRESHOLD_INVALID, DEFECT_AMBIGUOUS_ROWS)

    row = org_scoped[0]

    # ── 5. The row must be the canonical one ─────────────────────────────────
    # A canonical-key row under any other tier is INVALID, never
    # NOT_CONFIGURED: somebody configured a threshold and it cannot be used,
    # which is a different fact from nobody having configured one.
    if row.tier != ECO_THRESHOLD_TIER:
        return unresolved(ThresholdOutcome.THRESHOLD_INVALID, DEFECT_TIER_NOT_CANONICAL)

    # ── 6. The value must be present and in range ────────────────────────────
    # NULL means "configured but supplies no value", which for a single-valued
    # domain with no lower rank to consult is simply unusable.
    if row.value_minutes is None:
        return unresolved(ThresholdOutcome.THRESHOLD_INVALID, DEFECT_VALUE_ABSENT)

    minutes = int(row.value_minutes)
    # Re-validated here rather than trusted to the database CHECK: a database
    # built by create_all and one built by Alembic must not be assumed to agree,
    # and a reader that is fail-closed only when storage is well-formed is not
    # fail-closed.
    if not (MIN_THRESHOLD_MINUTES <= minutes <= MAX_THRESHOLD_MINUTES):
        return unresolved(
            ThresholdOutcome.THRESHOLD_INVALID, DEFECT_VALUE_OUT_OF_RANGE)

    return ThresholdResolution(
        outcome=ThresholdOutcome.RESOLVED,
        organization_id=organization_id,
        as_of=on,
        threshold_minutes=minutes,
        config_version_id=version.config_version_id,
        effective_from=version.effective_from,
    )


__all__ = [
    "resolve_threshold",
    "DEFECT_TIER_NOT_CANONICAL",
    "DEFECT_AMBIGUOUS_ROWS",
    "DEFECT_VALUE_ABSENT",
    "DEFECT_VALUE_OUT_OF_RANGE",
    "DEFECT_WORKER_SCOPED",
]

"""
Strict requirement resolution — the fail-closed evaluation path.
================================================================

STEP 5 of the owner-authorised Phase 2 foundation.

TWO PATHS, DELIBERATELY NOT COLLAPSED (owner ruling D-10 Option A)
------------------------------------------------------------------
CLOCK-OUT PATH — fail-open, and NOT TOUCHED BY THIS MODULE.
    ``attendance_policy.resolve_expected_work_seconds`` keeps its contract
    ("Always returns a positive int; never raises") and its terminal fallback.
    That behaviour is not sloppiness: it exists so a policy error can never
    block a departure, which is what preserves INV-1. Step 5 adds a sibling; it
    changes nothing on the clock-out path, and a test pins that.

EVALUATION PATH — fail-closed, implemented here.
    Where no requirement resolves, this resolver says so and supplies no
    number. §10.2: shortfall MUST NOT be computed, MUST NOT be assumed zero,
    MUST NOT be assumed to be the default.

The two are different operations on the same data, and conflating them is what
made this look like an either/or in the original audit. A Worker still clocks
out; the day simply is not evaluated.

WHAT THIS MODULE DOES NOT DO
----------------------------
* No worked-time calculation. It resolves what is *required*, never what was
  *worked*. Asserted by an import-graph test.
* No Activity / screenshot / application-name dependency (INV-6, INV-7).
* No ECO v2 calculation, UI, API, notification or rollout.
* No default. Anywhere. There is no constant in this module that could become
  one by accident.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional, Sequence, Tuple

from .outcomes import (
    InvalidCandidate,
    RequirementResolution,
    ResolutionOutcome,
)
from .ports import ConfigVersionProvider, ConfigVersionRef, RequirementCandidate

#: Upper bound on a single day's requirement. A day cannot require more than
#: the day contains. Matches the platform's existing 24h guard
#: (``attendance_policy._MAX_WORK_SECONDS``) but is defined independently so
#: this module takes no dependency on the fail-open resolver.
MAX_REQUIRED_MINUTES: int = 24 * 60


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def _validate(candidate: RequirementCandidate) -> Optional[InvalidCandidate]:
    """Reject a configured value that cannot be a day's requirement.

    Returns the rejection, or ``None`` when the value is usable. A rejected
    candidate does NOT silently fall through to the next rank as though the
    tier were unconfigured — it is recorded, so an administrator sees that the
    tier answered and answered wrongly.
    """
    raw = candidate.minutes

    if isinstance(raw, bool) or not isinstance(raw, int):
        # bool is an int subclass in Python; True would otherwise mean 1 minute.
        return InvalidCandidate(
            tier=candidate.tier, rank=candidate.rank, raw_value=raw,
            reason="requirement must be an integer number of minutes",
        )
    if raw <= 0:
        return InvalidCandidate(
            tier=candidate.tier, rank=candidate.rank, raw_value=raw,
            reason="requirement must be positive; zero would silently mean "
                   "'no shortfall is possible'",
        )
    if raw > MAX_REQUIRED_MINUTES:
        return InvalidCandidate(
            tier=candidate.tier, rank=candidate.rank, raw_value=raw,
            reason=f"requirement exceeds {MAX_REQUIRED_MINUTES} minutes in a day",
        )
    return None


def resolve_requirement_strict(
    *,
    organization_id: int,
    user_id: int,
    as_of: date | datetime,
    provider: ConfigVersionProvider,
) -> RequirementResolution:
    """Resolve a Worker's required minutes for one date, fail-closed.

    Deterministic: the same inputs and the same configuration version always
    produce the same result, regardless of the order the provider returns
    candidates in (INV-8).

    Precedence (§10.2): candidates are ordered by ``rank``, lowest first. The
    first rank that yields a VALID value wins. Lower ranks are not consulted
    and are never blended.

    Returns, never raises, for the three ordinary outcomes — a caller on the
    evaluation path checks ``is_evaluable`` rather than catching.
    """
    on = _as_date(as_of)

    # ── 1. Which immutable configuration version governs this date? ──────────
    version: Optional[ConfigVersionRef] = provider.version_as_of(
        organization_id=organization_id, as_of=on
    )
    if version is None:
        # Gate 7 historical floor: the date predates the first captured
        # version. Live configuration is NOT substituted — doing so would make
        # a historical figure move whenever policy is edited, which is exactly
        # the §17 defect the versioning rule exists to prevent.
        return RequirementResolution(
            outcome=ResolutionOutcome.NO_VERSION,
            organization_id=organization_id,
            user_id=user_id,
            as_of=on,
        )

    # ── 2. Ask every tier, for that version only ─────────────────────────────
    candidates: Sequence[RequirementCandidate] = provider.requirement_candidates(
        organization_id=organization_id,
        user_id=user_id,
        as_of=on,
        version=version,
    )

    # Sorted by (rank, tier): rank is precedence, tier breaks a tie so two
    # providers returning the same ranks in different orders cannot produce
    # different answers.
    ordered = sorted(candidates, key=lambda c: (c.rank, c.tier))
    chain: List[str] = []
    invalid: List[InvalidCandidate] = []

    for candidate in ordered:
        chain.append(candidate.tier)

        if candidate.minutes is None:
            continue  # tier configured but silent — consult the next rank

        rejection = _validate(candidate)
        if rejection is not None:
            invalid.append(rejection)
            continue  # recorded, not silently treated as absent

        # ── 3. First valid rank wins. Lower ranks are not consulted. ─────────
        return RequirementResolution(
            outcome=ResolutionOutcome.RESOLVED,
            organization_id=organization_id,
            user_id=user_id,
            as_of=on,
            required_minutes=candidate.minutes,
            source_tier=candidate.tier,
            source_rank=candidate.rank,
            source_ref=candidate.source_ref,
            config_version_id=version.config_version_id,
            effective_from=version.effective_from,
            chain_evaluated=tuple(chain),
            invalid_candidates=tuple(invalid),
        )

    # ── 4. Configuration exists, but no rank answered usably ─────────────────
    # §10.2: no shortfall, no zero, no default. An operational alert to the
    # Tenant administrator; nothing shown to the Worker (§24.3).
    return RequirementResolution(
        outcome=ResolutionOutcome.REQUIREMENT_UNRESOLVED,
        organization_id=organization_id,
        user_id=user_id,
        as_of=on,
        config_version_id=version.config_version_id,
        effective_from=version.effective_from,
        chain_evaluated=tuple(chain),
        invalid_candidates=tuple(invalid),
    )


__all__ = ["resolve_requirement_strict", "MAX_REQUIRED_MINUTES"]

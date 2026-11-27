"""
Capture and historical resolution for the §11 Policy artifact.
==============================================================

Two functions and nothing else:

    capture_policy_version(...)   append one immutable version
    resolve_policy_as_of(...)     which policy governed this date

``resolve_policy_as_of`` is the read half of the specification's
``resolvePolicyVersion(tenant, work_date)``. It is NOT wired into
``evaluateDay()`` — that is a later increment, and this package deliberately
imports nothing from ``eval_day``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .errors import InvalidPolicyVersion, PolicyVersionCollision
from .models import PolicyVersion, compute_policy_content_hash
from .outcomes import PolicyOutcome, PolicyResolution
from .vocabulary import (
    DEFAULT_RETROACTIVE_HORIZON_DAYS,
    NETTING_DECISION_PROVENANCES,
    is_valid_netting_mode,
    parse_netting_mode,
)


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def capture_policy_version(
    db: Session,
    *,
    organization_id: int,
    policy_version: str,
    effective_from: date | datetime,
    counts_toward_week: bool,
    netting_mode: str,
    netting_decision_provenance: str,
    grace_minutes: int,
    captured_by: str,
    break_treatment_ref: Optional[str] = None,
    rounding_profile_ref: Optional[str] = None,
    surplus_cap_per_day_minutes: Optional[int] = None,
    surplus_cap_per_period_minutes: Optional[int] = None,
    retroactive_horizon_days: Optional[int] = (
        DEFAULT_RETROACTIVE_HORIZON_DAYS),
    captured_at_utc: Optional[datetime] = None,
) -> PolicyVersion:
    """Append one immutable policy version.

    Every governed value is validated BEFORE anything is added to the session,
    so a rejected capture leaves the store byte-identical rather than a partial
    row to be rolled back. The unique constraints remain the net against a
    concurrent writer that passes these checks and then loses the race.

    THE TWO SURPLUS CAPS DEFAULT TO ``None``, THE HORIZON TO 90
    -----------------------------------------------------------
    Deliberately asymmetric, because the specification is.

    §21.5 states the horizon's default outright — "a Tenant-configurable
    retroactive horizon (default 90 days)" — so recording 90 when the
    Tenant states nothing is the specification's answer, not an invented
    one.

    §15.1 states no default for the caps, and there is no safe one to
    invent: a 0 cap means "bank nothing" and an absent cap means "nobody
    said". Under ``NONE`` there is no surplus to cap, so ``None`` is
    truthful; under a bank mode ``None`` is a policy gap MODEL-NETTING
    must refuse. Manufacturing a number here would remove its ability to
    tell those apart.

    WHY A BANK MODE WITH NO CAPS IS NOT REFUSED HERE
    ------------------------------------------------
    §15.1's "surplus MUST be capped" constrains BANK CONSUMPTION, and
    consumption is MODEL-NETTING's (Stage 3). Refusing the capture would
    also invalidate a shape two existing suites legitimately use to test
    that the mode is merely RECORDED in this increment. The nullable
    representation is what lets Stage 3 fail closed on the real thing: a
    fold that would consume an uncapped bank.

    Does not commit; the caller owns the transaction boundary.
    """
    eff = _as_date(effective_from)

    if not (policy_version or "").strip():
        raise InvalidPolicyVersion("policy_version must be a non-empty key")
    if not (captured_by or "").strip():
        raise InvalidPolicyVersion("captured_by must be a non-empty label")
    if not isinstance(counts_toward_week, bool):
        # Explicit by ruling: never inferred, and never a truthy integer that
        # happens to work.
        raise InvalidPolicyVersion(
            f"counts_toward_week must be an explicit bool, got "
            f"{type(counts_toward_week).__name__}")
    if not is_valid_netting_mode(netting_mode):
        raise InvalidPolicyVersion(
            f"{netting_mode!r} is not a governed netting mode")
    mode = parse_netting_mode(netting_mode).value
    # Owner ruling D5: a NEW capture must say how the mode was selected, and
    # OD-02: A makes EXPLICIT_TENANT_CHOICE the only thing it can honestly
    # say. Refused BEFORE the session is touched, like every other governed
    # value here, so a rejected capture leaves the store byte-identical.
    #
    # NULL is NOT accepted from a caller. It is the grandfathered state of
    # rows written before the column existed (ruling F: D), and a caller
    # claiming it would be back-dating itself into that exemption.
    if netting_decision_provenance not in NETTING_DECISION_PROVENANCES:
        raise InvalidPolicyVersion(
            f"{netting_decision_provenance!r} is not a governed netting "
            f"decision provenance; a new capture must record "
            f"{sorted(NETTING_DECISION_PROVENANCES)} "
            f"(§15 / §44.3, owner rulings D5 and OD-02: A)")
    if not isinstance(grace_minutes, int) or isinstance(grace_minutes, bool):
        raise InvalidPolicyVersion("grace_minutes must be an integer")
    if grace_minutes < 0:
        raise InvalidPolicyVersion(
            f"grace_minutes must be >= 0, got {grace_minutes}")
    for name, value in (
        ("surplus_cap_per_day_minutes", surplus_cap_per_day_minutes),
        ("surplus_cap_per_period_minutes", surplus_cap_per_period_minutes),
        ("retroactive_horizon_days", retroactive_horizon_days),
    ):
        if value is None:
            continue
        # bool is an int subclass; True would otherwise store as a cap of 1.
        if not isinstance(value, int) or isinstance(value, bool):
            raise InvalidPolicyVersion(f"{name} must be an integer or None")
        if value < 0:
            raise InvalidPolicyVersion(f"{name} must be >= 0, got {value}")

    clash = db.execute(
        select(PolicyVersion).where(
            PolicyVersion.organization_id == organization_id,
            PolicyVersion.policy_version == policy_version,
        )
    ).scalars().first()
    if clash is not None:
        raise PolicyVersionCollision(
            f"policy version {policy_version!r} already captured for "
            f"organization {organization_id}; versions are immutable")

    occupied = db.execute(
        select(PolicyVersion).where(
            PolicyVersion.organization_id == organization_id,
            PolicyVersion.effective_from == eff,
        )
    ).scalars().first()
    if occupied is not None:
        raise PolicyVersionCollision(
            f"organization {organization_id} already has a policy effective "
            f"{eff.isoformat()} ({occupied.policy_version!r}); two versions "
            f"cannot start on the same date")

    row = PolicyVersion(
        organization_id=organization_id,
        policy_version=policy_version,
        effective_from=eff,
        captured_by=captured_by,
        counts_toward_week=counts_toward_week,
        break_treatment_ref=break_treatment_ref,
        netting_mode=mode,
        # Provenance about the CAPTURE, not the policy -- so it is stored on
        # the row and deliberately absent from the digest below, exactly as
        # `captured_by` and `captured_at_utc` already are.
        netting_decision_provenance=netting_decision_provenance,
        rounding_profile_ref=rounding_profile_ref,
        grace_minutes=grace_minutes,
        surplus_cap_per_day_minutes=surplus_cap_per_day_minutes,
        surplus_cap_per_period_minutes=surplus_cap_per_period_minutes,
        retroactive_horizon_days=retroactive_horizon_days,
        content_hash=compute_policy_content_hash(
            organization_id=organization_id,
            policy_version=policy_version,
            effective_from=eff,
            counts_toward_week=counts_toward_week,
            break_treatment_ref=break_treatment_ref,
            netting_mode=mode,
            rounding_profile_ref=rounding_profile_ref,
            grace_minutes=grace_minutes,
            surplus_cap_per_day_minutes=surplus_cap_per_day_minutes,
            surplus_cap_per_period_minutes=surplus_cap_per_period_minutes,
            retroactive_horizon_days=retroactive_horizon_days,
        ),
    )
    if captured_at_utc is not None:
        row.captured_at_utc = captured_at_utc
    db.add(row)
    db.flush()
    return row


def resolve_policy_as_of(
    db: Session,
    *,
    organization_id: int,
    as_of: date | datetime,
) -> PolicyResolution:
    """Which policy governed ``organization_id`` on ``as_of``.

    The latest version whose ``effective_from <= as_of``. NEVER the latest
    version outright — that is what makes a future-dated policy invisible to
    today and to every historical date before it takes effect.

    Fails closed in three ways, each typed: below the historical floor
    (``NO_VERSION``), two versions claiming one date (``AMBIGUOUS``), and a
    stored value outside its governed domain (``INVALID``). No live
    configuration is consulted in any of them.
    """
    on = _as_date(as_of)

    rows = db.execute(
        select(PolicyVersion)
        .where(
            PolicyVersion.organization_id == organization_id,
            PolicyVersion.effective_from <= on,
        )
        .order_by(PolicyVersion.effective_from.desc(), PolicyVersion.id.desc())
        .limit(2)
    ).scalars().all()

    if not rows:
        return PolicyResolution(
            outcome=PolicyOutcome.NO_VERSION,
            organization_id=organization_id, as_of=on)

    winner = rows[0]
    # Two rows sharing the newest effective date means the answer is not
    # determined by the data. The unique constraint prevents it; detected here
    # so a database that somehow held both refuses rather than picking one.
    if len(rows) > 1 and rows[1].effective_from == winner.effective_from:
        return PolicyResolution(
            outcome=PolicyOutcome.AMBIGUOUS,
            organization_id=organization_id, as_of=on,
            invalid_reason=(
                f"two policy versions claim effective_from "
                f"{winner.effective_from.isoformat()}"))

    if not is_valid_netting_mode(winner.netting_mode):
        return PolicyResolution(
            outcome=PolicyOutcome.INVALID,
            organization_id=organization_id, as_of=on,
            invalid_reason=f"netting_mode {winner.netting_mode!r}")
    if winner.grace_minutes is None or winner.grace_minutes < 0:
        return PolicyResolution(
            outcome=PolicyOutcome.INVALID,
            organization_id=organization_id, as_of=on,
            invalid_reason=f"grace_minutes {winner.grace_minutes!r}")
    if winner.counts_toward_week is None:
        return PolicyResolution(
            outcome=PolicyOutcome.INVALID,
            organization_id=organization_id, as_of=on,
            invalid_reason="counts_toward_week is NULL")
    # D-6.8 / D-7.1: domain-checked only WHEN PRESENT. A NULL is not
    # invalid — it is a row that records no cap or no horizon, which is a
    # fact about the policy rather than a corrupt value. It is carried
    # through unchanged and NEVER replaced with a default here:
    # substituting 90 for a NULL horizon would assert a policy the Tenant
    # never configured, and the caller could no longer tell the two apart.
    for _name, _value in (
        ("surplus_cap_per_day_minutes",
         winner.surplus_cap_per_day_minutes),
        ("surplus_cap_per_period_minutes",
         winner.surplus_cap_per_period_minutes),
        ("retroactive_horizon_days", winner.retroactive_horizon_days),
    ):
        if _value is not None and _value < 0:
            return PolicyResolution(
                outcome=PolicyOutcome.INVALID,
                organization_id=organization_id, as_of=on,
                invalid_reason=f"{_name} {_value!r}")

    return PolicyResolution(
        outcome=PolicyOutcome.RESOLVED,
        organization_id=organization_id,
        as_of=on,
        policy_version=winner.policy_version,
        effective_from=winner.effective_from,
        content_hash=winner.content_hash,
        counts_toward_week=bool(winner.counts_toward_week),
        break_treatment_ref=winner.break_treatment_ref,
        netting_mode=winner.netting_mode,
        rounding_profile_ref=winner.rounding_profile_ref,
        grace_minutes=int(winner.grace_minutes),
        surplus_cap_per_day_minutes=(
            None if winner.surplus_cap_per_day_minutes is None
            else int(winner.surplus_cap_per_day_minutes)),
        surplus_cap_per_period_minutes=(
            None if winner.surplus_cap_per_period_minutes is None
            else int(winner.surplus_cap_per_period_minutes)),
        retroactive_horizon_days=(
            None if winner.retroactive_horizon_days is None
            else int(winner.retroactive_horizon_days)),
    )


__all__ = ["capture_policy_version", "resolve_policy_as_of"]

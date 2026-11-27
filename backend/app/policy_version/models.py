"""
The §11 Policy artifact — immutable, effective-dated.
=====================================================

WHAT IT OWNS, AND WHAT IT DELIBERATELY DOES NOT
------------------------------------------------
§11 lists seven policy attributes. Two of them already have an authoritative
historical home and are NOT duplicated here — duplicating either would create a
second answer to one question:

    daily_minutes   -> ``ConfigValue`` / Requirement Resolution. GV-27's
                       classification scoping hangs off that row.
    is_working_day  -> ``WorkDayLedger``, frozen at seal by D-18D so a later
                       roster edit cannot change what a sealed day was
                       measured against.

This table owns the remaining five:

    counts_toward_week · break_treatment_ref · netting_mode
    rounding_profile_ref · grace_minutes

THE TWO ``*_ref`` COLUMNS ARE OPAQUE, ON PURPOSE
-------------------------------------------------
§13's break-treatment profile and §16's rounding profile are separate versioned
artifacts, and both are DEFERRED. These columns therefore store an opaque
reference and nothing else: no profile table exists, no contents are inferred,
and neither column may be used to encode a profile's definition. They are
nullable because nothing can supply a real reference yet — a NOT NULL column
would force a placeholder, and a placeholder is an invented profile.

WHY IT IS NOT ``ConfigVersion``
-------------------------------
``resolvePolicyVersion(tenant, work_date)`` is step 1 of the specification's
``evaluateDay()``; ``resolveRequirement`` is step 2. They are separate resolves
returning separate objects, and the DayRecord at step 15 carries
``policy_version`` beside ``config_version``. ``EcoDayRecord`` already models
them as distinct identity components. Collapsing them here would make that
existing contract a lie.

NOTHING CONSUMES THIS YET
-------------------------
Capture and read only. ``evaluateDay()`` is untouched, no evaluation populates
``EcoDayRecord.policy_version``, and GV-25 stays blocked. The artifact exists so
the rule in force on a past date is recordable; wiring it is a later increment.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import (
    Boolean, CheckConstraint, Column, Date, DateTime, Index, Integer, String,
    UniqueConstraint, event, text,
)
from sqlalchemy import inspect as sa_inspect

from database.database import Base

from .append_only_ddl import append_only_statements
from .errors import PolicyVersionImmutable
from .vocabulary import NETTING_MODES


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PolicyVersion(Base):
    """One immutable capture of a Tenant's §11 policy, effective from a date."""

    __tablename__ = "policy_version"

    id = Column(Integer, primary_key=True, index=True)

    organization_id = Column(Integer, nullable=False, index=True)

    #: The Tenant-facing version key. ``String(64)`` to match
    #: ``EcoDayRecord.policy_version``, which is where a resolved policy will
    #: eventually be recorded — a wider column here would make a value
    #: storable that the day record could not hold.
    policy_version = Column(String(64), nullable=False)

    #: Inclusive lower bound. The version governs every date from here until a
    #: later version starts.
    effective_from = Column(Date, nullable=False)

    captured_at_utc = Column(DateTime(timezone=True), nullable=False,
                             default=_utcnow)

    #: WHO captured it, as a label rather than a user foreign key.
    #:
    #: Deliberate: a foreign key would pull this table into the Ruling M
    #: Worker-history retention boundary, and whether a captured policy should
    #: make its author undeletable is a question no ruling has answered. A
    #: label records the fact without asking it.
    captured_by = Column(String(255), nullable=False)

    # ── the five owned attributes ───────────────────────────────────────────
    #: Whether shortfall on a day governed by this policy contributes to the
    #: weekly accumulator at all. EXPLICIT: never inferred from another
    #: attribute, because "no netting" and "does not count" are different
    #: statements and a Tenant may mean either.
    counts_toward_week = Column(Boolean, nullable=False)

    #: Opaque reference to a §13 break-treatment profile. See the module
    #: docstring — the profile itself is deferred.
    break_treatment_ref = Column(String(64), nullable=True)

    #: §15. Stored as the governed string; the CHECK below pins the vocabulary
    #: in the schema so an unrecognised mode is unstorable, not merely
    #: unselected.
    netting_mode = Column(String(32), nullable=False)

    #: HOW the mode was selected (owner ruling D5; OD-02: A). NULLABLE, and
    #: that is the whole of owner ruling F: D. ``policy_version`` is
    #: append-only with database triggers, so rows captured before this column
    #: existed can never be back-filled; NULL is therefore the grandfathered
    #: historical state and the gate reads it as such. A NEW capture must
    #: supply a value -- ``service.capture_policy_version`` refuses without one
    #: -- so the requirement binds going forward without rewriting the past.
    #:
    #: DELIBERATELY OUTSIDE ``content_hash``. That digest covers the POLICY,
    #: and its own docstring states the rule this follows: ``captured_at_utc``
    #: and ``captured_by`` are excluded because "they are provenance about the
    #: capture, not the policy... two Tenants with the same rules would be
    #: unable to recognise that they match". How a mode was chosen is exactly
    #: that kind of fact, and hashing it would make two identical policies
    #: hash differently.
    netting_decision_provenance = Column(String(32), nullable=True)

    #: Opaque reference to a §16 rounding profile. Also deferred.
    rounding_profile_ref = Column(String(64), nullable=True)

    #: §11: "Per-day de-minimis below which gross shortfall is recorded but not
    #: attributed." NOT the attendance/lateness ``grace_minutes`` in
    #: ``schedule_policy`` — that one decides whether an ARRIVAL is late. Same
    #: name, unrelated meaning; they must never be read for one another.
    #:
    #: No column default. §11 says the default is 0, but a default applied
    #: silently at the database would let a capture omit the field and still
    #: look deliberate.
    grace_minutes = Column(Integer, nullable=False)

    #: ── D-6.8: §15.1's two surplus caps ────────────────────────────────────
    #: §15.1: "Surplus MUST be capped per day and per period, per Tenant
    #: configuration, to prevent one long day erasing a week." TWO caps, not
    #: one — a per-day bound on how much a single day may contribute, and a
    #: bound on the standing total for the period the netting mode governs
    #: (the workweek under WEEKLY_BANK, the configured reference period under
    #: PERIOD_BANK).
    #:
    #: Minutes, matching every other quantity on this row.
    #:
    #: NULLABLE, and NULL means "no cap recorded" — never "no cap applies" and
    #: never zero. Under ``NONE`` there is no surplus to cap, so NULL is the
    #: truthful value; under a bank mode a NULL cap is a policy gap that
    #: MODEL-NETTING must refuse rather than paper over. Distinguishing the two
    #: is why this is not a NOT NULL column with a 0 default: a 0 cap means
    #: "bank nothing", which is a real and very different policy.
    #:
    #: NOT a stored bank. §15.1 / INV-3: "The bank balance is itself a fold
    #: over day records, never a stored counter." These are the LIMITS the fold
    #: is computed under, versioned with the rest of the policy; no balance,
    #: counter or accumulator is introduced anywhere by this column or its
    #: sibling.
    surplus_cap_per_day_minutes = Column(Integer, nullable=True)
    surplus_cap_per_period_minutes = Column(Integer, nullable=True)

    #: ── D-7.1: §21.5's retroactive notification horizon ───────────────────
    #: Whole days. Bounds how far back a retroactive threshold event may be
    #: RAISED; §21.5 is explicit that beyond it "the recomputation still occurs
    #: and the record is still corrected, but no notification is sent", so this
    #: governs notification eligibility and never persistence.
    #:
    #: It lives here, on the versioned policy, because the horizon that governs
    #: a correction is the one in force for the WEEK being corrected — not the
    #: one in force today. A mutable setting could not answer that question.
    #:
    #: NULLABLE only because rows captured before this column existed carry no
    #: horizon and none may be fabricated for them (the migration performs no
    #: backfill). Every capture through the service records an explicit value,
    #: defaulting to §21.5's own 90. Resolution NEVER substitutes a default for
    #: a NULL — see ``resolve_policy_as_of``.
    retroactive_horizon_days = Column(Integer, nullable=True)

    #: SHA-256 over the identity and all owned attributes.
    content_hash = Column(String(64), nullable=False)

    __table_args__ = (
        # Two versions cannot claim the same start date: "which policy governed
        # that day" must have exactly one answer. This is also what makes the
        # resolver's ambiguity refusal a belt-and-braces check rather than the
        # only defence.
        UniqueConstraint(
            "organization_id", "effective_from",
            name="uq_policy_version_org_effective_from",
        ),
        UniqueConstraint(
            "organization_id", "policy_version",
            name="uq_policy_version_org_version_key",
        ),
        # The hot read: "latest version at or before this date, for this org".
        Index("ix_policy_version_org_effective",
              "organization_id", "effective_from"),
        CheckConstraint("length(policy_version) > 0",
                        name="ck_policy_version_key_nonempty"),
        CheckConstraint("length(content_hash) > 0",
                        name="ck_policy_version_hash_nonempty"),
        # Owner ruling D5 / OD-02: A. NULL is permitted and means a
        # grandfathered historical row (ruling F: D); any NON-NULL value must
        # be the one provenance a new capture may claim.
        CheckConstraint(
            "netting_decision_provenance IS NULL OR "
            "netting_decision_provenance = 'EXPLICIT_TENANT_CHOICE'",
            name="ck_policy_version_netting_provenance"),
        CheckConstraint("length(captured_by) > 0",
                        name="ck_policy_version_captured_by_nonempty"),
        CheckConstraint("grace_minutes >= 0",
                        name="ck_policy_version_grace_nonnegative"),
        # Each is checked only WHEN PRESENT: NULL is a legitimate "not
        # recorded", and a CHECK that rejected it would force the fabrication
        # the migration refuses to perform.
        CheckConstraint(
            "surplus_cap_per_day_minutes IS NULL "
            "OR surplus_cap_per_day_minutes >= 0",
            name="ck_policy_version_surplus_cap_day_nonnegative"),
        CheckConstraint(
            "surplus_cap_per_period_minutes IS NULL "
            "OR surplus_cap_per_period_minutes >= 0",
            name="ck_policy_version_surplus_cap_period_nonnegative"),
        CheckConstraint(
            "retroactive_horizon_days IS NULL "
            "OR retroactive_horizon_days >= 0",
            name="ck_policy_version_retroactive_horizon_nonnegative"),
        CheckConstraint(
            "netting_mode IN ('" + "', '".join(sorted(NETTING_MODES)) + "')",
            name="ck_policy_version_netting_mode",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<PolicyVersion org={self.organization_id} "
            f"{self.policy_version!r} from={self.effective_from}>"
        )


def compute_policy_content_hash(
    *,
    organization_id: int,
    policy_version: str,
    effective_from: date,
    counts_toward_week: bool,
    break_treatment_ref: Optional[str],
    netting_mode: str,
    rounding_profile_ref: Optional[str],
    grace_minutes: int,
    surplus_cap_per_day_minutes: Optional[int],
    surplus_cap_per_period_minutes: Optional[int],
    retroactive_horizon_days: Optional[int],
) -> str:
    """SHA-256 over a canonical serialisation of identity + every attribute.

    Deterministic across processes and platforms: sorted keys, no insignificant
    whitespace, ASCII escaping, so the byte sequence does not depend on the
    platform's default encoding.

    ``captured_at_utc`` and ``captured_by`` are EXCLUDED — they are provenance
    about the capture, not the policy. Including them would make an identical
    policy hash differently depending on when it was recorded, and two Tenants
    with the same rules would be unable to recognise that they match.

    Identity is inside the digest so a policy cannot be moved to a different
    organization or version key and still verify.
    """
    payload: Mapping[str, Any] = {
        "organization_id": int(organization_id),
        "policy_version": policy_version,
        "effective_from": effective_from.isoformat(),
        "counts_toward_week": bool(counts_toward_week),
        "break_treatment_ref": break_treatment_ref,
        "netting_mode": netting_mode,
        "rounding_profile_ref": rounding_profile_ref,
        "grace_minutes": int(grace_minutes),
        # D-6.8 / D-7.1. Added to the EXISTING canonical projection rather
        # than hashed separately: one digest covers the whole policy, so a
        # cap or horizon that changed without the version changing is
        # detectable by the same check that already covers grace.
        #
        # None is carried through as JSON null, exactly as the two optional
        # refs above already are, so "no cap recorded" and "a cap of 0" hash
        # differently — which is the distinction MODEL-NETTING will need.
        "surplus_cap_per_day_minutes": (
            None if surplus_cap_per_day_minutes is None
            else int(surplus_cap_per_day_minutes)),
        "surplus_cap_per_period_minutes": (
            None if surplus_cap_per_period_minutes is None
            else int(surplus_cap_per_period_minutes)),
        "retroactive_horizon_days": (
            None if retroactive_horizon_days is None
            else int(retroactive_horizon_days)),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ═════════════════════════════════════════════════════════════════════════════
# Append-only enforcement — the database is the guarantee
# ═════════════════════════════════════════════════════════════════════════════
# Attached to the TABLE so ``Base.metadata.create_all()`` carries it. The
# Alembic revision emits the SAME statements explicitly, because
# ``op.create_table`` builds its own Table object and this listener does not
# fire for it.

@event.listens_for(PolicyVersion.__table__, "after_create")
def _install_policy_guard(target, connection, **kw):
    for statement in append_only_statements(connection.dialect.name):
        connection.execute(text(statement))


# ═════════════════════════════════════════════════════════════════════════════
# ORM refusal — defence in depth, never the guarantee
# ═════════════════════════════════════════════════════════════════════════════
# A Core-level bulk UPDATE or DELETE bypasses these entirely; that is what the
# triggers above are for. These exist so the ordinary mistake fails with a
# sentence rather than a database error.

def _refuse(target, verb: str):
    raise PolicyVersionImmutable(
        f"PolicyVersion id={getattr(target, 'id', None)} cannot be {verb}. "
        f"A captured policy is append-only — capture a LATER version instead, "
        f"so the rule that governed any past day stays reproducible."
    )


@event.listens_for(PolicyVersion, "before_update", propagate=True)
def _reject_update(mapper, connection, target):
    if sa_inspect(target).modified:
        _refuse(target, "modified")


@event.listens_for(PolicyVersion, "before_delete", propagate=True)
def _reject_delete(mapper, connection, target):
    _refuse(target, "deleted")


__all__ = ["PolicyVersion", "compute_policy_content_hash"]

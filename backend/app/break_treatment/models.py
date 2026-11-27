"""
The BreakTreatmentProfile table — immutable, effective-dated, content-addressed.
===============================================================================

Shaped after ``PolicyVersion`` deliberately: the same identity columns, the same
append-only enforcement, the same "captured_by is a label, not a foreign key"
rule, and the same exclusion of capture provenance from the content hash.
Two Tenants whose treatment rules are identical must produce identical hashes.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import (
    Boolean, CheckConstraint, Column, Date, DateTime, Integer, String,
    UniqueConstraint, event,
)
from sqlalchemy import inspect as sa_inspect

from database.database import Base

from .errors import BreakTreatmentImmutable

PROFILE_TABLE = "break_treatment_profile"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BreakTreatmentProfile(Base):
    """One Tenant's break-treatment rules, in force from a date.

    Identity is ``(organization_id, profile_ref)``; applicability is
    ``(organization_id, effective_from)``. Both are unique, for the same reason
    ``PolicyVersion`` makes both unique: a key must name exactly one profile,
    and a date must resolve to exactly one answer rather than to whichever row
    the query happened to order first.
    """

    __tablename__ = PROFILE_TABLE

    id = Column(Integer, primary_key=True, index=True)

    organization_id = Column(Integer, nullable=False, index=True)

    #: The public key. String(64) to match ``PolicyVersion.break_treatment_ref``,
    #: which is what points here — the reference and the referent must be able
    #: to hold the same value.
    profile_ref = Column(String(64), nullable=False)

    #: The date this profile begins to govern. Resolution selects the latest
    #: version whose ``effective_from <= work_date``; below the earliest there
    #: is NO answer, never today's.
    effective_from = Column(Date, nullable=False)

    captured_at_utc = Column(DateTime(timezone=True), nullable=False,
                             default=_utcnow)

    #: A label, never a foreign key — the same convention as
    #: ``ConfigVersion.captured_by_label`` and ``PolicyVersion.captured_by``.
    #: A profile must stay explainable after the capturing account is gone.
    captured_by = Column(String(255), nullable=False)

    # ── §13.2 attributes carried by this increment ──────────────────────────
    #: "Whether BRK_M minutes count toward the daily requirement. Typically
    #: false." Carried so P-4's "BRK_M never qualifies through this path" is a
    #: stated Tenant rule rather than a hardcoded assumption.
    meal_qualifies = Column(Boolean, nullable=False)

    #: "Whether BRK_R minutes count. Typically true in jurisdictions where rest
    #: is paid." P-4 gates rest qualification on exactly this flag.
    rest_qualifies = Column(Boolean, nullable=False)

    #: "Maximum BRK_R minutes that may qualify per day. Prevents an over-long
    #: rest break from silently covering a shortfall. null = uncapped."
    #:
    #: NULLABLE, and NULL is a real answer: §13.2 defines it as uncapped, not
    #: as unresolved. Applied CUMULATIVELY per day (P-4) — never per interval,
    #: which is the arithmetic §19.1 exists to pin down.
    rest_qualifying_cap_minutes = Column(Integer, nullable=True)

    #: SHA-256 over identity + the three governed attributes. Capture
    #: provenance is excluded, so an identical profile hashes identically
    #: whenever it was recorded.
    content_hash = Column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "profile_ref",
                         name="uq_break_treatment_profile_ref"),
        UniqueConstraint("organization_id", "effective_from",
                         name="uq_break_treatment_profile_effective"),
        CheckConstraint("length(trim(profile_ref)) > 0",
                        name="ck_break_treatment_profile_ref_nonempty"),
        CheckConstraint("length(trim(captured_by)) > 0",
                        name="ck_break_treatment_profile_actor_nonempty"),
        CheckConstraint("length(content_hash) > 0",
                        name="ck_break_treatment_profile_hash_nonempty"),
        # null = uncapped; a negative cap is not a weaker cap, it is nonsense.
        CheckConstraint(
            "rest_qualifying_cap_minutes IS NULL OR "
            "rest_qualifying_cap_minutes >= 0",
            name="ck_break_treatment_profile_cap_nonnegative"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (f"<BreakTreatmentProfile org={self.organization_id} "
                f"{self.profile_ref} from={self.effective_from}>")


def compute_profile_content_hash(
    *,
    organization_id: int,
    profile_ref: str,
    effective_from: date,
    meal_qualifies: bool,
    rest_qualifies: bool,
    rest_qualifying_cap_minutes: Optional[int],
) -> str:
    """SHA-256 over a canonical serialisation of identity + the attributes.

    Sorted keys, no insignificant whitespace, ASCII escaping, so the byte
    sequence does not depend on the platform's default encoding. Identity is
    inside the digest, so a profile cannot be moved to another organization or
    key and still verify.
    """
    payload: Mapping[str, Any] = {
        "organization_id": int(organization_id),
        "profile_ref": profile_ref,
        "effective_from": effective_from.isoformat(),
        "meal_qualifies": bool(meal_qualifies),
        "rest_qualifies": bool(rest_qualifies),
        "rest_qualifying_cap_minutes": (
            None if rest_qualifying_cap_minutes is None
            else int(rest_qualifying_cap_minutes)),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ══════════════════════════════════════════════════════════════════════════════
# Append-only enforcement
# ══════════════════════════════════════════════════════════════════════════════
def _refuse(target, verb: str):
    raise BreakTreatmentImmutable(
        f"BreakTreatmentProfile id={getattr(target, 'id', None)} cannot be "
        f"{verb}. A changed rule is a NEW profile version; historical "
        f"evaluations keep the treatment they were actually decided under."
    )


@event.listens_for(BreakTreatmentProfile, "before_update", propagate=True)
def _reject_update(mapper, connection, target):
    state = sa_inspect(target)
    if not state.modified:
        return
    if any(state.attrs[name].history.has_changes()
           for name in mapper.columns.keys()):
        _refuse(target, "updated")


@event.listens_for(BreakTreatmentProfile, "before_delete", propagate=True)
def _reject_delete(mapper, connection, target):
    _refuse(target, "deleted")


__all__ = ["BreakTreatmentProfile", "compute_profile_content_hash",
           "PROFILE_TABLE"]

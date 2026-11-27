"""
Making a test Tenant §44.3-ready, in ONE place.
================================================

Two suites now need a Tenant that passes all nine enablement gates: the gate
checker's own suite, and the rollout suite, because owner ruling D6 makes the
gates a precondition of SHADOW → ENABLED. A second copy of this setup would
drift, and the day it drifted one suite would be testing a Tenant the other
considers unready.

NOTHING HERE IS EVIDENCE OF ANYTHING
------------------------------------
Every row is written into an in-memory SQLite database created by the calling
test. No real Tenant is configured, no real rollout is enrolled, and no real
DPIA, consultation or worker notice is recorded. The point of these builders is
to let a test assert that the gate REFUSES when a piece is missing — which
requires being able to supply the pieces.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

UTC = timezone.utc

#: The governed classification rows a §44.3-ready Tenant is resolved against.
#: DE is in §40.4's named set, so it exercises the consultation gate; GB
#: exercises the UK half of the DPIA gate without being EU.
GERMANY = "DE"
UNITED_KINGDOM = "GB"


def create_schema(engine):
    """Create every table the gate checker reads, on ONE isolated engine.

    Imports each model package first: a table absent from ``Base.metadata`` is
    not created, and the failure surfaces much later as "no such table" inside
    an unrelated assertion.
    """
    from database.database import Base

    for module in ("break_classification", "break_treatment",
                   "clock_session_boundary", "compliance_evidence",
                   "config_versioning", "eco_day_alert", "eco_ledger_incident",
                   "eval_day", "eval_event", "alert", "jurisdiction",
                   "notification_platform", "policy_version",
                   "recipient_authority", "rollout", "eco_view_authority",
                   "work_ledger", "work_ledger_producer"):
        try:
            __import__(f"app.{module}.models")
        except ModuleNotFoundError:
            __import__(f"app.{module}")
    import app.eval_day.leave_snapshot  # noqa: F401
    import database.models  # noqa: F401

    Base.metadata.create_all(engine)


# ── Operational evidence. Each builder satisfies exactly ONE gate. ──────────
def workweek_evidence(db, *, organization_id, week_start="MONDAY",
                      timezone_iana="Europe/Berlin"):
    import json

    import database.models as dbm
    from app import _tz
    from app.schedule_policy import keys as K

    if timezone_iana is not None:
        db.add(dbm.OrgSettings(organization_id=organization_id,
                               default_timezone=timezone_iana))
    if week_start is not None:
        db.add(dbm.AppSettings(organization_id=organization_id, team_key=None,
                               user_id=None, key=K.SETTING_WEEK_START_DAY,
                               value=week_start))
    db.add(dbm.AppSettings(
        organization_id=organization_id, team_key=None, user_id=None,
        key=K.SETTING_WORKING_DAYS,
        value=json.dumps(["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY",
                          "FRIDAY"])))
    db.commit()
    _tz.invalidate_tz_cache()


def requirement_evidence(db, *, organization_id, minutes=480):
    from app.config_first_capture.service import (
        REQUIREMENT_RANK, REQUIREMENT_TIER,
    )
    from app.config_versioning import DatabaseConfigVersionProvider
    from app.requirement_resolution import RequirementCandidate

    DatabaseConfigVersionProvider(db).capture_version(
        organization_id=organization_id,
        config_version_id=f"ready-cfg-{organization_id}",
        effective_from=date(2026, 1, 1),
        candidates=[RequirementCandidate(tier=REQUIREMENT_TIER,
                                         rank=REQUIREMENT_RANK,
                                         minutes=minutes)])
    db.commit()


def netting_evidence(db, *, organization_id, netting_mode="NONE"):
    from tests._eco_policy import capture_test_policy

    capture_test_policy(db, organization_id=organization_id,
                        netting_mode=netting_mode)


def break_treatment_evidence(db, *, organization_id):
    from app.break_treatment.models import BreakTreatmentProfile

    db.add(BreakTreatmentProfile(
        organization_id=organization_id, profile_ref=f"brk-{organization_id}",
        effective_from=date(2026, 1, 1),
        captured_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
        captured_by="owner@example.test",
        meal_qualifies=False, rest_qualifies=True,
        rest_qualifying_cap_minutes=30, content_hash="0" * 64))
    db.commit()


def recipient_evidence(db, *, organization_id, user_id):
    from app.recipient_authority import EcoRecipientDesignation

    db.add(EcoRecipientDesignation(organization_id=organization_id,
                                   audience="PRIMARY_ADMIN", user_id=user_id))
    db.commit()


def operational_evidence(db, *, organization_id, admin_user_id):
    """The five gates that describe configuration (owner ruling G: C)."""
    workweek_evidence(db, organization_id=organization_id)
    requirement_evidence(db, organization_id=organization_id)
    netting_evidence(db, organization_id=organization_id)
    break_treatment_evidence(db, organization_id=organization_id)
    recipient_evidence(db, organization_id=organization_id,
                       user_id=admin_user_id)


# ── Jurisdiction and compliance evidence ────────────────────────────────────
class _Actor:
    """The minimum an ECO governance act needs: a role, a user and a Tenant."""

    def __init__(self, *, user_id, organization_id, role="admin"):
        self.user_id = user_id
        self.organization_id = organization_id
        self.role = role
        self.label = role


def classification_evidence(db, *, effective_from=date(2020, 1, 1)):
    """The platform-governed country mapping. Tenant-independent by design."""
    from app.jurisdiction import record_classification

    record_classification(db, country_code=GERMANY, is_eu=True, is_uk=False,
                          requires_consultation=True,
                          effective_from=effective_from)
    record_classification(db, country_code=UNITED_KINGDOM, is_eu=False,
                          is_uk=True, requires_consultation=False,
                          effective_from=effective_from)
    db.commit()


def jurisdiction_evidence(db, *, organization_id, countries=(GERMANY,),
                          effective_from=date(2020, 1, 1)):
    """Assign the Tenant its jurisdictions. Platform act (ruling B8: C).

    ``countries=()`` records the DECLARED EMPTY set — a Tenant that has stated
    it operates in no mapped jurisdiction, which is a different fact from one
    nobody ever assigned.
    """
    from app.jurisdiction import assign_platform

    assign_platform(db, organization_id=organization_id,
                    country_codes=countries, effective_from=effective_from)
    db.commit()


def compliance_evidence(db, *, organization_id, admin_user_id,
                        effective_from=date(2020, 1, 1),
                        countries=(GERMANY,)):
    """DPIA, consultation attestation and worker notice, all COMPLETED."""
    from app.compliance_evidence import (
        record_consultation_attestation, record_dpia_evidence,
        record_worker_notice,
    )

    actor = _Actor(user_id=admin_user_id, organization_id=organization_id)
    record_dpia_evidence(db, organization_id=organization_id, actor=actor,
                         effective_from=effective_from,
                         eu_substantive_applies=True, eu_uk_gate_applies=True,
                         jurisdiction_scope=countries,
                         external_reference="DPIA-TEST-1")
    record_consultation_attestation(
        db, organization_id=organization_id, actor=actor,
        effective_from=effective_from, jurisdictions=countries,
        external_reference="WC-TEST-1")
    record_worker_notice(db, organization_id=organization_id, actor=actor,
                         effective_from=effective_from,
                         external_reference="NOTICE-TEST-1")
    db.commit()


def make_enablement_ready(db, *, organization_id, admin_user_id,
                          countries=(GERMANY,),
                          effective_from=date(2020, 1, 1)):
    """Everything except the shadow period, which the ROLLOUT owns.

    The shadow observation is deliberately excluded: it is the one gate that
    cannot be satisfied by writing a row, and a helper that supplied it would
    make §44.2's observation period skippable.
    """
    classification_evidence(db, effective_from=effective_from)
    jurisdiction_evidence(db, organization_id=organization_id,
                          countries=countries, effective_from=effective_from)
    operational_evidence(db, organization_id=organization_id,
                         admin_user_id=admin_user_id)
    compliance_evidence(db, organization_id=organization_id,
                        admin_user_id=admin_user_id,
                        effective_from=effective_from, countries=countries)


def shadow_observation(*, days_ago=20, now=None, state="SHADOW",
                       min_days=14.0, entered=True):
    """A ``ShadowObservation`` built directly, without a rollout row."""
    from app.enablement_gates import ShadowObservation

    now = now or datetime.now(UTC)
    return ShadowObservation(
        exists=True, state=state, min_observation_days=min_days,
        shadow_entered_at=(now - timedelta(days=days_ago) if entered
                           else None))


__all__ = [
    "GERMANY",
    "UNITED_KINGDOM",
    "break_treatment_evidence",
    "classification_evidence",
    "compliance_evidence",
    "create_schema",
    "jurisdiction_evidence",
    "make_enablement_ready",
    "netting_evidence",
    "operational_evidence",
    "recipient_evidence",
    "requirement_evidence",
    "shadow_observation",
    "workweek_evidence",
]

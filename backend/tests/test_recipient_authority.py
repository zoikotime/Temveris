"""
RECIPIENT-AUTHORITY — ZT-SPEC-ECO-002 v2.0 §33.1, owner ruling D-3.
====================================================================

Stage 5. Proves the authority resolves, the snapshot freezes, and the freeze is
permanent — and proves, from several directions, that nothing here delivers
anything. §34 leaves channel selection, preferences, transport, retry and
suppression to the central notification service; the lettered classes below
map one-to-one onto the authorized proof obligations A–Z.
"""
from __future__ import annotations

import ast
import pathlib

import pytest
import sqlalchemy as sa

from app.recipient_authority import (
    AUDIENCE_ORDER,
    AudienceState,
    CrossTenantRecipient,
    DESIGNATABLE_AUDIENCES,
    EcoAudience,
    EcoRecipientDesignation,
    RecipientAuthorityUnavailable,
    SNAPSHOT_VERSION,
    build_recipient_snapshot,
    original_recipient_user_ids,
    recipients_from_snapshot,
    resolve_eco_recipients,
)
from app.recipient_authority.outcomes import (
    BASIS_DESIGNATION,
    BASIS_EVENT_SUBJECT,
    BASIS_NOT_DESIGNATED,
    BASIS_NO_CANONICAL_MANAGER,
)

BACKEND = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = BACKEND / "app" / "recipient_authority"
MIGRATION = (BACKEND / "alembic" / "versions"
             / "e7b3a4d91c26_eco_recipient_designation.py")

ORG = 4100
OTHER_ORG = 4200
WORKER = 41001
ADMIN_A = 41002
ADMIN_B = 41003
HR = 41004
OUTSIDER = 42001


@pytest.fixture(scope="module", autouse=True)
def _schema(engine):
    from database.models import Organization, User

    for model in (Organization, User, EcoRecipientDesignation):
        model.__table__.create(engine, checkfirst=True)


@pytest.fixture
def seeded(db):
    """Two tenants, five users. No designations: absence is the default."""
    from database.models import Organization, User

    db.execute(sa.delete(EcoRecipientDesignation))
    for org_id in (ORG, OTHER_ORG):
        if db.get(Organization, org_id) is None:
            db.add(Organization(id=org_id, name=f"org-{org_id}",
                                billing_type="standard"))
    db.flush()
    for uid, org_id in ((WORKER, ORG), (ADMIN_A, ORG), (ADMIN_B, ORG),
                        (HR, ORG), (OUTSIDER, OTHER_ORG)):
        if db.get(User, uid) is None:
            db.add(User(id=uid, name=f"u{uid}", email=f"u{uid}@example.test",
                        organization_id=org_id))
    db.flush()
    return db


def _designate(db, *, audience, user_id, organization_id=ORG):
    db.add(EcoRecipientDesignation(organization_id=organization_id,
                                   audience=audience.value, user_id=user_id))
    db.flush()


def _resolve(db, *, organization_id=ORG, worker_user_id=WORKER):
    return resolve_eco_recipients(db, organization_id=organization_id,
                                  worker_user_id=worker_user_id)


def _package_files():
    return sorted(PACKAGE.glob("*.py"))


# ═════════════════════════════════════════════════════════════════════════════
# A — exactly four locked audiences
# ═════════════════════════════════════════════════════════════════════════════

class TestTheFourAudiences:

    def test_the_vocabulary_is_exactly_the_four_in_the_matrix(self):
        assert [a.value for a in EcoAudience] == [
            "WORKER", "PRIMARY_ADMIN", "DIRECT_MANAGER", "HR_ADMIN"]

    def test_the_order_is_the_matrix_order_and_is_total(self):
        assert AUDIENCE_ORDER == (
            EcoAudience.WORKER, EcoAudience.PRIMARY_ADMIN,
            EcoAudience.DIRECT_MANAGER, EcoAudience.HR_ADMIN)
        assert set(AUDIENCE_ORDER) == set(EcoAudience)

    def test_no_invented_audience_exists(self):
        """§3: no team lead, supervisor, owner, payroll, finance or
        arbitrary recipient. The specification maps none of them."""
        values = {a.value for a in EcoAudience}
        for banned in ("TEAM_LEAD", "SUPERVISOR", "OWNER", "PAYROLL",
                       "FINANCE", "REVIEWER", "ARBITRARY", "EMAIL"):
            assert banned not in values

    def test_every_resolution_answers_all_four(self, seeded):
        """No audience is ever silently dropped — absent ones are RECORDED."""
        resolved = _resolve(seeded)
        assert tuple(e.audience for e in resolved.audiences) == AUDIENCE_ORDER


# ═════════════════════════════════════════════════════════════════════════════
# B / P / Q — the Worker
# ═════════════════════════════════════════════════════════════════════════════

class TestWorker:

    def test_the_worker_resolves_from_the_event_subject(self, seeded):
        entry = _resolve(seeded).by_audience(EcoAudience.WORKER)
        assert entry.state is AudienceState.RESOLVED
        assert entry.user_ids == (WORKER,)
        assert entry.basis == BASIS_EVENT_SUBJECT

    def test_the_worker_identity_is_stable_across_resolutions(self, seeded):
        """P: the same subject resolves to the same identity every time."""
        first = _resolve(seeded).by_audience(EcoAudience.WORKER)
        second = _resolve(seeded).by_audience(EcoAudience.WORKER)
        assert first == second

    def test_a_worker_rename_does_not_change_the_identity(self, seeded):
        """Identity, not display data. Renaming the user moves nothing."""
        from database.models import User

        before = _resolve(seeded).by_audience(EcoAudience.WORKER)
        seeded.get(User, WORKER).name = "Renamed Person"
        seeded.flush()
        assert _resolve(seeded).by_audience(EcoAudience.WORKER) == before

    def test_the_worker_is_never_designatable(self):
        """A WORKER designation row would let a tenant redirect somebody's own
        notification. Refused by the vocabulary, not by convention."""
        assert EcoAudience.WORKER.value not in DESIGNATABLE_AUDIENCES

    def test_a_worker_designation_row_is_refused_by_the_database(self, seeded):
        with pytest.raises(sa.exc.IntegrityError):
            with seeded.begin_nested():
                _designate(seeded, audience=EcoAudience.WORKER,
                           user_id=ADMIN_A)


class TestIdentityNotEmail:
    """Q: authority is identity-based. D-3 forbids inferring it from an
    address, and the snapshot must not be an address carrier either."""

    def test_the_snapshot_contains_no_address(self, seeded):
        _designate(seeded, audience=EcoAudience.PRIMARY_ADMIN,
                   user_id=ADMIN_A)
        payload = str(build_recipient_snapshot(_resolve(seeded)))
        assert "@" not in payload
        for banned in ("email", "address", "smtp", "username"):
            assert banned not in payload.lower()

    def test_the_designation_table_has_no_address_column(self):
        names = {c.name for c in EcoRecipientDesignation.__table__.c}
        assert "user_id" in names
        for banned in ("email", "address", "recipient_email", "name"):
            assert banned not in names

    def test_the_package_never_reads_the_address_resolver(self):
        """§2: ``app.notifications.recipients`` resolves ADDRESSES through a
        free-text settings cascade and is not an admissible ECO authority."""
        for path in _package_files():
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert "notifications" not in node.module, path.name
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        assert "notifications" not in alias.name, path.name


# ═════════════════════════════════════════════════════════════════════════════
# C / D / E / F — the other three audiences
# ═════════════════════════════════════════════════════════════════════════════

class TestPrimaryAdmin:

    def test_a_designated_primary_admin_resolves(self, seeded):
        _designate(seeded, audience=EcoAudience.PRIMARY_ADMIN,
                   user_id=ADMIN_A)
        entry = _resolve(seeded).by_audience(EcoAudience.PRIMARY_ADMIN)
        assert entry.state is AudienceState.RESOLVED
        assert entry.user_ids == (ADMIN_A,)
        assert entry.basis == BASIS_DESIGNATION

    def test_several_primary_admins_all_resolve_in_id_order(self, seeded):
        """§5: never "the first user". All holders, deterministically."""
        _designate(seeded, audience=EcoAudience.PRIMARY_ADMIN,
                   user_id=ADMIN_B)
        _designate(seeded, audience=EcoAudience.PRIMARY_ADMIN,
                   user_id=ADMIN_A)
        entry = _resolve(seeded).by_audience(EcoAudience.PRIMARY_ADMIN)
        assert entry.user_ids == (ADMIN_A, ADMIN_B)

    def test_an_undesignated_tenant_manufactures_nobody(self, seeded):
        entry = _resolve(seeded).by_audience(EcoAudience.PRIMARY_ADMIN)
        assert entry.state is AudienceState.UNRESOLVED
        assert entry.user_ids == ()
        assert entry.basis == BASIS_NOT_DESIGNATED

    def test_the_org_owner_is_not_adopted_as_the_primary_admin(self, seeded):
        """§3/§5: the specification maps no owner/PRIMARY-ADMIN equivalence,
        and ``org_settings.owner_user_id`` is the COMMERCIAL owner besides."""
        from database.models import OrgSettings

        OrgSettings.__table__.create(seeded.get_bind(), checkfirst=True)
        seeded.add(OrgSettings(organization_id=ORG, owner_user_id=ADMIN_A))
        seeded.flush()
        entry = _resolve(seeded).by_audience(EcoAudience.PRIMARY_ADMIN)
        assert entry.state is AudienceState.UNRESOLVED

    def test_the_user_role_string_is_never_read(self, seeded):
        """A job-title edit must not change who is notified."""
        from database.models import User

        seeded.get(User, ADMIN_A).role = "admin"
        seeded.get(User, HR).role = "hr"
        seeded.flush()
        resolved = _resolve(seeded)
        for audience in (EcoAudience.PRIMARY_ADMIN, EcoAudience.HR_ADMIN):
            assert resolved.by_audience(audience).state is \
                AudienceState.UNRESOLVED

        for path in _package_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    assert node.attr != "role", f"{path.name} reads User.role"


class TestDirectManager:

    def test_the_direct_manager_is_unresolved_with_a_named_reason(self,
                                                                  seeded):
        entry = _resolve(seeded).by_audience(EcoAudience.DIRECT_MANAGER)
        assert entry.state is AudienceState.UNRESOLVED
        assert entry.user_ids == ()
        assert entry.basis == BASIS_NO_CANONICAL_MANAGER

    def test_the_repository_really_has_no_reporting_line(self):
        """The finding the basis rests on, asserted rather than assumed.

        If a canonical manager relationship is ever added, this fails and the
        audience must be implemented — the guard is the reminder."""
        from database.database import Base

        for table in Base.metadata.sorted_tables:
            for column in table.c:
                assert column.name not in ("manager_id", "reports_to_id",
                                           "reports_to", "line_manager_id"), \
                    f"{table.name}.{column.name} — implement DIRECT_MANAGER"

    def test_it_never_falls_back_to_the_primary_admin(self, seeded):
        """§6, the substitution the authorization forbids by name."""
        _designate(seeded, audience=EcoAudience.PRIMARY_ADMIN,
                   user_id=ADMIN_A)
        entry = _resolve(seeded).by_audience(EcoAudience.DIRECT_MANAGER)
        assert entry.user_ids == ()

    def test_it_is_never_inferred_from_the_team_head(self, seeded):
        """``teams.head_user_id`` is reachable only through the free-text
        ``User.team`` string — inference from team membership."""
        from database.models import Team, User

        Team.__table__.create(seeded.get_bind(), checkfirst=True)
        seeded.add(Team(organization_id=ORG, name="Ops",
                        head_user_id=ADMIN_B))
        seeded.get(User, WORKER).team = "Ops"
        seeded.flush()
        entry = _resolve(seeded).by_audience(EcoAudience.DIRECT_MANAGER)
        assert entry.user_ids == ()

        for path in _package_files():
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    assert node.attr not in ("head_user_id", "team"), path.name

    def test_it_is_not_designatable_org_wide(self):
        """An org-wide "the direct manager" row is an invented hierarchy."""
        assert EcoAudience.DIRECT_MANAGER.value not in DESIGNATABLE_AUDIENCES

    def test_a_direct_manager_designation_row_is_refused(self, seeded):
        with pytest.raises(sa.exc.IntegrityError):
            with seeded.begin_nested():
                _designate(seeded, audience=EcoAudience.DIRECT_MANAGER,
                           user_id=ADMIN_B)


class TestHrAdmin:

    def test_a_designated_hr_admin_resolves(self, seeded):
        _designate(seeded, audience=EcoAudience.HR_ADMIN, user_id=HR)
        entry = _resolve(seeded).by_audience(EcoAudience.HR_ADMIN)
        assert entry.state is AudienceState.RESOLVED
        assert entry.user_ids == (HR,)
        assert entry.basis == BASIS_DESIGNATION

    def test_an_unconfigured_hr_admin_is_not_manufactured(self, seeded):
        entry = _resolve(seeded).by_audience(EcoAudience.HR_ADMIN)
        assert entry.state is AudienceState.UNRESOLVED
        assert entry.basis == BASIS_NOT_DESIGNATED

    def test_the_org_hr_email_is_not_adopted_as_authority(self, seeded):
        """``OrgSettings.hr_email`` is an ADDRESS. D-3 forbids inferring
        authority from one, and an address names no identity to freeze."""
        from database.models import OrgSettings

        OrgSettings.__table__.create(seeded.get_bind(), checkfirst=True)
        row = seeded.query(OrgSettings).filter_by(organization_id=ORG).first()
        if row is None:
            row = OrgSettings(organization_id=ORG)
            seeded.add(row)
        row.hr_email = "hr@example.test"
        seeded.flush()
        assert _resolve(seeded).by_audience(EcoAudience.HR_ADMIN).state is \
            AudienceState.UNRESOLVED


class TestOptionalAudienceAbsence:
    """F: "If configured" and it is not. A legitimate steady state."""

    def test_absence_refuses_nothing(self, seeded):
        resolved = _resolve(seeded)
        assert resolved.by_audience(EcoAudience.WORKER).state is \
            AudienceState.RESOLVED

    def test_absence_is_recorded_rather_than_omitted(self, seeded):
        snapshot = build_recipient_snapshot(_resolve(seeded))
        named = {e["audience"]: e for e in snapshot["audiences"]}
        assert set(named) == {a.value for a in EcoAudience}
        assert named["HR_ADMIN"]["state"] == "UNRESOLVED"
        assert named["HR_ADMIN"]["basis"] == BASIS_NOT_DESIGNATED

    def test_the_two_kinds_of_absence_are_distinguishable(self, seeded):
        """A tenant that did not configure HR reads differently from a
        capability the platform does not have."""
        snapshot = build_recipient_snapshot(_resolve(seeded))
        named = {e["audience"]: e["basis"] for e in snapshot["audiences"]}
        assert named["HR_ADMIN"] == BASIS_NOT_DESIGNATED
        assert named["DIRECT_MANAGER"] == BASIS_NO_CANONICAL_MANAGER
        assert named["HR_ADMIN"] != named["DIRECT_MANAGER"]


# ═════════════════════════════════════════════════════════════════════════════
# G — cross-tenant rejection
# ═════════════════════════════════════════════════════════════════════════════

class TestCrossTenant:

    def test_a_designation_naming_an_outsider_is_refused(self, seeded):
        _designate(seeded, audience=EcoAudience.PRIMARY_ADMIN,
                   user_id=OUTSIDER)
        with pytest.raises(CrossTenantRecipient):
            _resolve(seeded)

    def test_the_refusal_is_raised_not_resolved_to_unconfigured(self, seeded):
        """A boundary violation must not read as a tenant simply having no
        designation — the two demand completely different responses."""
        _designate(seeded, audience=EcoAudience.HR_ADMIN, user_id=OUTSIDER)
        with pytest.raises(CrossTenantRecipient) as exc:
            _resolve(seeded)
        assert str(OUTSIDER) in str(exc.value)

    def test_a_worker_from_another_tenant_is_refused(self, seeded):
        with pytest.raises(CrossTenantRecipient):
            _resolve(seeded, worker_user_id=OUTSIDER)

    def test_a_designation_is_scoped_to_its_own_tenant(self, seeded):
        """The other tenant's designation must not leak into this one."""
        _designate(seeded, audience=EcoAudience.PRIMARY_ADMIN,
                   user_id=OUTSIDER, organization_id=OTHER_ORG)
        assert _resolve(seeded).by_audience(
            EcoAudience.PRIMARY_ADMIN).state is AudienceState.UNRESOLVED

    def test_a_global_designation_is_unrepresentable(self):
        column = EcoRecipientDesignation.__table__.c.organization_id
        assert column.nullable is False

    def test_unusable_arguments_fail_explicitly(self, seeded):
        for kwargs in ({"organization_id": None}, {"worker_user_id": None},
                       {"worker_user_id": "not-an-id"}):
            with pytest.raises(RecipientAuthorityUnavailable):
                _resolve(seeded, **kwargs)


# ═════════════════════════════════════════════════════════════════════════════
# H / I — determinism and duplicates
# ═════════════════════════════════════════════════════════════════════════════

class TestDeterminism:

    def test_insertion_order_does_not_change_the_answer(self, seeded):
        _designate(seeded, audience=EcoAudience.PRIMARY_ADMIN,
                   user_id=ADMIN_B)
        _designate(seeded, audience=EcoAudience.PRIMARY_ADMIN,
                   user_id=ADMIN_A)
        first = build_recipient_snapshot(_resolve(seeded))

        seeded.execute(sa.delete(EcoRecipientDesignation))
        _designate(seeded, audience=EcoAudience.PRIMARY_ADMIN,
                   user_id=ADMIN_A)
        _designate(seeded, audience=EcoAudience.PRIMARY_ADMIN,
                   user_id=ADMIN_B)
        assert build_recipient_snapshot(_resolve(seeded)) == first

    def test_repeated_resolution_is_byte_identical(self, seeded):
        _designate(seeded, audience=EcoAudience.HR_ADMIN, user_id=HR)
        assert build_recipient_snapshot(_resolve(seeded)) == \
            build_recipient_snapshot(_resolve(seeded))

    def test_the_snapshot_carries_no_timestamp(self, seeded):
        """A clock read here would make two resolutions of one unchanged
        tenant differ, breaking replay and moving the ALERT content hash."""
        payload = str(build_recipient_snapshot(_resolve(seeded))).lower()
        for banned in ("captured_at", "resolved_at", "timestamp", "utc"):
            assert banned not in payload


class TestDuplicateRecipients:
    """I / §14: one person holding two audiences is one person, but the
    audience attribution must survive."""

    def test_one_user_may_hold_two_audiences(self, seeded):
        _designate(seeded, audience=EcoAudience.PRIMARY_ADMIN, user_id=ADMIN_A)
        _designate(seeded, audience=EcoAudience.HR_ADMIN, user_id=ADMIN_A)
        resolved = _resolve(seeded)
        assert resolved.by_audience(EcoAudience.PRIMARY_ADMIN).user_ids == \
            (ADMIN_A,)
        assert resolved.by_audience(EcoAudience.HR_ADMIN).user_ids == \
            (ADMIN_A,)

    def test_the_recipient_list_de_duplicates_across_audiences(self, seeded):
        _designate(seeded, audience=EcoAudience.PRIMARY_ADMIN, user_id=ADMIN_A)
        _designate(seeded, audience=EcoAudience.HR_ADMIN, user_id=ADMIN_A)
        assert _resolve(seeded).resolved_user_ids == (WORKER, ADMIN_A)

    def test_the_worker_is_not_duplicated_when_also_designated(self, seeded):
        _designate(seeded, audience=EcoAudience.PRIMARY_ADMIN, user_id=WORKER)
        resolved = _resolve(seeded)
        assert resolved.resolved_user_ids == (WORKER,)
        # ...and the attribution is still both.
        assert resolved.by_audience(EcoAudience.PRIMARY_ADMIN).user_ids == \
            (WORKER,)

    def test_the_same_user_cannot_hold_one_audience_twice(self, seeded):
        _designate(seeded, audience=EcoAudience.HR_ADMIN, user_id=HR)
        with pytest.raises(sa.exc.IntegrityError):
            with seeded.begin_nested():
                _designate(seeded, audience=EcoAudience.HR_ADMIN, user_id=HR)


# ═════════════════════════════════════════════════════════════════════════════
# J / K / L / M / N / O — the snapshot, and its permanence
# ═════════════════════════════════════════════════════════════════════════════

class TestSnapshotShape:

    def test_it_answers_who_was_authoritative_at_event_time(self, seeded):
        _designate(seeded, audience=EcoAudience.PRIMARY_ADMIN, user_id=ADMIN_A)
        snapshot = build_recipient_snapshot(_resolve(seeded))
        assert snapshot["schema_version"] == SNAPSHOT_VERSION
        assert snapshot["organization_id"] == ORG
        assert snapshot["worker_user_id"] == WORKER
        entry = next(e for e in snapshot["audiences"]
                     if e["audience"] == "PRIMARY_ADMIN")
        assert entry == {"audience": "PRIMARY_ADMIN", "state": "RESOLVED",
                         "user_ids": [ADMIN_A], "basis": BASIS_DESIGNATION}

    def test_the_v1_keys_are_retained(self, seeded):
        """``app.alert.service._resolve_worker`` reads ``worker_user_id`` and
        fails closed without it. A shape change must not break a v1 reader."""
        snapshot = build_recipient_snapshot(_resolve(seeded))
        assert snapshot["worker_user_id"] == WORKER
        assert snapshot["roles"] == ["WORKER"]

    def test_it_is_json_safe(self, seeded):
        import json

        _designate(seeded, audience=EcoAudience.HR_ADMIN, user_id=HR)
        snapshot = build_recipient_snapshot(_resolve(seeded))
        assert json.loads(json.dumps(snapshot)) == snapshot

    def test_there_is_no_second_competing_snapshot_field(self):
        """§9: the existing event JSON stays the authoritative record."""
        from app.eval_event import EcoThresholdEvent

        names = {c.name for c in EcoThresholdEvent.__table__.c}
        assert "recipient_snapshot" in names
        assert not [n for n in names
                    if "recipient" in n and n not in
                    ("recipient_snapshot", "worker_recipient")]


class TestPermanence:
    """D-3.7 — the snapshot is authoritative permanently."""

    @pytest.fixture
    def frozen(self, seeded):
        _designate(seeded, audience=EcoAudience.PRIMARY_ADMIN, user_id=ADMIN_A)
        _designate(seeded, audience=EcoAudience.HR_ADMIN, user_id=HR)
        return build_recipient_snapshot(_resolve(seeded))

    def test_a_active_recipient_is_included(self, seeded, frozen):
        """A."""
        named = {e["audience"]: e["user_ids"] for e in frozen["audiences"]}
        assert named["PRIMARY_ADMIN"] == [ADMIN_A]

    def test_b_deactivation_does_not_rewrite_the_snapshot(self, seeded,
                                                          frozen):
        """B."""
        from database.models import User

        before = dict(frozen)
        seeded.get(User, ADMIN_A).is_active = False
        seeded.flush()
        assert frozen == before
        assert recipients_from_snapshot(frozen)[1]["user_ids"] == [ADMIN_A]

    def test_c_deletion_does_not_rewrite_the_snapshot(self, seeded, frozen):
        """C. The designation must be removed first — the FK is RESTRICT —
        and neither step touches the frozen structure."""
        from database.models import User

        before = dict(frozen)
        seeded.execute(sa.delete(EcoRecipientDesignation).where(
            EcoRecipientDesignation.user_id == HR))
        seeded.execute(sa.delete(User).where(User.id == HR))
        seeded.flush()
        assert seeded.get(User, HR) is None
        assert frozen == before
        assert original_recipient_user_ids(frozen) == (WORKER, ADMIN_A, HR)

    def test_d_a_designation_change_does_not_rewrite_the_snapshot(
            self, seeded, frozen):
        """D."""
        before = dict(frozen)
        seeded.execute(sa.delete(EcoRecipientDesignation))
        _designate(seeded, audience=EcoAudience.PRIMARY_ADMIN, user_id=ADMIN_B)
        assert frozen == before

    def test_e_a_new_event_resolves_the_new_authority(self, seeded, frozen):
        """E. History is frozen; the NEXT event is not."""
        seeded.execute(sa.delete(EcoRecipientDesignation))
        _designate(seeded, audience=EcoAudience.PRIMARY_ADMIN, user_id=ADMIN_B)
        later = build_recipient_snapshot(_resolve(seeded))
        assert later != frozen
        named = {e["audience"]: e["user_ids"] for e in later["audiences"]}
        assert named["PRIMARY_ADMIN"] == [ADMIN_B]

    def test_g_replay_produces_the_same_snapshot(self, seeded, frozen):
        """G."""
        assert build_recipient_snapshot(_resolve(seeded)) == frozen

    def test_h_a_historical_read_cannot_consult_current_authority(self):
        """H, enforced by the SIGNATURE rather than by documentation: the
        historical readers take no Session, so they cannot re-resolve."""
        import inspect

        for fn in (recipients_from_snapshot, original_recipient_user_ids):
            params = list(inspect.signature(fn).parameters)
            assert params == ["snapshot"], fn.__name__

    def test_a_historical_read_survives_the_authority_being_emptied(
            self, seeded, frozen):
        seeded.execute(sa.delete(EcoRecipientDesignation))
        seeded.flush()
        assert original_recipient_user_ids(frozen) == (WORKER, ADMIN_A, HR)

    def test_a_v1_snapshot_is_projected_not_migrated(self):
        """A pre-Stage-5 snapshot still reads, and rewriting one in place is
        exactly what D-3.7 forbids."""
        v1 = {"worker_user_id": 77, "roles": ["WORKER"]}
        entries = recipients_from_snapshot(v1)
        assert len(entries) == 1
        assert entries[0]["audience"] == "WORKER"
        assert entries[0]["user_ids"] == [77]
        assert v1 == {"worker_user_id": 77, "roles": ["WORKER"]}

    def test_an_empty_snapshot_yields_nothing_rather_than_guessing(self):
        assert recipients_from_snapshot({}) == ()
        assert original_recipient_user_ids(None) == ()


# ═════════════════════════════════════════════════════════════════════════════
# R / S — the two vectors this stage serves
# ═════════════════════════════════════════════════════════════════════════════

class TestGV07RecipientPrerequisite:
    """GV-07: the withdrawal goes to "all original recipients" (§33.1).

    Delivery is NOT built here. What is proved is that the original event-time
    snapshot is persisted and that "all original recipients" is answerable from
    it without re-resolving today's designations.
    """

    def test_the_withdrawal_copies_the_original_snapshot_verbatim(self):
        """The copy is EVAL-EVENT's, and it is a copy, not a re-resolution."""
        source = (BACKEND / "app" / "eval_event" / "service.py").read_text(
            encoding="utf-8")
        assert "recipient_snapshot=dict(\n" \
               "                        open_crossing.recipient_snapshot" \
               " or {})" in source
        assert source.count("resolve_eco_recipients(") == 1

    def test_all_original_recipients_is_answerable_from_the_snapshot(self,
                                                                     seeded):
        _designate(seeded, audience=EcoAudience.PRIMARY_ADMIN, user_id=ADMIN_A)
        _designate(seeded, audience=EcoAudience.HR_ADMIN, user_id=HR)
        original = build_recipient_snapshot(_resolve(seeded))

        # Everything about today's authority changes...
        seeded.execute(sa.delete(EcoRecipientDesignation))
        _designate(seeded, audience=EcoAudience.PRIMARY_ADMIN, user_id=ADMIN_B)
        seeded.flush()

        # ...and the withdrawal's recipients are still the ORIGINAL ones.
        assert original_recipient_user_ids(original) == (WORKER, ADMIN_A, HR)

    def test_the_audience_attribution_survives_into_the_withdrawal(self,
                                                                   seeded):
        _designate(seeded, audience=EcoAudience.HR_ADMIN, user_id=HR)
        original = build_recipient_snapshot(_resolve(seeded))
        carried = dict(original)  # what EVAL-EVENT copies onto the withdrawal
        named = {e["audience"]: e["user_ids"]
                 for e in recipients_from_snapshot(carried)}
        assert named["HR_ADMIN"] == [HR]


class TestGV18AdministratorAuthority:
    """GV-18: requirement unresolved -> administrator alert, worker NOT shown.

    Only the authoritative administrator RECIPIENT side is established here.
    The alert itself, and its delivery, are not built.
    """

    def test_the_administrator_audience_is_resolvable(self, seeded):
        _designate(seeded, audience=EcoAudience.PRIMARY_ADMIN, user_id=ADMIN_A)
        entry = _resolve(seeded).by_audience(EcoAudience.PRIMARY_ADMIN)
        assert entry.state is AudienceState.RESOLVED
        assert entry.user_ids == (ADMIN_A,)

    def test_the_matrix_row_is_administrator_only(self):
        """§33.1 "Requirement unresolved": WORKER Never, PRIMARY ADMIN Always
        (operational), DIRECT MANAGER No, HR ADMIN No. Recorded here because
        Stage 5 resolves the authority the row needs; the row itself is not
        implemented, and no code consults this."""
        expected = {"WORKER": "NEVER", "PRIMARY_ADMIN": "ALWAYS",
                    "DIRECT_MANAGER": "NO", "HR_ADMIN": "NO"}
        assert set(expected) == {a.value for a in EcoAudience}

    def test_no_alert_row_is_created_by_resolving(self, seeded):
        from app.alert import EcoNotification

        EcoNotification.__table__.create(seeded.get_bind(), checkfirst=True)
        _designate(seeded, audience=EcoAudience.PRIMARY_ADMIN, user_id=ADMIN_A)
        _resolve(seeded)
        assert seeded.query(EcoNotification).count() == 0


# ═════════════════════════════════════════════════════════════════════════════
# T / U / V — nothing is delivered
# ═════════════════════════════════════════════════════════════════════════════

class TestNoDelivery:

    def test_no_transport_module_is_imported(self):
        """§34: channel selection, preferences, transport, retry and
        suppression belong to the central notification service."""
        banned = ("smtplib", "email", "app.email_service", "aiosmtplib",
                  "sendgrid", "app.notifications", "requests", "httpx")
        for path in _package_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    for bad in banned:
                        assert not name.startswith(bad), \
                            f"{path.name} imports {name}"

    def test_no_delivery_vocabulary_appears(self):
        for path in _package_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            identifiers = {n.id for n in ast.walk(tree)
                           if isinstance(n, ast.Name)}
            identifiers |= {n.attr for n in ast.walk(tree)
                            if isinstance(n, ast.Attribute)}
            identifiers |= {n.name for n in ast.walk(tree)
                            if isinstance(n, (ast.FunctionDef,
                                              ast.AsyncFunctionDef,
                                              ast.ClassDef))}
            for banned in ("send", "deliver", "dispatch", "transport",
                           "channel", "retry", "suppress", "DeliveryAttempt",
                           "send_email", "notify"):
                assert banned not in identifiers, f"{path.name} has {banned}"

    def test_no_delivery_attempt_is_created(self, seeded):
        from app.alert import EcoNotificationDeliveryAttempt

        EcoNotificationDeliveryAttempt.__table__.create(seeded.get_bind(),
                                                        checkfirst=True)
        _designate(seeded, audience=EcoAudience.PRIMARY_ADMIN, user_id=ADMIN_A)
        _resolve(seeded)
        build_recipient_snapshot(_resolve(seeded))
        assert seeded.query(EcoNotificationDeliveryAttempt).count() == 0

    def test_no_email_is_sent(self, seeded, monkeypatch):
        """Nothing in the resolution path may reach the mailer."""
        import app.email_service as mailer

        calls = []
        for name in [n for n in dir(mailer) if n.startswith("send")]:
            if callable(getattr(mailer, name)):
                monkeypatch.setattr(mailer, name,
                                    lambda *a, _n=name, **k: calls.append(_n))
        _designate(seeded, audience=EcoAudience.HR_ADMIN, user_id=HR)
        build_recipient_snapshot(_resolve(seeded))
        assert calls == []

    def test_alert_still_authorizes_only_the_worker_audience(self):
        """Stage 5 built the AUTHORITY, not the permission to deliver to it.
        Widening this set is Stage 6 and needs its own authorization."""
        from app.alert.outcomes import AUTHORIZED_AUDIENCES

        assert AUTHORIZED_AUDIENCES == frozenset({"WORKER"})

    def test_no_http_surface_exists(self):
        for path in _package_files():
            source = path.read_text(encoding="utf-8")
            for banned in ("APIRouter", "@router.", "fastapi"):
                assert banned not in source, path.name


# ═════════════════════════════════════════════════════════════════════════════
# W / X / Y / Z — the earlier stages still hold
# ═════════════════════════════════════════════════════════════════════════════

class TestEarlierStagesHold:
    """Executed by their own suites; asserted here as PROMOTION state, which
    is the thing Stage 5 could plausibly disturb."""

    @staticmethod
    def _by_id():
        import json

        return {v["id"]: v for v in json.loads(
            (BACKEND / "tests" / "golden" / "eco_golden_vectors.json")
            .read_text(encoding="utf-8"))["vectors"]}

    @pytest.mark.parametrize("gid", ["GV-01", "GV-19", "GV-20", "GV-23"])
    def test_the_promoted_vectors_stay_promoted(self, gid):
        harness = self._by_id()[gid]["harness"]
        assert harness["execution"] == "executable", gid
        assert harness["blocking_dependencies"] == [], gid


# ═════════════════════════════════════════════════════════════════════════════
# Scope held
# ═════════════════════════════════════════════════════════════════════════════

class TestScopeHeld:

    def test_the_migration_is_purely_additive(self):
        tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
        upgrade = next(n for n in tree.body
                       if isinstance(n, ast.FunctionDef)
                       and n.name == "upgrade")
        called = {n.func.attr for n in ast.walk(upgrade)
                  if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Attribute)}
        for banned in ("drop_table", "drop_column", "alter_column",
                       "rename_table", "execute", "bulk_insert"):
            assert banned not in called

    def test_the_migration_and_the_model_agree_on_the_vocabulary(self):
        constraint = next(
            c for c in EcoRecipientDesignation.__table__.constraints
            if getattr(c, "name", "")
            == "ck_eco_recipient_designation_audience")
        rendered = str(constraint.sqltext)
        for audience in DESIGNATABLE_AUDIENCES:
            assert f"'{audience}'" in rendered
        for excluded in ("WORKER", "DIRECT_MANAGER"):
            assert f"'{excluded}'" not in rendered

        # The migration's own list, which is a separate transcription and can
        # therefore drift, must render to the same set.
        tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
        literal = next(
            ast.literal_eval(n.value) for n in tree.body
            if isinstance(n, ast.Assign)
            and getattr(n.targets[0], "id", "") == "_DESIGNATABLE")
        assert set(literal) == set(DESIGNATABLE_AUDIENCES)

    def test_the_table_ships_empty(self, seeded):
        seeded.execute(sa.delete(EcoRecipientDesignation))
        seeded.flush()
        for path in (BACKEND / "app").rglob("*.py"):
            if path.is_relative_to(PACKAGE):
                continue
            source = path.read_text(encoding="utf-8")
            assert "EcoRecipientDesignation(" not in source, path.name

    def test_no_effective_dating_was_introduced(self):
        """D-3.6: a second time axis would be a second way to answer "who held
        this on that date", and the snapshot already answers it."""
        names = {c.name for c in EcoRecipientDesignation.__table__.c}
        for banned in ("effective_from", "effective_to", "valid_from",
                       "valid_to", "revoked_at"):
            assert banned not in names

    def test_both_foreign_keys_are_restrict(self):
        for column in ("organization_id", "user_id"):
            fks = list(EcoRecipientDesignation.__table__.c[column].
                       foreign_keys)
            assert len(fks) == 1
            assert fks[0].ondelete == "RESTRICT", column

    def test_eco_v1_and_the_rollout_are_untouched(self):
        router = (BACKEND / "app" / "employees" / "router.py").read_text(
            encoding="utf-8")
        assert "EARLY_CLOCK_OUT_FEATURE_ENABLED = False" in router
        for path in _package_files():
            source = path.read_text(encoding="utf-8")
            assert "RolloutControl(" not in source

    def test_no_incident_work_leaked_in(self):
        """§21: eco_ledger_incident and GV-26 are Stage 7."""
        for path in _package_files():
            source = path.read_text(encoding="utf-8")
            for banned in ("eco_ledger_incident", "LedgerIncident",
                           "GV-26"):
                assert banned not in source, path.name

    def test_gv18_is_not_promoted_and_gv07_crossed_elsewhere(self):
        """§15/§18: recipient authority existing was not a promotion."""
        import json

        by_id = {v["id"]: v for v in json.loads(
            (BACKEND / "tests" / "golden" / "eco_golden_vectors.json")
            .read_text(encoding="utf-8"))["vectors"]}
        # GV-07 has since been promoted, but NOT by this package: recipient
        # authority made "all original recipients" ANSWERABLE, and the vector
        # crossed only once ALERT-DELIVERY made the withdrawal REACH them.
        # Keeping that distinction visible is why this guard exists.
        assert by_id["GV-07"]["harness"]["execution"] == "executable"
        assert by_id["GV-07"]["harness"]["blocking_dependencies"] == []
        # GV-18 has since crossed too, and — like GV-07 — NOT by this package.
        # Stage 5 made the administrator RESOLVABLE; the vector crossed only
        # once ruling 2: B's day-scoped primitive could carry the §24.3 alert
        # for a day that emits no threshold event.
        assert by_id["GV-18"]["harness"]["execution"] == "executable"
        assert by_id["GV-18"]["harness"]["blocking_dependencies"] == []

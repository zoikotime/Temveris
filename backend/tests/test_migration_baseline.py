"""Migration-baseline guards (P2.5).

The 2026-08 audit found the migration history untrustworthy: ``alembic_version``
was stamped ``e7ebc3f1fdb5`` while the schema had drifted past it, and
``a1b2c3d4e5f7`` would have failed with DuplicateTable on ``alembic upgrade``.
These tests lock in the two structural properties that make the history
reviewable again, so the drift cannot silently reappear.

The backfill migration itself (``c4a7e2b915d3``) uses PostgreSQL-only SQL
(``split_part``, ``~``), so it cannot be exercised against the SQLite test DB
the way ``test_migration_commercial_tables.py`` exercises its DDL migration. It
was instead rehearsed against production inside a rolled-back transaction:
6 rows updated, second run 0 rows (idempotent), 0 dangling org refs. What is
asserted here is that its guards are present in the SQL — the properties that
make it safe to run unattended.
"""
import pathlib
import re

from alembic.config import Config
from alembic.script import ScriptDirectory

_BACKEND = pathlib.Path(__file__).resolve().parent.parent


def _script_dir() -> ScriptDirectory:
    cfg = Config(str(_BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND / "alembic"))
    return ScriptDirectory.from_config(cfg)


def test_single_linear_head():
    """More than one head means two branches were merged carelessly — the
    condition that produced the drift this phase repairs."""
    heads = _script_dir().get_heads()
    assert len(heads) == 1, f"expected exactly one head, found {heads}"


def test_backfill_chains_off_the_offline_intervals_revision():
    """The backfill must sit directly after the last pre-existing revision, so the
    stamp-then-upgrade plan reaches it without skipping anything."""
    sd = _script_dir()
    rev = sd.get_revision("c4a7e2b915d3")
    assert rev.down_revision == "d5e8a1c34b70"


def test_every_revision_is_reachable_from_head():
    """No orphaned revision files: walking down from head must visit them all."""
    sd = _script_dir()
    walked = {r.revision for r in sd.walk_revisions()}
    on_disk = {
        m.group(1)
        for p in (_BACKEND / "alembic" / "versions").glob("*.py")
        for m in [re.search(r'^revision(?::\s*str)?\s*=\s*["\']([^"\']+)["\']',
                            p.read_text(encoding="utf-8"), re.M)]
        if m
    }
    assert on_disk - walked == set(), f"orphaned revisions: {sorted(on_disk - walked)}"


def test_backfill_sql_is_guarded():
    """The three guards that make the backfill safe to run unattended:
    idempotency, no invented tenant, no uniqueness collision."""
    src = (_BACKEND / "alembic" / "versions"
           / "c4a7e2b915d3_backfill_null_org_app_settings.py").read_text(encoding="utf-8")
    upgrade = src.split("def upgrade()")[1].split("def downgrade()")[0]
    assert "organization_id IS NULL" in upgrade, "missing idempotency guard"
    assert "FROM organizations" in upgrade, "missing org-exists guard"
    assert "NOT EXISTS" in upgrade, "missing uniqueness-collision guard"
    assert "UPDATE app_settings" in upgrade
    # Scoped to the two keys the defective provisioning path wrote (via
    # _KEY_PATTERN) — never a blanket "any NULL organization_id" sweep.
    assert "_KEY_PATTERN" in upgrade, "upgrade must constrain the key pattern"
    pattern = re.search(r'_KEY_PATTERN\s*=\s*r?"([^"]+)"', src).group(1)
    assert "shared_pool_enabled" in pattern
    assert "leave_waiting_period_months" in pattern
    # Data-only: this migration must never carry DDL.
    for ddl in ("op.create_table", "op.add_column", "op.drop_column",
                "op.create_index", "op.alter_column", "ALTER TABLE"):
        assert ddl not in src, f"backfill migration must be data-only, found {ddl}"


def test_d11_chains_off_the_backfill():
    """Chain position only. Which revision is HEAD is asserted once, by
    test_single_linear_head — coupling every revision test to it means each new
    migration breaks the previous one's test."""
    sd = _script_dir()
    assert sd.get_revision("e8b3c6d1f472").down_revision == "c4a7e2b915d3"


def test_d11_uses_restrict_not_cascade_or_set_null():
    """Organizations are soft-lifecycle entities: an accidental hard delete must
    fail loudly, never cascade-destroy tenant config nor dump rows into the
    shared NULL-organization bucket."""
    src = (_BACKEND / "alembic" / "versions"
           / "e8b3c6d1f472_org_fk_restrict_settings_schedule.py").read_text(encoding="utf-8")
    up = src.split("def upgrade()")[1].split("def downgrade()")[0]
    assert "ON DELETE RESTRICT" in up
    assert "ON DELETE CASCADE" not in up
    assert "ON DELETE SET NULL" not in up
    # Short-lock pattern + catalog guard + explicit pre-flight.
    assert "NOT VALID" in up and "VALIDATE CONSTRAINT" in up
    assert "FROM pg_constraint" in up, "missing idempotency guard"
    assert "RuntimeError" in up, "missing dangling-row pre-flight"
    for t in ("app_settings", "shifts", "shift_assignments", "holidays"):
        assert t in src
    assert "teams" not in up, "teams already has an FK and is out of scope"


def test_models_declare_restrict_for_the_four_tables():
    """The models must not contradict the migration, or autogenerate will try to
    revert it and create_all will emit the wrong rule on fresh databases."""
    from database import models
    for m in (models.AppSettings, models.Shift, models.ShiftAssignment, models.Holiday):
        fk = list(m.__table__.c.organization_id.foreign_keys)[0]
        assert fk.ondelete == "RESTRICT", f"{m.__tablename__} declares {fk.ondelete}"
    # teams keeps its existing SET NULL — deliberately unchanged.
    fk = list(models.Team.__table__.c.organization_id.foreign_keys)[0]
    assert fk.ondelete == "SET NULL"


# ── P3: schedule schema ──────────────────────────────────────────────────────

def _p3_src() -> str:
    return (_BACKEND / "alembic" / "versions"
            / "f1a9d4c72e86_schedule_schema_shift_policy_fields.py").read_text(encoding="utf-8")


def test_p3_chains_off_d11():
    assert _script_dir().get_revision("f1a9d4c72e86").down_revision == "e8b3c6d1f472"


def test_p3_is_schema_only():
    """No data change, no backfill (that is P6), no business logic."""
    up = _p3_src().split("def upgrade()")[1].split("def downgrade()")[0]
    for forbidden in ("UPDATE ", "INSERT ", "DELETE ", "SELECT "):
        assert forbidden not in up.upper().replace("IF NOT EXISTS", ""), \
            f"P3 must be schema-only, found {forbidden.strip()}"


def test_p3_is_idempotent_and_additive():
    up = _p3_src().split("def upgrade()")[1].split("def downgrade()")[0]
    assert up.count("ADD COLUMN IF NOT EXISTS") == 7
    assert "CREATE INDEX IF NOT EXISTS" in up
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" in up
    # Nothing may be dropped or retyped on the way up, except the unique
    # constraint that the expression index replaces.
    assert "DROP COLUMN" not in up
    assert "ALTER COLUMN" not in up
    assert up.count("DROP CONSTRAINT") == 1


def test_p3_downgrade_reverses_everything():
    src = _p3_src()
    down = src.split("def downgrade()")[1]
    for col in ("config", "is_default", "crosses_midnight", "expected_work_seconds",
                "is_off_day", "calendar_key"):
        assert col in down, f"downgrade does not drop {col}"
    # The original constraint name is held in a module constant and interpolated.
    assert re.search(r'_HOLIDAY_UNIQUE_CONSTRAINT\s*=\s*"uq_holidays_org_date"', src)
    assert "_HOLIDAY_UNIQUE_CONSTRAINT" in down, \
        "downgrade must restore the original UNIQUE constraint"
    assert "ADD CONSTRAINT" in down


def test_p3_omits_columns_with_no_consumer():
    """Architecture 4.2 also listed per-shift threshold columns and a shift-level
    timezone. They are intentionally absent: the resolver has no precedence hook
    for them, `config` is the approved mechanism for per-shift rules, and a shift
    timezone would be a second source of truth against
    OrgSettings.default_timezone."""
    src = _p3_src()
    for omitted in ("grace_minutes", "late_after_minutes", "very_late_after_minutes",
                    "min_hours_required_seconds",
                    "allow_early_clock_in", "allow_late_clock_out"):
        assert f"ADD COLUMN IF NOT EXISTS {omitted}" not in src
    assert "ADD COLUMN IF NOT EXISTS timezone" not in src


def test_models_match_the_p3_migration():
    """Every column the migration adds must exist on the model, or create_all and
    autogenerate will disagree with a migrated database."""
    from database import models
    for col in ("expected_work_seconds", "crosses_midnight", "is_default", "config"):
        assert col in models.Shift.__table__.c, f"Shift.{col} missing"
    for col in ("expected_work_seconds", "is_off_day"):
        assert col in models.ShiftAssignment.__table__.c, f"ShiftAssignment.{col} missing"
    assert "calendar_key" in models.Holiday.__table__.c
    # Flags must be non-null with a server default so existing rows are valid.
    for m, col in ((models.Shift, "crosses_midnight"), (models.Shift, "is_default"),
                   (models.ShiftAssignment, "is_off_day")):
        c = m.__table__.c[col]
        assert c.nullable is False and c.server_default is not None, col
    # Policy columns must be nullable — NULL means "fall back to the cascade".
    assert models.Shift.__table__.c["expected_work_seconds"].nullable is True
    assert models.ShiftAssignment.__table__.c["expected_work_seconds"].nullable is True


def test_holiday_uniqueness_now_includes_calendar_key():
    """Uniqueness is (org, calendar_key, country_code, date, name).

    `calendar_key` arrived with the schedule migration (f1a9d4c72e86);
    `country_code` and `name` with the country-scoped calendars migration
    (m7n2p5q8r1t4). Every addition WIDENS the key, so a row that satisfied an
    earlier version still satisfies this one — which is what makes both
    migrations safe on a populated database.
    """
    from database import models
    uniques = [c for c in models.Holiday.__table__.constraints
               if c.__class__.__name__ == "UniqueConstraint"]
    assert len(uniques) == 1
    assert {c.name for c in uniques[0].columns} == {
        "organization_id", "calendar_key", "country_code", "date", "name"}


def test_holiday_country_code_is_nullable_iso2():
    """NULL country_code means "unscoped", which is what every pre-migration row
    keeps. A NOT NULL column here would have forced the migration to guess a
    country for existing holidays — the exact mistake that left two US
    organizations holding an Indian calendar."""
    from database import models
    col = models.Holiday.__table__.c["country_code"]
    assert col.nullable is True
    assert col.type.length == 2

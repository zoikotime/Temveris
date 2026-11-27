"""Work Schedule Policy setting keys (P4 — configuration model only).

P4 registers the schedule settings and nothing else: no resolver, no attendance
change, no UI, no migration. These tests lock in two things.

1. **The keys are correctly registered** — present in the settings router's
   DEFAULTS with a description, string-typed, uniquely namespaced, and carrying
   the approved factory defaults.
2. **The registration is inert** — in particular that no schedule key has leaked
   into ``settings.scope.KEY_META``, because that is what makes a setting render
   in the Organization Settings admin UI. Three of these keys need control types
   the UI does not implement until P8, so rendering them early would show
   half-working inputs to admins.
"""
from __future__ import annotations

import json

import pytest

from app.settings.router import DEFAULTS, DESCRIPTIONS
from app.settings import scope as settings_scope
from app import schedule_policy as sp
from database.database import Base


def _code_only(src: str) -> str:
    """Source with comments and docstrings removed.

    The call-site guards below scan for key names. A comment or docstring that
    merely NAMES a key is documentation, not a consumer, so stripping both keeps
    the guard about behaviour. Uses tokenize rather than regex so it cannot be
    fooled by formatting.
    """
    import io
    import tokenize
    out, prev_end, ignore_string = [], (1, 0), True
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            ttype, tstr, start, end, _ = tok
            if ttype == tokenize.COMMENT:
                continue
            if ttype == tokenize.STRING and ignore_string:
                continue          # bare string expression == docstring
            if ttype in (tokenize.NEWLINE, tokenize.NL, tokenize.INDENT,
                         tokenize.DEDENT):
                ignore_string = True
            elif ttype not in (tokenize.STRING,):
                ignore_string = False
            out.append(tstr)
    except Exception:
        return src
    return "\n".join(out)


@pytest.fixture(scope="session", autouse=True)
def _all_tables(engine):
    """conftest creates only a subset of tables; scoped_view() reads app_settings."""
    Base.metadata.create_all(engine)
    yield


# ── Registration ─────────────────────────────────────────────────────────────

def test_every_schedule_key_is_registered_with_a_description():
    for key in sp.SETTING_DEFAULTS:
        assert key in DEFAULTS, f"{key} not registered in settings DEFAULTS"
        assert DEFAULTS[key] == sp.SETTING_DEFAULTS[key]
        assert DESCRIPTIONS.get(key), f"{key} has no description"


def test_all_keys_are_namespaced_and_collision_free():
    """The `schedule.` prefix is what guarantees no clash with an existing key."""
    others = {k for k in DEFAULTS if not k.startswith("schedule.")}
    for key in sp.SETTING_DEFAULTS:
        assert key.startswith("schedule."), key
        assert key not in others


def test_defaults_are_strings():
    """app_settings.value is Text and the whole cascade is string-typed."""
    for key, value in sp.SETTING_DEFAULTS.items():
        assert isinstance(value, str), f"{key} default is {type(value).__name__}"


# ── Approved default values ──────────────────────────────────────────────────

def test_approved_system_defaults():
    d = sp.SETTING_DEFAULTS
    assert d[sp.SETTING_EXPECTED_WORK_HOURS] == "8:00"
    assert d[sp.SETTING_EMPLOYMENT_TYPE] == "full_time"
    assert json.loads(d[sp.SETTING_WORKING_DAYS]) == [
        "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY",
    ]
    assert d[sp.SETTING_GRACE_MINUTES] == "15"
    # Empty = no scheduled shift. This is what makes the rollout
    # behaviour-neutral: without a start time nobody can be marked Late,
    # matching today's treatment of users with no default_clock_in_time.
    assert d[sp.SETTING_SHIFT_START_TIME] == ""
    # Empty for the same reason as the start: an implicit 18:00 made every
    # employee of an unconfigured org accrue "left early" time against a
    # shift nobody had set.
    assert d[sp.SETTING_SHIFT_END_TIME] == ""
    assert d[sp.SETTING_VERY_LATE_AFTER_MIN] == "120"
    assert d[sp.SETTING_MIN_HOURS_REQUIRED] == "4:00"
    assert d[sp.SETTING_ALLOW_EARLY_CLOCK_IN] == "true"
    assert d[sp.SETTING_ALLOW_LATE_CLOCK_OUT] == "true"
    assert d[sp.SETTING_ABSENT_AFTER_HOUR] == "12"
    assert d[sp.SETTING_WORK_MODE] == "onsite"


def test_the_two_expected_hours_defaults_agree():
    """There is ONE system default for the length of a working day.

    attendance_policy owns the legacy employment-type path and schedule_policy
    owns the resolver. While they disagreed, GET /settings advertised 8:45 as
    the system default and an unconfigured organization was actually held to
    8:00 — the resolver reaches the employment-type policy before it reaches
    this key.
    """
    from app.attendance_policy import (
        DEFAULT_EXPECTED_WORK_SECONDS, parse_hhmm_to_seconds,
    )
    assert parse_hhmm_to_seconds(
        sp.SETTING_DEFAULTS[sp.SETTING_EXPECTED_WORK_HOURS]
    ) == DEFAULT_EXPECTED_WORK_SECONDS


def test_engine_is_off_by_default():
    """The rollout switch must ship OFF, so P5+ consumers stay dormant."""
    assert sp.SETTING_DEFAULTS[sp.SETTING_ENGINE_ENABLED] == "false"


def test_optional_thresholds_default_to_empty_not_zero():
    """`late_after` empty means 'fall back to grace'; 0 would mean 'late
    immediately'. Same for shift_id: empty means no shift assigned."""
    assert sp.SETTING_DEFAULTS[sp.SETTING_LATE_AFTER_MINUTES] == ""
    assert sp.SETTING_DEFAULTS[sp.SETTING_SHIFT_ID] == ""
    assert sp.SETTING_DEFAULTS[sp.SETTING_SHIFT_START_TIME] == ""


def test_expected_hours_default_is_selectable():
    """8:45 must be offered by the control, or the default is not reachable."""
    assert "8:45" in sp.DURATION_OPTIONS
    assert sp.SETTING_DEFAULTS[sp.SETTING_MIN_HOURS_REQUIRED] in sp.DURATION_OPTIONS


def test_part_time_target_is_selectable():
    """An employee-scope part-time override needs 4:00 available; the existing
    work_hours_threshold list starts at 6:00."""
    assert "4:00" in sp.DURATION_OPTIONS


def test_employment_types_cover_the_approved_four():
    assert sp.EMPLOYMENT_TYPES == ["full_time", "part_time", "contract", "intern"]


def test_working_days_vocabulary_matches_attendance_policy():
    """Weekday names must agree with the existing convention or the two
    subsystems will disagree about what Saturday is called."""
    from app.attendance_policy.resolvers import _WEEKDAY_NAMES
    assert sp.WEEKDAY_NAMES == _WEEKDAY_NAMES


# ── Inertness: nothing reads or renders these yet ────────────────────────────

# NOTE: the two P4 guards that asserted these keys were NOT rendered are
# superseded by the P8 section at the end of this file, which asserts the
# curated rows DO render while the internal switches still do not.


def test_only_approved_call_sites_read_the_schedule_keys():
    """A SCOPE guard: the schedule keys may only be read from approved call
    sites, so a module cannot start reading raw settings instead of asking the
    resolver. That is the failure mode this whole refactor removed — five files
    each reading their own threshold.

    Adding a name here should be deliberate, and should usually be the wrong
    answer: the right one is to call resolve_work_schedule().
    """
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    # Anything inside the package may use its own keys (P5 added the resolver and
    # classifier there).
    # P10 added the write-path consumers, each gated on schedule.engine_enabled.
    approved = {"settings/router.py", "attendance_policy/resolvers.py",
                "employees/router.py", "attendance_alerts.py",
                "risk/router.py", "intelligence/risk_engine.py",
                "intelligence/burnout.py", "reports/router.py",
                "payroll/policy.py",
                # The employee Profile page REPORTS the resolved schedule
                # instead of storing a self-service copy of it.
                "profile/router.py",
                # Leave review asks the resolver whether a day is a working
                # day, instead of testing weekday() == 0 or 4.
                "leave/policy.py",
                # Weekly pay periods start on the org's own first day of
                # the week, not a hard-coded Monday.
                "payroll/router.py",
                # /schedule/my-schedule returns the caller's working days so
                # the calendar grid stops assuming a Sat/Sun weekend.
                "schedule/router.py",
                # The sealed work-day ledger derives its canonical workweek_key
                # from the organization's own first day of the week — the same
                # reason payroll/router.py is on this list. It calls
                # week_start_date(), the resolver, and reads no raw setting;
                # the ISO-week arithmetic it replaced was wrong for every
                # non-Monday tenant. Narrowest possible entry: the one module,
                # not the package.
                "work_ledger/workweek.py",
                # ECO threshold CAPTURE must refuse an effective date earlier
                # than the current open workweek, so it has to ask which
                # workweek is open now. It calls week_start_date() — the
                # resolver — plus _tz.local_date for the organization's own
                # today, and reads no raw setting. Narrowest possible entry:
                # the one module that asks, not the package. The ECO threshold
                # READER is deliberately absent from this list and is barred by
                # its own AST guard from computing a week at all.
                "eco_threshold_capture/boundary.py",
                # The forward-only Work Ledger producer resolves the
                # working-day result and FREEZES it onto the sealed day
                # (owner decision D-18D), and refuses to seal when the
                # resolver's own source label is "system" — the fail-open
                # path. It calls resolve_work_schedule() and reads no raw
                # setting; it is on this list only because it imports the
                # package to reach the resolver. Narrowest possible entry:
                # the one module that asks, not the package.
                "work_ledger_producer/context.py",
                # Owner ruling D2-A: the dedicated working-day classifier is
                # the semantic authority, so it — not the producer — is the
                # module that imports the package to reach the resolver. It
                # reads no raw setting and introduces no precedence of its
                # own; D5-A ratified the resolver's existing hierarchy.
                # Narrowest possible entry: the one module that asks.
                "working_day_classifier/classifier.py"}
    offenders = []
    for p in root.rglob("*.py"):
        rel = p.relative_to(root).as_posix()
        if rel.startswith("schedule_policy/") or rel in approved:
            continue
        # Strip comments AND docstrings before scanning: prose naming a key is
        # documentation, not a consumer. P8 added such a pointer to
        # settings/scope.py, and P10b a docstring cross-reference in merge_tiers.
        # Using the AST rather than regex so this cannot be fooled by formatting.
        code = _code_only(p.read_text(encoding="utf-8"))
        if re.search(r"schedule_policy|schedule\.(engine_enabled|working_days|"
                     r"shift_start_time|expected_work_hours|grace_minutes)", code):
            offenders.append(rel)
    assert offenders == [], f"schedule keys already consumed by: {offenders}"


# ── Render metadata is authored and coherent, ready for P8 ───────────────────

def test_key_meta_contribution_covers_the_visible_rows():
    meta = sp.KEY_META_CONTRIBUTION
    assert len(meta) == 13
    # week_start_day joins the withheld set: it is delivered by GET /settings
    # and honoured by the weekly rollups, but the admin UI has no control for
    # it yet (same treatment absent_after_hour and work_mode get).
    hidden = {sp.SETTING_ENGINE_ENABLED, sp.SETTING_SHADOW_MODE_ENABLED,
              sp.SETTING_ABSENT_AFTER_HOUR, sp.SETTING_WORK_MODE,
              sp.SETTING_WEEK_START_DAY}
    assert set(meta) == set(sp.SETTING_DEFAULTS) - hidden


def test_key_meta_contribution_is_well_formed():
    valid_types = {"bool", "int", "time", "enum", "multiselect", "shift_ref"}
    for key, m in sp.KEY_META_CONTRIBUTION.items():
        assert key in DEFAULTS, f"{key} has render metadata but is not registered"
        assert m["group"] == sp.GROUP
        assert m.get("label"), f"{key} has no label"
        assert m.get("type") in valid_types, f"{key} type={m.get('type')}"
        if m["type"] == "enum":
            assert m.get("options"), f"{key} is an enum with no options"
            assert DEFAULTS[key] in m["options"], (
                f"{key} default {DEFAULTS[key]!r} is not selectable")
        if m["type"] == "multiselect":
            assert m.get("options")
            assert set(json.loads(DEFAULTS[key])) <= set(m["options"])
        if m["type"] == "int":
            assert m.get("min") is not None and m.get("max") is not None
            if DEFAULTS[key]:
                assert m["min"] <= int(DEFAULTS[key]) <= m["max"]


def test_grace_bounds_match_the_existing_employee_endpoint():
    """The legacy per-employee grace endpoint validates 0..240; the new control
    must not allow a value that endpoint would reject."""
    m = sp.KEY_META_CONTRIBUTION[sp.SETTING_GRACE_MINUTES]
    assert (m["min"], m["max"]) == (0, 240)


# ══════════════════════════════════════════════════════════════════════════════
# P8 — the rows are now rendered in the Organization Settings admin UI
# ══════════════════════════════════════════════════════════════════════════════

def test_schedule_rows_are_merged_into_key_meta():
    """KEY_META is what makes a setting appear, and scoped_view iterates it."""
    for key in sp.KEY_META_CONTRIBUTION:
        assert key in settings_scope.KEY_META, f"{key} would not render"


def test_working_schedule_group_is_first_in_group_order():
    assert settings_scope.GROUP_ORDER[0] == sp.GROUP
    # The pre-existing groups keep their relative order.
    rest = settings_scope.GROUP_ORDER[1:]
    assert rest == ["Working Hours", "Attendance", "Screenshot Monitoring",
                    "Activity Tracking", "Notifications"]


def test_internal_switches_are_still_never_rendered():
    """engine_enabled and shadow_mode_enabled must stay deliverable by
    GET /settings but invisible in the admin UI."""
    for key in (sp.SETTING_ENGINE_ENABLED, sp.SETTING_SHADOW_MODE_ENABLED,
                sp.SETTING_ABSENT_AFTER_HOUR, sp.SETTING_WORK_MODE):
        assert key in DEFAULTS, f"{key} must still be delivered"
        assert key not in settings_scope.KEY_META, f"{key} must not render"


def test_scoped_view_now_returns_the_schedule_rows(db):
    rows = settings_scope.scoped_view(db, 4243, DEFAULTS, DESCRIPTIONS, scope="org")
    sched = [r for r in rows if r["key"].startswith("schedule.")]
    assert len(sched) == 13
    # "Absent After" is the row an admin needs to configure absence timing;
    # before it existed the cutoff was a system default no tenant could reach.
    assert sp.SETTING_ABSENT_AFTER_MINUTES in {r["key"] for r in sched}
    assert {r["group"] for r in sched} == {sp.GROUP}
    # Every row must carry what the renderer needs.
    for r in sched:
        assert r["label"] and r["type"]
        if r["type"] in ("enum", "multiselect"):
            assert r["options"], f"{r['key']} has no options"


def test_scoped_view_row_order_matches_the_approved_layout(db):
    rows = settings_scope.scoped_view(db, 4244, DEFAULTS, DESCRIPTIONS, scope="org")
    order = [r["key"] for r in rows if r["key"].startswith("schedule.")]
    assert order == list(sp.KEY_META_CONTRIBUTION.keys()), \
        "the rendered order must follow the approved Working Schedule layout"


def test_new_control_types_are_declared():
    types = {m["type"] for m in sp.KEY_META_CONTRIBUTION.values()}
    assert {"multiselect", "shift_ref", "time"} <= types

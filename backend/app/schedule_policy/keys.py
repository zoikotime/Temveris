"""
Temveris – Work Schedule Policy: setting keys and metadata
===========================================================
Phase P4 — **configuration model only**. This module declares the schedule
settings and their factory defaults. Nothing reads them yet: there is no
resolver (P5), no attendance change (P7/P10) and no UI (P8).

Registration reuses the existing extension point in ``app/settings/router.py``
— the same ``try/except`` merge block already used by ``app.active_time.config``
and ``app.attendance_policy``. Declaring a key there means:

  * ``PUT /settings/update`` and ``PUT /settings/scoped`` accept it (they reject
    anything absent from ``DEFAULTS``);
  * ``GET /settings`` returns it, resolved through the existing
    employee > team > org > default cascade in ``app/settings/scope.py``;
  * ``GET /settings/defaults`` lists it.

No new table, no migration, no new storage: every value is an ``app_settings``
row at one of the existing scope tiers, so inheritance is inherited for free.

Why these keys are NOT in ``settings.scope.KEY_META`` yet
--------------------------------------------------------
``KEY_META`` is what makes a setting *render* in the Organization Settings
admin UI, and ``scoped_view()`` iterates it. Three of these keys need control
types the UI does not implement yet (``multiselect``, ``shift_ref``, and
``time``, which is declared in the KEY_META docstring but never built), so
wiring them in now would put half-working number inputs for "Working Days" and
"Assigned Shift" in front of admins. The render metadata is therefore authored
here as ``KEY_META_CONTRIBUTION`` and left unwired; P8 merges it in the same
change that implements the controls. This is the pattern ``scope.py`` already
documents: "advanced engine tunables live in DEFAULTS (so GET /settings still
delivers them) but aren't shown in this admin UI".

Time Zone is deliberately absent
--------------------------------
It is already editable under Company & Workspace Profile
(``WorkspaceProfileTab.jsx`` → ``OrgSettings.default_timezone``). The resolver
will read it from there in P5. Adding a second control would duplicate a
setting.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

# ──────────────────────────────────────────────────────────────────────────
# Key names — all namespaced ``schedule.`` so they can never collide with an
# existing key and group cleanly in the admin UI.
# ──────────────────────────────────────────────────────────────────────────
SETTING_ENGINE_ENABLED        = "schedule.engine_enabled"
SETTING_SHADOW_MODE_ENABLED   = "schedule.shadow_mode_enabled"
SETTING_EMPLOYMENT_TYPE       = "schedule.employment_type"
SETTING_WORKING_DAYS          = "schedule.working_days"
SETTING_SHIFT_ID              = "schedule.shift_id"
SETTING_SHIFT_START_TIME      = "schedule.shift_start_time"
SETTING_SHIFT_END_TIME        = "schedule.shift_end_time"
SETTING_EXPECTED_WORK_HOURS   = "schedule.expected_work_hours"
SETTING_GRACE_MINUTES         = "schedule.grace_minutes"
SETTING_LATE_AFTER_MINUTES    = "schedule.late_after_minutes"
SETTING_VERY_LATE_AFTER_MIN   = "schedule.very_late_after_minutes"
SETTING_ALLOW_EARLY_CLOCK_IN  = "schedule.allow_early_clock_in"
SETTING_ALLOW_LATE_CLOCK_OUT  = "schedule.allow_late_clock_out"
SETTING_MIN_HOURS_REQUIRED    = "schedule.min_hours_required"
SETTING_ABSENT_AFTER_HOUR     = "schedule.absent_after_hour"
SETTING_ABSENT_AFTER_MINUTES  = "schedule.absent_after_minutes"
SETTING_WORK_MODE             = "schedule.work_mode"
SETTING_WEEK_START_DAY        = "schedule.week_start_day"

# ──────────────────────────────────────────────────────────────────────────
# Vocabularies
# ──────────────────────────────────────────────────────────────────────────
# Uppercase weekday names, matching the existing convention in
# attendance_policy.resolvers._WEEKDAY_NAMES (which indexes date.weekday()).
WEEKDAY_NAMES: List[str] = [
    "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY",
]

DEFAULT_WORKING_DAYS: List[str] = [
    "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY",
]

EMPLOYMENT_TYPES: List[str] = ["full_time", "part_time", "contract", "intern"]

WORK_MODES: List[str] = ["onsite", "remote", "hybrid"]

# Selectable daily durations (HH:MM). Extends the existing
# ``work_hours_threshold`` option list downward to 4:00 so a part-time employee
# can be given a real target at employee scope, and upward for long shifts.
# 8:45 is present because it is the system default.
DURATION_OPTIONS: List[str] = [
    "4:00", "4:30", "5:00", "5:30", "6:00", "6:30", "7:00", "7:30",
    "8:00", "8:30", "8:45", "9:00", "9:30", "10:00", "10:30",
    "11:00", "11:30", "12:00",
]

# ──────────────────────────────────────────────────────────────────────────
# Factory defaults. Every value is a STRING — ``app_settings.value`` is Text and
# the whole cascade is string-typed (see settings/scope.py).
#
# These defaults are chosen to reproduce a conventional Mon–Fri 09:00–18:00
# organisation. They are NOT read by anything in P4; the first consumer is the
# P5 resolver, and it stays behind ``schedule.engine_enabled`` (false) until the
# P11 rollout.
# ──────────────────────────────────────────────────────────────────────────
SETTING_DEFAULTS: Dict[str, str] = {
    # Master rollout switch. OFF: every consumer keeps its legacy behaviour.
    # Flipping it per-org is both the rollout and the rollback unit.
    SETTING_ENGINE_ENABLED:       "false",

    # Shadow mode: compute the resolver's answer alongside the legacy one and log
    # where they disagree, WITHOUT changing any response the UI reads. Separate
    # from engine_enabled on purpose — shadow mode is how an org earns the right
    # to enable the engine, so it has to work while the engine is still off.
    # Also a load guard: the shadow comparison costs extra queries per user on
    # /employees/team, so it must be opt-in per org rather than always-on.
    SETTING_SHADOW_MODE_ENABLED:  "false",

    # Visible in the admin UI from P8 onward (12 rows, in this order).
    SETTING_EMPLOYMENT_TYPE:      "full_time",
    SETTING_WORKING_DAYS:         json.dumps(DEFAULT_WORKING_DAYS),
    SETTING_SHIFT_ID:             "",          # empty = no shift assigned
    # Empty = NO scheduled shift, which is what makes rollout behaviour-neutral.
    # A worker with no shift start cannot be classified Late — exactly today's
    # behaviour for the 62 of 69 users who have no default_clock_in_time. A 09:00
    # default would have given every existing employee a scheduled start the
    # moment the engine was enabled, silently making them late-classifiable.
    # Once an admin configures a start time at org or team scope, employees
    # without their own override inherit it through the normal cascade.
    SETTING_SHIFT_START_TIME:     "",
    # Empty for the same reason as the start time, and for symmetry: an 18:00
    # default gave an organization that had configured NO shift at all an
    # implicit end of day, so its people accrued "left early" seconds on the
    # attendance report against a shift nobody set. 18:00 is one company's
    # afternoon.
    SETTING_SHIFT_END_TIME:       "",
    # MUST equal attendance_policy.DEFAULT_EXPECTED_WORK_SECONDS (28800).
    # It shipped as 8:45 while the value an unconfigured organization
    # actually resolved to was 8:00, because the resolver falls through to
    # the employment-type policy before reaching this default — so the
    # settings API advertised one system default and the product enforced
    # another. test_schedule_policy_keys pins the two together.
    SETTING_EXPECTED_WORK_HOURS:  "8:00",
    SETTING_GRACE_MINUTES:        "15",
    SETTING_LATE_AFTER_MINUTES:   "",          # empty = fall back to grace_minutes
    SETTING_VERY_LATE_AFTER_MIN:  "120",
    SETTING_ALLOW_EARLY_CLOCK_IN: "true",
    SETTING_ALLOW_LATE_CLOCK_OUT: "true",
    SETTING_MIN_HOURS_REQUIRED:   "4:00",

    # Minutes after SHIFT START before a no-show is Absent. Empty = not
    # configured, which keeps SETTING_ABSENT_AFTER_HOUR as the rule — see
    # classify._absence_cutoff. Empty rather than a number on purpose: a
    # default here would move the cutoff for every organization that has
    # configured a shift start, which is a live attendance policy change
    # nobody asked for. An org opts in by setting it.
    SETTING_ABSENT_AFTER_MINUTES: "",

    # Registered but intentionally NOT rendered — see KEY_META_CONTRIBUTION.
    # Still the cutoff for employees with NO scheduled start (62 of 69 users
    # when this shipped), and the floor-hour for those who have one.
    SETTING_ABSENT_AFTER_HOUR:    "12",
    SETTING_WORK_MODE:            "onsite",
    # Which day a week begins on, for weekly rollups and weekly pay periods.
    # MONDAY reproduces the previous `today.weekday()` arithmetic exactly; a
    # Sun-Thu (Gulf) or Sat-start organization could not express its week at
    # all, so its weekly windows straddled two of its own weeks.
    SETTING_WEEK_START_DAY:       "MONDAY",
}

SETTING_DESCRIPTIONS: Dict[str, str] = {
    SETTING_ENGINE_ENABLED:
        "Internal rollout switch for the Work Schedule engine (true/false). "
        "While false, schedule settings are stored but attendance keeps using "
        "its existing behaviour.",
    SETTING_SHADOW_MODE_ENABLED:
        "Internal verification switch (true/false). Computes the Schedule "
        "Resolver's answer alongside the existing one and logs any disagreement, "
        "without changing what any client receives.",
    SETTING_EMPLOYMENT_TYPE:
        "Default employment type for this scope — full_time, part_time, "
        "contract or intern. An individual's own type on their employee record "
        "remains authoritative; this supplies the default for new hires.",
    SETTING_WORKING_DAYS:
        'Days of the week that count as working days, as a JSON list of '
        'uppercase weekday names, e.g. ["MONDAY","TUESDAY"]. Days not listed '
        'are weekly offs.',
    SETTING_SHIFT_ID:
        "Assigned shift for this scope, if one was defined previously. "
        "Leave empty to use the shift start/end times below instead.",
    SETTING_SHIFT_START_TIME:
        "Scheduled shift start as HH:MM (24-hour). Late arrival is measured "
        "from this time. Leave empty for no scheduled start, in which case "
        "arrivals are never marked late.",
    SETTING_SHIFT_END_TIME:
        "Scheduled shift end as HH:MM (24-hour). Leave empty for no scheduled "
        "end, in which case early departure is not measured.",
    SETTING_EXPECTED_WORK_HOURS:
        "Expected worked time per day (HH:MM), EXCLUDING breaks. This is the "
        "single source of truth for required daily hours.",
    SETTING_GRACE_MINUTES:
        "Minutes after shift start that still count as on time. A clock-in "
        "within this window is Present.",
    SETTING_LATE_AFTER_MINUTES:
        "Minutes after shift start before a clock-in is marked Late. Leave "
        "empty to use the grace period.",
    SETTING_VERY_LATE_AFTER_MIN:
        "Minutes after shift start before a clock-in is marked Very Late.",
    SETTING_ALLOW_EARLY_CLOCK_IN:
        "Allow clocking in before the shift start time (true/false).",
    SETTING_ALLOW_LATE_CLOCK_OUT:
        "Allow clocking out after the shift end time (true/false).",
    SETTING_MIN_HOURS_REQUIRED:
        "Minimum worked time (HH:MM) for the day to count as attended at all.",
    SETTING_ABSENT_AFTER_MINUTES:
        "Employees are marked absent this many minutes after their scheduled "
        "shift start if they have not clocked in. Leave empty to fall back to "
        "the absence cutoff hour below.",
    SETTING_ABSENT_AFTER_HOUR:
        "Hour of day (0-23) after which an employee who has not clocked in is "
        "treated as Absent rather than yet to start. Used for employees with "
        "no scheduled shift start, and as the floor for those who have one — "
        "nobody is ever marked absent before their shift begins.",
    SETTING_WEEK_START_DAY:
        "The day a week starts on, used by weekly summaries and weekly pay "
        "periods. One of MONDAY..SUNDAY.",
    SETTING_WORK_MODE:
        "Default work mode for this scope — onsite, remote or hybrid. Stored "
        "for future hybrid/remote policy; not consumed yet.",
    SETTING_WEEK_START_DAY:
        "The day a week starts on, used by weekly summaries and weekly pay "
        "periods. One of MONDAY..SUNDAY.",
}

# ──────────────────────────────────────────────────────────────────────────
# Admin-UI render metadata — AUTHORED BUT NOT WIRED.
#
# P8 merges this into ``app.settings.scope.KEY_META`` and adds "Working
# Schedule" to GROUP_ORDER, in the same change that implements the three
# missing control types. Until then these keys are deliberately invisible in
# the admin UI (see the module docstring).
#
# Control types:
#   bool | int | time | enum      — already implemented by the admin UI
#   multiselect                   — NEW in P8 (working days)
#   shift_ref                     — NEW in P8 (options loaded from /schedule/shifts)
#
# Order here is the approved row order for the Working Schedule section.
# ``schedule.engine_enabled``, ``schedule.shadow_mode_enabled``,
# ``schedule.absent_after_hour`` and ``schedule.work_mode`` are intentionally
# omitted: the first two are internal rollout/verification switches, work_mode
# is not consumed yet, and the absence cutoff is now configured as
# ``schedule.absent_after_minutes`` (relative to the shift) — the hour survives
# as the fallback for employees who have no scheduled start.
# ──────────────────────────────────────────────────────────────────────────
GROUP = "Working Schedule"

KEY_META_CONTRIBUTION: Dict[str, Dict[str, Any]] = {
    SETTING_EMPLOYMENT_TYPE: {
        "group": GROUP, "label": "Employment type", "type": "enum",
        "options": EMPLOYMENT_TYPES,
        "option_labels": {
            "full_time": "Full Time", "part_time": "Part Time",
            "contract": "Contract", "intern": "Intern",
        },
    },
    SETTING_WORKING_DAYS: {
        "group": GROUP, "label": "Working days", "type": "multiselect",
        "options": WEEKDAY_NAMES,
        "option_labels": {d: d.title() for d in WEEKDAY_NAMES},
    },
    SETTING_SHIFT_ID: {
        "group": GROUP, "label": "Assigned shift", "type": "shift_ref",
    },
    SETTING_SHIFT_START_TIME: {
        "group": GROUP, "label": "Shift start time", "type": "time",
    },
    SETTING_SHIFT_END_TIME: {
        "group": GROUP, "label": "Shift end time", "type": "time",
    },
    SETTING_EXPECTED_WORK_HOURS: {
        "group": GROUP, "label": "Expected daily hours", "type": "enum",
        "options": DURATION_OPTIONS,
    },
    SETTING_GRACE_MINUTES: {
        "group": GROUP, "label": "Grace period", "type": "int",
        "unit": "minutes", "min": 0, "max": 240,
    },
    SETTING_LATE_AFTER_MINUTES: {
        "group": GROUP, "label": "Late after", "type": "int",
        "unit": "minutes", "min": 0, "max": 480,
    },
    SETTING_VERY_LATE_AFTER_MIN: {
        "group": GROUP, "label": "Very late after", "type": "int",
        "unit": "minutes", "min": 0, "max": 720,
    },
    SETTING_ABSENT_AFTER_MINUTES: {
        "group": GROUP, "label": "Absent After", "type": "int",
        "unit": "minutes", "min": 0, "max": 1440,
    },
    SETTING_ALLOW_EARLY_CLOCK_IN: {
        "group": GROUP, "label": "Allow early clock in", "type": "bool",
    },
    SETTING_ALLOW_LATE_CLOCK_OUT: {
        "group": GROUP, "label": "Allow late clock out", "type": "bool",
    },
    SETTING_MIN_HOURS_REQUIRED: {
        "group": GROUP, "label": "Minimum required hours", "type": "enum",
        "options": DURATION_OPTIONS,
    },
}

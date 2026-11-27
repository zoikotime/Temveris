"""
Temveris – Work Schedule Policy package
========================================
Phase P4: the configuration model only — setting keys, factory defaults and
(unwired) admin-UI render metadata. See ``keys.py``.

Nothing in this package is read by any code path yet. The resolver
(``resolve_work_schedule``) lands in P5, and every consumer stays behind
``schedule.engine_enabled`` (default false) until the P11 rollout.

Mirrors the structure of ``app.attendance_policy``: a feature package that
contributes its keys to the settings router through the existing
``SETTING_DEFAULTS`` / ``SETTING_DESCRIPTIONS`` merge point, so the
employee > team > org > system-default cascade applies with no new storage.
"""

from .classify import (
    ArrivalStatus,
    ArrivalResult,
    DepartureStatus,
    DepartureResult,
    classify_arrival,
    classify_departure,
)
from .resolver import (
    EffectiveSchedule,
    week_start_date,
    resolve_work_schedule,
    resolve_work_schedules,
)
from .keys import (
    # Registration payload consumed by app/settings/router.py
    SETTING_DEFAULTS,
    SETTING_DESCRIPTIONS,
    # Render metadata — authored, wired into scope.KEY_META in P8
    KEY_META_CONTRIBUTION,
    GROUP,
    # Vocabularies
    WEEKDAY_NAMES,
    DEFAULT_WORKING_DAYS,
    EMPLOYMENT_TYPES,
    WORK_MODES,
    DURATION_OPTIONS,
    # Key names
    SETTING_ENGINE_ENABLED,
    SETTING_SHADOW_MODE_ENABLED,
    SETTING_EMPLOYMENT_TYPE,
    SETTING_WORKING_DAYS,
    SETTING_SHIFT_ID,
    SETTING_SHIFT_START_TIME,
    SETTING_SHIFT_END_TIME,
    SETTING_EXPECTED_WORK_HOURS,
    SETTING_GRACE_MINUTES,
    SETTING_LATE_AFTER_MINUTES,
    SETTING_VERY_LATE_AFTER_MIN,
    SETTING_ALLOW_EARLY_CLOCK_IN,
    SETTING_ALLOW_LATE_CLOCK_OUT,
    SETTING_MIN_HOURS_REQUIRED,
    SETTING_ABSENT_AFTER_HOUR,
    SETTING_ABSENT_AFTER_MINUTES,
    SETTING_WORK_MODE,
    SETTING_WEEK_START_DAY,
)

__all__ = [
    "EffectiveSchedule",
    "resolve_work_schedule",
    "resolve_work_schedules",
    "ArrivalStatus",
    "ArrivalResult",
    "DepartureStatus",
    "DepartureResult",
    "classify_arrival",
    "classify_departure",
    "SETTING_DEFAULTS",
    "SETTING_DESCRIPTIONS",
    "KEY_META_CONTRIBUTION",
    "GROUP",
    "WEEKDAY_NAMES",
    "DEFAULT_WORKING_DAYS",
    "EMPLOYMENT_TYPES",
    "WORK_MODES",
    "DURATION_OPTIONS",
    "SETTING_ENGINE_ENABLED",
    "SETTING_SHADOW_MODE_ENABLED",
    "SETTING_EMPLOYMENT_TYPE",
    "SETTING_WORKING_DAYS",
    "SETTING_SHIFT_ID",
    "SETTING_SHIFT_START_TIME",
    "SETTING_SHIFT_END_TIME",
    "SETTING_EXPECTED_WORK_HOURS",
    "SETTING_GRACE_MINUTES",
    "SETTING_LATE_AFTER_MINUTES",
    "SETTING_VERY_LATE_AFTER_MIN",
    "SETTING_ALLOW_EARLY_CLOCK_IN",
    "SETTING_ALLOW_LATE_CLOCK_OUT",
    "SETTING_MIN_HOURS_REQUIRED",
    "SETTING_ABSENT_AFTER_HOUR",
    "SETTING_ABSENT_AFTER_MINUTES",
    "SETTING_WORK_MODE",
    "SETTING_WEEK_START_DAY",
    "week_start_date",
]

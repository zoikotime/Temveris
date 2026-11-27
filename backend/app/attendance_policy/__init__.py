"""
Temveris – Attendance Policy package
=====================================
Phase 1 policy/configuration foundation for the Controlled Clock-Out feature.

Pure, reusable, multi-tenant resolvers only. Nothing here is wired into the
existing clock-in/clock-out/break/tracker flows yet — see resolvers.py.
"""

from .resolvers import (
    # Public resolvers
    resolve_expected_work_seconds,
    is_flexible_clock_out_day,
    engine_enabled_for_org,
    # Cascade-aware attendance policy reads
    attendance_policy_for,
    policy_bool,
    policy_int,
    policy_float,
    SETTING_EARLY_CLOCKOUT_ENABLED,
    SETTING_DISCONNECT_AUTOCLOSE,
    SETTING_DISCONNECT_GRACE_MINUTES,
    SETTING_IDLE_ALERT_MINUTES,
    SETTING_DESKTOP_ONLY_CLOCK_IN,
    SETTING_RISK_DEFICIT_PCT,
    SETTING_RISK_HIGH_PCT,
    # Parsing helpers (exposed for reuse/tests)
    parse_hhmm_to_seconds,
    normalize_employment_type,
    default_expected_seconds_for_type,
    # Defaults
    DEFAULT_EXPECTED_WORK_SECONDS,
    DEFAULT_EXPECTED_WORK_DURATIONS,
    DEFAULT_FLEXIBLE_CLOCK_OUT_DAYS,
    # Setting keys + registration payload (consumed by the settings router)
    SETTING_EXPECTED_WORK_DURATIONS,
    SETTING_FLEXIBLE_CLOCK_OUT_DAYS,
    SETTING_WORK_HOURS_THRESHOLD,
    SETTING_DEFAULTS,
    SETTING_DESCRIPTIONS,
)

__all__ = [
    "resolve_expected_work_seconds",
    "is_flexible_clock_out_day",
    "engine_enabled_for_org",
    "attendance_policy_for",
    "policy_bool",
    "policy_int",
    "policy_float",
    "SETTING_EARLY_CLOCKOUT_ENABLED",
    "SETTING_DISCONNECT_AUTOCLOSE",
    "SETTING_DISCONNECT_GRACE_MINUTES",
    "SETTING_IDLE_ALERT_MINUTES",
    "SETTING_DESKTOP_ONLY_CLOCK_IN",
    "SETTING_RISK_DEFICIT_PCT",
    "SETTING_RISK_HIGH_PCT",
    "parse_hhmm_to_seconds",
    "normalize_employment_type",
    "default_expected_seconds_for_type",
    "DEFAULT_EXPECTED_WORK_SECONDS",
    "DEFAULT_EXPECTED_WORK_DURATIONS",
    "DEFAULT_FLEXIBLE_CLOCK_OUT_DAYS",
    "SETTING_EXPECTED_WORK_DURATIONS",
    "SETTING_FLEXIBLE_CLOCK_OUT_DAYS",
    "SETTING_WORK_HOURS_THRESHOLD",
    "SETTING_DEFAULTS",
    "SETTING_DESCRIPTIONS",
]

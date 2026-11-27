"""
Temveris — Active Time Engine
==============================

A deterministic, configurable, screenshot-correlated activity-scoring engine
that replaces the old single-OR ``is_active`` boolean (which saturated to
100% for idle users and collapsed to 20-30% for real workers).

Public surface:
    config.ScoringConfig      — weights + thresholds (loadable from AppSettings)
    config.load_config(db, org_id)  — build a ScoringConfig from the settings table
    engine.WindowInput        — raw per-interval input
    engine.score_windows(...) — score an ordered sequence of windows
    engine.aggregate_day(...) — roll scored windows up to a daily Active Time %
    engine.expand_minutes(...) — per-minute classification for storage

The engine is pure (no DB, no clock) so it is trivially testable; persistence
and wiring live in the tracking/employees routers (Phase 2).
"""

from .config import ScoringConfig, load_config, SETTING_DEFAULTS, SETTING_DESCRIPTIONS
from .engine import (
    WindowInput,
    WindowScore,
    DailyScore,
    MinuteScore,
    score_window,
    score_windows,
    expand_minutes,
    aggregate_day,
    classify_score,
)

__all__ = [
    "ScoringConfig",
    "load_config",
    "SETTING_DEFAULTS",
    "SETTING_DESCRIPTIONS",
    "WindowInput",
    "WindowScore",
    "DailyScore",
    "MinuteScore",
    "score_window",
    "score_windows",
    "expand_minutes",
    "aggregate_day",
    "classify_score",
]

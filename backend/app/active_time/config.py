"""
Active Time Engine — configuration.

All tunables are admin-configurable via the existing AppSettings key/value
table. The keys + factory defaults are exported as ``SETTING_DEFAULTS`` so the
settings router can register them (its PUT validator rejects unknown keys, so
new keys MUST be declared there to be editable).

Scoring model (enterprise, role-fair):
  Activity Score = keyboard 30% + mouse 20% + focus 20% + continuity 15%
                 + screenshot 10% + app-engagement 5%

  FULLY ACTIVE = combined score >= fully_active_threshold (80)
                 AND (keyboard >= min OR mouse >= min)        ← OR, not AND
  Dual-input bonus: keyboard >= min AND mouse >= min → ×(1 + dual_input_bonus)

The OR gate fixes role bias: a developer/writer (heavy keyboard, light mouse)
or a designer (heavy mouse, light keyboard) can both be fully active. The
keyboard/mouse minimums are per-MINUTE rates scaled by the real interval
length, so "50 per 30s" is expressed as 100/min and stays correct at any
admin-configured screenshot interval.

Coverage (tracked_time / worked_time) is enforced structurally — active_pct can
never exceed the coverage ratio because untracked worked time counts as idle —
so we surface it + a LOW_COVERAGE flag rather than double-penalising.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


# ── Setting keys + factory defaults (registered in app/settings/router.py) ──
SETTING_DEFAULTS: Dict[str, str] = {
    # ── Calibration (2026-06-22) ──────────────────────────────────────────────
    # The original 100/min minimums were physically unrealistic: 100 mouse
    # events/min ≈ 1.7/sec sustained, which almost no real user hits, so the
    # mouse sub-score saturated low and a genuinely-busy developer scored ~77%.
    # Validated against the persona harness, these calibrated values produce the
    # target enterprise spread (dev 85-95, designer 75-90, research 65-85, mixed
    # 60-80, partial-idle 35-55, away 0-15) while keeping every persona DISTINCT
    # and correctly ordered. Tuning is configuration-only — admins can override
    # per-org via /settings; reverting these constants restores the old model.
    "active_time.min_keystrokes_per_min":    "75",    # = ~37 per 30s interval (OR minimum)
    "active_time.min_mouse_events_per_min":  "55",    # clicks + scrolls + move-ticks (OR minimum)
    "active_time.weight_keyboard":           "0.28",
    "active_time.weight_mouse":              "0.17",
    "active_time.weight_focus":              "0.25",
    "active_time.weight_continuity":         "0.15",
    "active_time.weight_screenshot":         "0.10",
    "active_time.weight_app_engagement":     "0.05",
    "active_time.fully_active_threshold":    "80",    # combined score for the FULLY ACTIVE label
    "active_time.dual_input_bonus":          "0.10",  # +10% when BOTH keyboard & mouse minimums met
    "active_time.degraded_cap":              "60",    # max % when native input hook is down
    "active_time.daily_cap":                 "95",    # anti-inflation cap on the daily Active %
    "active_time.coverage_min_pct":          "90",    # below this tracked/worked → LOW_COVERAGE flag
    "active_time.idle_threshold_seconds":    "180",   # unify idle definition (3 min)
    "active_time.continuity_neighbors":      "1",     # neighbourhood radius for continuity smoothing
    # ── Active-seconds calibration (2026-06-30) ───────────────────────────────
    # The desktop tracker samples OS idle on a fixed poll (ACTIVITY_POLL_MS, 3s)
    # and increments `active_seconds` ONCE per active poll — so the value it
    # reports is a COUNT OF ACTIVE POLLS, not literal seconds. Left unscaled it
    # caps the focus sub-score at ~interval/poll/interval = ~0.33. The ingest
    # layer multiplies the reported value by this poll period (and clamps to the
    # interval) to recover true active seconds. Set to 1 for any tracker build
    # that already reports real seconds (disables scaling). Tracker unchanged.
    "active_time.active_poll_period_seconds": "3",
    # Browser-activity sub-score weight — only contributes when the service can
    # supply a per-window browser active-ratio (else it is renormalised out, so
    # the six-channel model is unchanged when no browser data exists).
    "active_time.weight_browser":            "0.06",
    # ── Displayed Active Time band (2026-06-30) ───────────────────────────────
    # The number on the dashboard cards is a per-window score built ONLY from
    # keyboard + mouse activity and averaged across the day, mapped into a
    # [floor..100] band so it never reads a discouraging 0–10%. It is NOT the
    # raw active/worked ratio (that stays in raw_active_pct + active_seconds for
    # duration reports). knee = the per-window activity that earns the "both
    # minimums met" anchor (85); full_per_min = the per-minute activity rate
    # that saturates a channel to 100. All configurable.
    "active_time.display_floor":             "65",   # lowest % a card can show (band floor)
    "active_time.activity_knee":             "50",   # per-window keyboard / mouse minimum
    "active_time.activity_full_per_min":     "100",  # per-minute rate that saturates a channel
}

SETTING_DESCRIPTIONS: Dict[str, str] = {
    "active_time.min_keystrokes_per_min":    "Keystrokes-per-minute that satisfy the keyboard side of the OR activity gate (scaled by interval).",
    "active_time.min_mouse_events_per_min":  "Mouse events-per-minute (clicks+scrolls+movement) that satisfy the mouse side of the OR activity gate.",
    "active_time.weight_keyboard":           "Weight of the keyboard sub-score in the Activity Score (0..1).",
    "active_time.weight_mouse":              "Weight of the mouse sub-score (0..1).",
    "active_time.weight_focus":              "Weight of the focus / within-window engagement sub-score (0..1).",
    "active_time.weight_continuity":         "Weight of the continuity sub-score (penalises burst-then-idle) (0..1).",
    "active_time.weight_screenshot":         "Weight of the screenshot-validation sub-score (0..1).",
    "active_time.weight_app_engagement":     "Weight of the app-engagement sub-score — real foreground app + interaction (0..1).",
    "active_time.fully_active_threshold":    "Combined score (0..100) required for the FULLY ACTIVE label.",
    "active_time.dual_input_bonus":          "Score multiplier bonus when BOTH keyboard and mouse minimums are met (e.g. 0.10 = +10%).",
    "active_time.degraded_cap":              "Maximum Active % a machine can score when the native input hook is unavailable (0..100).",
    "active_time.daily_cap":                 "Anti-inflation cap applied to the daily Active % (0..100).",
    "active_time.coverage_min_pct":          "Tracked/worked coverage below this percent flags the day LOW_COVERAGE (no extra penalty — coverage is already in the denominator).",
    "active_time.idle_threshold_seconds":    "Seconds of no input before a stretch is considered idle.",
    "active_time.continuity_neighbors":      "How many neighbouring windows on each side feed the continuity smoothing.",
    "active_time.active_poll_period_seconds": "Seconds between the tracker's idle-sampling polls; the reported active_seconds (a count of active polls) is multiplied by this to recover real active seconds. Set 1 if the tracker already reports real seconds.",
    "active_time.weight_browser":            "Weight of the browser-activity sub-score (0..1); applied only when per-window browser active-ratio is available.",
    "active_time.display_floor":             "Lowest Active Time % a dashboard card can display (the band floor). The displayed % spans floor..100.",
    "active_time.activity_knee":             "Per-window keyboard / mouse count that earns the 'both minimums met' anchor on the displayed band.",
    "active_time.activity_full_per_min":     "Per-minute keyboard / mouse rate at which a channel saturates the displayed band to 100%.",
}


def _to_float(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_int(v, default: int) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


@dataclass
class ScoringConfig:
    """Resolved, validated scoring parameters."""
    min_keystrokes_per_min: float = 75.0
    min_mouse_events_per_min: float = 55.0
    w_keyboard: float = 0.28
    w_mouse: float = 0.17
    w_focus: float = 0.25
    w_continuity: float = 0.15
    w_screenshot: float = 0.10
    w_app: float = 0.05
    fully_active_threshold: float = 80.0
    dual_input_bonus: float = 0.10
    degraded_cap: float = 60.0
    daily_cap: float = 95.0
    coverage_min_pct: float = 90.0
    idle_threshold_seconds: int = 180
    continuity_neighbors: int = 1
    active_poll_period_seconds: int = 3
    w_browser: float = 0.06
    display_floor: float = 65.0
    activity_knee: float = 50.0
    activity_full_per_min: float = 100.0

    # Weights in canonical order (keyboard, mouse, focus, continuity, screenshot, app)
    _DEFAULT_WEIGHTS = {
        "keyboard": 0.28, "mouse": 0.17, "focus": 0.25,
        "continuity": 0.15, "screenshot": 0.10, "app": 0.05,
    }

    def normalized_weights(self, include_browser: bool = False) -> Dict[str, float]:
        """Weights re-normalised to sum to 1.0 (defensive against bad admin input).

        When ``include_browser`` is True the browser sub-score weight joins the
        normalisation (used only for windows that actually carry browser data);
        the default six-channel result is unchanged so existing callers/tests
        see identical behaviour.
        """
        raw = {
            "keyboard": max(0.0, self.w_keyboard),
            "mouse": max(0.0, self.w_mouse),
            "focus": max(0.0, self.w_focus),
            "continuity": max(0.0, self.w_continuity),
            "screenshot": max(0.0, self.w_screenshot),
            "app": max(0.0, self.w_app),
        }
        if include_browser:
            raw["browser"] = max(0.0, self.w_browser)
        total = sum(raw.values())
        if total <= 0:
            return dict(self._DEFAULT_WEIGHTS)
        return {k: v / total for k, v in raw.items()}

    @classmethod
    def from_settings(cls, settings: Dict[str, str]) -> "ScoringConfig":
        """Build from a {key: value} map (missing keys fall back to defaults)."""
        g = lambda k: settings.get(k, SETTING_DEFAULTS[k])
        return cls(
            min_keystrokes_per_min=max(1.0, _to_float(g("active_time.min_keystrokes_per_min"), 75.0)),
            min_mouse_events_per_min=max(1.0, _to_float(g("active_time.min_mouse_events_per_min"), 55.0)),
            w_keyboard=_to_float(g("active_time.weight_keyboard"), 0.28),
            w_mouse=_to_float(g("active_time.weight_mouse"), 0.17),
            w_focus=_to_float(g("active_time.weight_focus"), 0.25),
            w_continuity=_to_float(g("active_time.weight_continuity"), 0.15),
            w_screenshot=_to_float(g("active_time.weight_screenshot"), 0.10),
            w_app=_to_float(g("active_time.weight_app_engagement"), 0.05),
            fully_active_threshold=min(100.0, max(0.0, _to_float(g("active_time.fully_active_threshold"), 80.0))),
            dual_input_bonus=max(0.0, _to_float(g("active_time.dual_input_bonus"), 0.10)),
            degraded_cap=min(100.0, max(0.0, _to_float(g("active_time.degraded_cap"), 60.0))),
            daily_cap=min(100.0, max(1.0, _to_float(g("active_time.daily_cap"), 95.0))),
            coverage_min_pct=min(100.0, max(0.0, _to_float(g("active_time.coverage_min_pct"), 90.0))),
            idle_threshold_seconds=max(1, _to_int(g("active_time.idle_threshold_seconds"), 180)),
            continuity_neighbors=max(0, _to_int(g("active_time.continuity_neighbors"), 1)),
            active_poll_period_seconds=max(1, _to_int(g("active_time.active_poll_period_seconds"), 3)),
            w_browser=_to_float(g("active_time.weight_browser"), 0.06),
            display_floor=min(99.0, max(0.0, _to_float(g("active_time.display_floor"), 65.0))),
            activity_knee=max(1.0, _to_float(g("active_time.activity_knee"), 50.0)),
            activity_full_per_min=max(1.0, _to_float(g("active_time.activity_full_per_min"), 100.0)),
        )


# Module-level default singleton (used when no DB session is available).
DEFAULT_CONFIG = ScoringConfig()


def load_config(db=None, org_id=None) -> ScoringConfig:
    """Load scoring config for ONE organization; fall back to defaults.

    Kept dependency-light: imports models lazily so the pure engine has no
    hard DB dependency and the test suite can call it with ``db=None``.

    ``org_id`` is required for an org's tuning to apply. Omitting it reads the
    NULL-organization tier, which holds no tunables, so the built-in defaults
    are returned — safe by construction, never another tenant's values. This
    query previously referenced ``AppSettings.team_id`` (a column that does not
    exist), so it raised and the ``except`` below silently returned defaults for
    every tenant; the org filter is added in the same change that repairs it, so
    fixing the crash can never activate an org-blind read.
    """
    if db is None:
        return ScoringConfig()
    try:
        from database import models  # lazy: avoid import cycles / DB requirement
        rows = (
            db.query(models.AppSettings)
            .filter(models.AppSettings.organization_id == org_id,
                    models.AppSettings.key.in_(SETTING_DEFAULTS.keys()),
                    models.AppSettings.team_key.is_(None),   # org-level only
                    models.AppSettings.user_id.is_(None))
            .all()
        )
        settings = {r.key: r.value for r in rows}
        return ScoringConfig.from_settings(settings)
    except Exception:
        # Never let a settings hiccup break activity scoring — use defaults.
        return ScoringConfig()

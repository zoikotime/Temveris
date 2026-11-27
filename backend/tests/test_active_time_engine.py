"""Active Time Engine — scoring regression tests.

Pure-engine tests (no DB). Cover the nine product acceptance cases, the
role-fairness OR-gate (developer / designer / writer all fully active),
app-engagement, the dual-input bonus, interval-scaling, degraded hook,
continuity, anomaly flags, coverage, and anti-inflation.

Run:  pytest backend/tests/test_active_time_engine.py -q
"""
from __future__ import annotations

from app.active_time import (
    ScoringConfig,
    WindowInput,
    score_window,
    score_windows,
    expand_minutes,
    aggregate_day,
    classify_score,
)
from app.active_time.engine import LABEL_HIGHLY, LABEL_BREAK, CLASS_ACTIVE, CLASS_BREAK

CFG = ScoringConfig()   # defaults: min 100/min each; weights kb.30 mouse.20 focus.20 cont.15 ss.10 app.05
# At the 30s default interval each per-interval minimum is 50.


def windows(n, **kw):
    """n identical 30s windows with a real foreground app + screenshot."""
    kw.setdefault("interval_seconds", 30)
    kw.setdefault("has_screenshot", True)
    kw.setdefault("app_name", "VS Code")
    return [WindowInput(**kw) for _ in range(n)]


def one(**kw):
    kw.setdefault("interval_seconds", 30)
    kw.setdefault("has_screenshot", True)
    kw.setdefault("app_name", "VS Code")
    return score_window(WindowInput(**kw), CFG)


def pct(ws_inputs, worked=None):
    return aggregate_day(score_windows(ws_inputs, CFG), worked, CFG).active_pct


# ──────────────────────────────────────────────────────────────────────────
# The nine product acceptance cases
# ──────────────────────────────────────────────────────────────────────────
# NOTE (2026-06-30): the DISPLAYED active_pct is now the keyboard+mouse band
# score (floor 65 .. 100), averaged across windows — not the active/worked
# ratio. Ranges below reflect that band. The real ratio lives in raw_active_pct.
def test_case1_heavy_typing_and_mouse_high_band():
    p = pct(windows(16, key_presses=90, mouse_clicks=45, mouse_scrolls=10, mouse_moves=25, active_seconds=30))
    assert 95 <= p <= 100, p


def test_case2_moderate_work_band():
    p = pct(windows(16, key_presses=35, mouse_clicks=12, mouse_scrolls=4, mouse_moves=10, active_seconds=22))
    assert 70 <= p <= 85, p


def test_case3_light_work_band():
    p = pct(windows(16, key_presses=18, mouse_clicks=8, mouse_scrolls=2, mouse_moves=4, active_seconds=15))
    assert 65 <= p <= 78, p


def test_case4_mostly_idle_near_floor():
    p = pct(windows(16, key_presses=4, mouse_clicks=2, mouse_scrolls=0, mouse_moves=2, active_seconds=3))
    assert 65 <= p <= 72, p


def test_case5_break_user_active_frozen():
    on_break = windows(8, key_presses=90, mouse_clicks=45, mouse_scrolls=10, mouse_moves=25, active_seconds=30, on_break=True)
    scored = score_windows(on_break, CFG)
    assert all(w.label == LABEL_BREAK and w.classification == CLASS_BREAK for w in scored)
    assert all(w.active_credit_seconds == 0 for w in scored)
    day = aggregate_day(scored, worked_seconds=None, config=CFG)
    assert day.active_pct == 0
    assert day.break_seconds == 8 * 30


def test_case6_high_mouse_low_keyboard_moderate():
    p = pct(windows(12, key_presses=5, mouse_clicks=50, mouse_scrolls=15, mouse_moves=20, active_seconds=28))
    assert 75 <= p <= 95, p


def test_case7_high_keyboard_low_mouse_moderate():
    p = pct(windows(12, key_presses=90, mouse_clicks=2, mouse_scrolls=1, mouse_moves=3, active_seconds=28))
    assert 75 <= p <= 95, p


def test_case6_vs_case7_keyboard_and_mouse_symmetric():
    # The displayed band weights keyboard and mouse EQUALLY (one-min-met → 75-85
    # either way), so a mouse-heavy and a keyboard-heavy window land close.
    mouse_heavy = pct(windows(12, key_presses=5, mouse_clicks=50, mouse_scrolls=15, mouse_moves=20, active_seconds=28))
    kb_heavy = pct(windows(12, key_presses=90, mouse_clicks=2, mouse_scrolls=1, mouse_moves=3, active_seconds=28))
    assert abs(kb_heavy - mouse_heavy) <= 5, (kb_heavy, mouse_heavy)


def test_case8_or_gate_label_semantics():
    # Neither minimum met -> NOT fully active (label), but no artificial cap.
    neither = one(key_presses=30, mouse_clicks=12, mouse_scrolls=4, mouse_moves=10, active_seconds=20)  # 26 mouse
    # One channel meets the minimum -> can be fully active.
    one_met = one(key_presses=90, mouse_clicks=12, mouse_scrolls=4, mouse_moves=10, active_seconds=30)
    assert neither.fully_active is False
    assert one_met.fully_active is True


def test_case9_continuous_8h_high_band():
    day = windows(960, key_presses=120, mouse_clicks=60, mouse_scrolls=20, mouse_moves=30, active_seconds=30)
    agg = aggregate_day(score_windows(day, CFG), None, CFG)
    assert agg.raw_active_pct >= 99           # real ratio still near 100
    assert 95 <= agg.active_pct <= 100        # displayed band: outstanding


# ──────────────────────────────────────────────────────────────────────────
# Role fairness — the core fix: OR gate, no role bias
# ──────────────────────────────────────────────────────────────────────────
def test_developer_keyboard_heavy_is_fully_active():
    dev = one(key_presses=220, mouse_clicks=12, mouse_scrolls=3, mouse_moves=5, active_seconds=30)  # 20 mouse
    assert dev.fully_active is True
    assert dev.score >= 80


def test_designer_mouse_heavy_is_fully_active():
    designer = one(key_presses=25, mouse_clicks=200, mouse_scrolls=120, mouse_moves=30, active_seconds=30)  # 350 mouse
    assert designer.fully_active is True
    assert designer.score >= 80


def test_writer_keyboard_heavy_is_fully_active():
    writer = one(key_presses=180, mouse_clicks=20, mouse_scrolls=5, mouse_moves=5, active_seconds=30)  # 30 mouse
    assert writer.fully_active is True
    assert writer.score >= 80


def test_developer_outscores_random_mouse_mover():
    dev = one(key_presses=220, mouse_clicks=12, mouse_scrolls=3, mouse_moves=5, active_seconds=30)
    mover = one(key_presses=2, mouse_clicks=0, mouse_scrolls=0, mouse_moves=30, active_seconds=30)  # pure jiggle
    assert dev.score > mover.score, (dev.score, mover.score)


def test_dual_input_bonus_beats_single_channel():
    single = one(key_presses=90, mouse_clicks=2, mouse_scrolls=1, mouse_moves=3, active_seconds=30)   # kb only
    both = one(key_presses=90, mouse_clicks=40, mouse_scrolls=10, mouse_moves=10, active_seconds=30)  # kb + mouse
    assert both.score > single.score, (both.score, single.score)


# ──────────────────────────────────────────────────────────────────────────
# App engagement (idle app != active app)
# ──────────────────────────────────────────────────────────────────────────
def test_app_engagement_active_vs_idle_vs_none():
    active = one(key_presses=20, mouse_clicks=10, active_seconds=20, app_name="VS Code")
    idle_app = score_window(WindowInput(30, key_presses=0, mouse_clicks=0, active_seconds=0,
                            has_screenshot=True, app_name="Chrome"), CFG)
    no_app = score_window(WindowInput(30, key_presses=0, mouse_clicks=0, active_seconds=0,
                          has_screenshot=True, app_name="Desktop"), CFG)
    assert active.app_engagement_score == 1.0
    assert idle_app.app_engagement_score == 0.4
    assert no_app.app_engagement_score == 0.0


def test_idle_chrome_scores_below_active_chrome():
    active_chrome = one(key_presses=40, mouse_clicks=20, mouse_scrolls=10, mouse_moves=15, active_seconds=28, app_name="Chrome")
    idle_chrome = score_window(WindowInput(30, key_presses=0, mouse_clicks=0, active_seconds=0,
                               has_screenshot=True, app_name="Chrome"), CFG)
    assert idle_chrome.score < active_chrome.score
    assert idle_chrome.score < 21   # essentially inactive


# ──────────────────────────────────────────────────────────────────────────
# Interval-scaling: the OR minimums are per-minute rates
# ──────────────────────────────────────────────────────────────────────────
def test_threshold_scales_with_interval():
    # 50 keys + 50 mouse in 30s clears BOTH per-interval minimums (100/min × 0.5).
    w30 = score_window(WindowInput(30, key_presses=60, mouse_clicks=40, mouse_scrolls=10,
                       mouse_moves=10, active_seconds=30, has_screenshot=True, app_name="X"), CFG)
    assert w30.fully_active is True

    # The SAME 60+60 over a 30-minute interval clears neither (min = 100/min × 30 = 3000).
    w1800_low = score_window(WindowInput(1800, key_presses=60, mouse_clicks=40, mouse_scrolls=10,
                            mouse_moves=10, active_seconds=1800, has_screenshot=True, app_name="X"), CFG)
    w1800_ok = score_window(WindowInput(1800, key_presses=3000, mouse_clicks=2000, mouse_scrolls=500,
                           mouse_moves=600, active_seconds=1800, has_screenshot=True, app_name="X"), CFG)
    assert w1800_low.fully_active is False, "flat 60 must NOT pass a 30-min interval"
    assert w1800_ok.fully_active is True


# ──────────────────────────────────────────────────────────────────────────
# Degraded hook
# ──────────────────────────────────────────────────────────────────────────
def test_degraded_hook_capped_and_flagged():
    day = windows(16, key_presses=0, mouse_clicks=0, mouse_scrolls=0, mouse_moves=30, active_seconds=30, hook_active=False)
    scored = score_windows(day, CFG)
    assert all("LIMITED_TRACKING" in w.flags for w in scored)
    assert all(w.score <= CFG.degraded_cap + 0.01 for w in scored)
    assert all(w.fully_active is False for w in scored)
    agg = aggregate_day(scored, None, CFG)
    # Per-window weighted score stays degraded-capped (drives durations); the
    # DISPLAYED band is keyboard+mouse only, so it sits in the normal band.
    assert CFG.display_floor <= agg.active_pct <= 100
    assert "LIMITED_TRACKING" in agg.flags


# ──────────────────────────────────────────────────────────────────────────
# Continuity
# ──────────────────────────────────────────────────────────────────────────
def test_continuity_penalises_isolated_burst():
    active = dict(key_presses=90, mouse_clicks=45, mouse_scrolls=10, mouse_moves=25, active_seconds=30, has_screenshot=True, app_name="X")
    idle = dict(key_presses=0, mouse_clicks=0, mouse_scrolls=0, mouse_moves=0, active_seconds=0, has_screenshot=False, app_name="Desktop")
    continuous = [WindowInput(30, **active) for _ in range(5)]
    burst = [WindowInput(30, **idle), WindowInput(30, **idle), WindowInput(30, **active),
             WindowInput(30, **idle), WindowInput(30, **idle)]
    cont_mid = score_windows(continuous, CFG)[2]
    burst_mid = score_windows(burst, CFG)[2]
    assert burst_mid.continuity_score < cont_mid.continuity_score
    assert burst_mid.score < cont_mid.score


def test_five_active_fifteen_idle_is_low():
    active = dict(key_presses=90, mouse_clicks=45, mouse_scrolls=10, mouse_moves=25, active_seconds=30, has_screenshot=True, app_name="X")
    idle = dict(key_presses=0, mouse_clicks=0, active_seconds=0, has_screenshot=False, app_name="Desktop")
    day = [WindowInput(30, **active) for _ in range(10)] + [WindowInput(30, **idle) for _ in range(30)]
    # Displayed band: idle windows floor near 65, active windows ~100, so a
    # mostly-idle day averages low-band (not the old <35 ratio).
    assert 65 <= pct(day) <= 80


# ──────────────────────────────────────────────────────────────────────────
# Screenshot correlation anomalies
# ──────────────────────────────────────────────────────────────────────────
def test_empty_screenshot_anomaly_flagged():
    w = one(key_presses=0, mouse_clicks=0, active_seconds=0)
    assert "EMPTY_SCREENSHOT_ANOMALY" in w.flags
    assert w.score < 21


def test_missing_screenshot_high_activity_flagged():
    w = score_window(WindowInput(30, key_presses=200, mouse_clicks=100, mouse_scrolls=20, mouse_moves=20,
                     active_seconds=30, has_screenshot=False, app_name="X"), CFG)
    assert "MISSING_SCREENSHOT_HIGH_ACTIVITY" in w.flags
    assert w.screenshot_score == 0.0


# ──────────────────────────────────────────────────────────────────────────
# Coverage (tracked / worked) — structural, surfaced as a flag
# ──────────────────────────────────────────────────────────────────────────
def test_coverage_caps_pct_and_flags_low_coverage():
    # 12 heavy 30s windows = 360s tracked, but worked = 1200s (8 min not tracked).
    scored = score_windows(windows(12, key_presses=90, mouse_clicks=45, mouse_scrolls=10, mouse_moves=25, active_seconds=30), CFG)
    agg = aggregate_day(scored, worked_seconds=1200, config=CFG)
    assert agg.coverage_ratio == round(360 / 1200, 4)
    # The REAL ratio still can't exceed coverage; the displayed band does not
    # encode coverage (it's keyboard+mouse), so we check raw_active_pct here.
    assert agg.raw_active_pct <= agg.coverage_ratio * 100 + 0.01
    assert "LOW_COVERAGE" in agg.flags


def test_full_coverage_no_flag():
    scored = score_windows(windows(12, key_presses=90, mouse_clicks=45, mouse_scrolls=10, mouse_moves=25, active_seconds=30), CFG)
    agg = aggregate_day(scored, worked_seconds=12 * 30, config=CFG)
    assert agg.coverage_ratio == 1.0
    assert "LOW_COVERAGE" not in agg.flags


# ──────────────────────────────────────────────────────────────────────────
# Minute model & classification
# ──────────────────────────────────────────────────────────────────────────
def test_minute_expansion_coverage_accurate():
    ws = score_windows([WindowInput(90, key_presses=300, mouse_clicks=150, mouse_scrolls=30, mouse_moves=60,
                       active_seconds=90, has_screenshot=True, app_name="X")], CFG)
    minutes = expand_minutes(ws)
    assert len(minutes) == 2
    assert abs(sum(m.active_seconds for m in minutes) - ws[0].active_credit_seconds) <= 1


def test_classify_bands():
    assert classify_score(90)[1] == CLASS_ACTIVE
    assert classify_score(70)[1] == CLASS_ACTIVE
    assert classify_score(50)[1] == "PARTIALLY_ACTIVE"
    assert classify_score(30)[1] == "LOW_ACTIVITY"
    assert classify_score(10)[1] == "IDLE"


# ──────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────
def test_config_weights_normalise():
    c = ScoringConfig(w_keyboard=6, w_mouse=4, w_focus=4, w_continuity=3, w_screenshot=2, w_app=1)
    nw = c.normalized_weights()
    assert abs(sum(nw.values()) - 1.0) < 1e-9
    assert set(nw) == {"keyboard", "mouse", "focus", "continuity", "screenshot", "app"}


def test_config_from_settings_and_caps():
    c = ScoringConfig.from_settings({
        "active_time.min_keystrokes_per_min": "200",
        "active_time.daily_cap": "150",       # clamped to 100
        "active_time.degraded_cap": "-5",     # clamped to 0
        "active_time.weight_app_engagement": "0.08",
    })
    assert c.min_keystrokes_per_min == 200
    assert c.daily_cap == 100
    assert c.degraded_cap == 0
    assert c.w_app == 0.08


def test_zero_weights_fall_back_to_defaults():
    c = ScoringConfig(w_keyboard=0, w_mouse=0, w_focus=0, w_continuity=0, w_screenshot=0, w_app=0)
    nw = c.normalized_weights()
    assert abs(sum(nw.values()) - 1.0) < 1e-9
    assert nw["keyboard"] == 0.28   # matches _DEFAULT_WEIGHTS (calibrated 2026-06-22)


def test_display_active_pct_bounds_and_spread():
    from app.active_time.engine import display_active_pct
    cfg = ScoringConfig()  # floor 65
    # Always within [floor, 100].
    for uid in range(1, 200):
        v = display_active_pct(uid, "2026-06-30", None, cfg)
        assert 65 <= v <= 100, v
    # Deterministic: same (user, day) → same value.
    assert display_active_pct(7, "2026-06-30", 80, cfg) == display_active_pct(7, "2026-06-30", 80, cfg)
    # Changes day to day for the same user.
    days = {display_active_pct(7, f"2026-07-{d:02d}", 80, cfg) for d in range(1, 28)}
    assert len(days) > 5
    # Spread across a team: produces many distinct values (the strict
    # "≤3 per number" guarantee comes from the endpoint's rank-based pass).
    from collections import Counter
    team = Counter(display_active_pct(uid, "2026-06-30", None, cfg) for uid in range(1, 41))
    assert len(team) >= 10
    # Activity bias: a high-activity band trends higher than a zero band (on avg).
    hi = sum(display_active_pct(u, "2026-06-30", 100, cfg) for u in range(1, 60)) / 59
    lo = sum(display_active_pct(u, "2026-06-30", 65, cfg) for u in range(1, 60)) / 59
    assert hi > lo


def test_empty_day_is_safe():
    agg = aggregate_day([], worked_seconds=3600, config=CFG)
    assert agg.active_pct == 0
    assert agg.idle_seconds == 3600

"""
Active Time Engine — pure scoring logic.

No DB, no clock, no I/O — every function is deterministic given its inputs, so
the whole engine is unit-testable in isolation. Persistence and wiring live in
the tracking/employees routers.

Pipeline:
    score_windows(windows, config)  →  [WindowScore]   (per screenshot interval)
    expand_minutes([WindowScore])    →  [MinuteScore]    (per minute, classified)
    aggregate_day(...)               →  DailyScore       (capped Active Time %)

Scoring of one window combines SIX sub-scores with configurable weights:
    keyboard   30% : key_presses        vs a per-minute-rate minimum (scaled by interval)
    mouse      20% : clicks+scrolls+moves vs a per-minute-rate minimum
    focus      20% : OS active_seconds / interval (within-window engagement)
    continuity 15% : neighbourhood engagement (penalises burst-then-idle)
    screenshot 10% : correlation — credit only when a screenshot exists AND at
                     least one input channel is strong (OR), not both (AND)
    app        5%  : real foreground app + interaction (idle-app < active-app)

FULLY ACTIVE = combined score >= fully_active_threshold AND (keyboard >= min OR
mouse >= min) — the OR gate, so keyboard-heavy (dev/writer) and mouse-heavy
(designer) roles are both treated fairly. Meeting BOTH minimums adds a
dual-input bonus. Meeting NEITHER caps the window at insufficient_input_cap.
When the native input hook is down, the window is scored from movement + OS
active-seconds only, flagged LIMITED_TRACKING, and capped at degraded_cap.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from .config import ScoringConfig, DEFAULT_CONFIG

# ── Window band labels (display) ──
LABEL_HIGHLY = "highly_active"
LABEL_ACTIVE = "active"
LABEL_MODERATE = "moderate"
LABEL_LOW = "low"
LABEL_INACTIVE = "inactive"
LABEL_BREAK = "break"

# ── Minute classifications (storage + spec) ──
CLASS_ACTIVE = "ACTIVE"
CLASS_PARTIAL = "PARTIALLY_ACTIVE"
CLASS_LOW = "LOW_ACTIVITY"
CLASS_IDLE = "IDLE"
CLASS_BREAK = "BREAK"

# Foreground-app names that represent NO engagement (no real app in focus).
_NON_ENGAGEMENT_APPS = {
    "", "desktop", "lockapp", "lockapp.exe", "lockscreen", "loginwindow",
    "screensaver", "screensaverengine", "idle", "unknown",
}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _is_real_app(app_name: Optional[str]) -> bool:
    if not app_name:
        return False
    return app_name.strip().lower() not in _NON_ENGAGEMENT_APPS


def classify_score(score: float) -> Tuple[str, str, float]:
    """Map a 0..100 window score to (band_label, minute_classification, weight)."""
    if score >= 81:
        return LABEL_HIGHLY, CLASS_ACTIVE, 1.0
    if score >= 61:
        return LABEL_ACTIVE, CLASS_ACTIVE, 1.0
    if score >= 41:
        return LABEL_MODERATE, CLASS_PARTIAL, 0.5
    if score >= 21:
        return LABEL_LOW, CLASS_LOW, 0.25
    return LABEL_INACTIVE, CLASS_IDLE, 0.0


# ──────────────────────────────────────────────────────────────────────────
# Inputs / outputs
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class WindowInput:
    """Raw per-interval signal, exactly as the tracker reports it."""
    interval_seconds: int
    key_presses: int = 0
    mouse_clicks: int = 0
    mouse_moves: int = 0       # number of 1-second polls where the cursor moved
    mouse_scrolls: int = 0
    active_seconds: int = 0    # OS non-idle seconds within the interval
    has_screenshot: bool = False
    hook_active: bool = True   # native global input hook loaded on the client?
    on_break: bool = False
    app_name: Optional[str] = None
    window_start: Optional[datetime] = None  # used by expand_minutes
    browser_active_ratio: Optional[float] = None  # 0..1 active/duration from BrowserActivity (if any)

    @property
    def mouse_events(self) -> int:
        return int(self.mouse_clicks) + int(self.mouse_scrolls) + int(self.mouse_moves)


@dataclass
class WindowScore:
    interval_seconds: int
    score: float                 # 0..100
    label: str
    classification: str          # minute-level class for this window
    weight: float                # nominal class weight (0..1)
    keyboard_score: float        # 0..1
    mouse_score: float           # 0..1
    focus_score: float           # 0..1
    continuity_score: float      # 0..1
    screenshot_score: float      # 0..1
    app_engagement_score: float  # 0..1
    fully_active: bool
    on_break: bool
    flags: List[str] = field(default_factory=list)
    active_credit_seconds: int = 0
    window_start: Optional[datetime] = None
    display_score: float = 0.0   # keyboard+mouse band score [floor..100] — what the UI shows


@dataclass
class MinuteScore:
    classification: str
    score: float
    active_seconds: int
    on_break: bool
    minute_start: Optional[datetime] = None


@dataclass
class DailyScore:
    worked_seconds: int
    active_seconds: int
    idle_seconds: int
    break_seconds: int
    active_pct: float            # capped (what the UI shows)
    raw_active_pct: float        # before the daily cap
    coverage_ratio: float        # tracked_seconds / worked_seconds (0..1)
    minutes_active: int = 0
    minutes_partial: int = 0
    minutes_low: int = 0
    minutes_idle: int = 0
    window_count: int = 0
    flags: List[str] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────
# Per-window sub-scores (everything except continuity, which spans the sequence)
# ──────────────────────────────────────────────────────────────────────────
def _app_engagement(w: WindowInput) -> float:
    """Real foreground app + interaction. An open-but-idle app scores well below
    an actively-used one (Chrome idle 30 min != Chrome with typing/scrolling)."""
    if not _is_real_app(w.app_name):
        return 0.0
    has_activity = (w.key_presses > 0 or w.mouse_events > 0 or w.active_seconds > 0)
    return 1.0 if has_activity else 0.4


def display_score(key_presses: int, mouse_events: int, interval_seconds: int,
                  config: ScoringConfig) -> float:
    """Per-window DISPLAYED Active Time score in [display_floor, 100].

    Keyboard + mouse ONLY (per product spec) — no focus/app/screenshot/browser.
    Each channel is mapped through a two-segment curve:
        below the per-window ``activity_knee``  → ramps 0 → knee-fraction
        at/above the knee                       → ramps knee-fraction → 1.0,
                                                   saturating at the per-minute
                                                   ``activity_full_per_min`` rate
    The knee-fraction is set so meeting BOTH minimums anchors the score at 85
    (with the default 65 floor). The two channel scores are averaged onto
    [floor, 100], so the result moves smoothly with activity and never drops
    below the floor. This is the number the dashboard shows; the real
    active/worked ratio stays in ``raw_active_pct`` for duration reports.
    """
    floor = float(config.display_floor)
    span = max(1e-9, 100.0 - floor)
    knee = max(1.0, float(config.activity_knee))
    interval_min = max(1, int(interval_seconds)) / 60.0
    top = max(knee + 1.0, float(config.activity_full_per_min) * interval_min)
    knee_frac = _clamp((85.0 - floor) / span)   # both-minimums-met anchor → 85

    def _chan(v: float) -> float:
        v = max(0.0, float(v or 0))
        if v < knee:
            return knee_frac * (v / knee)
        return knee_frac + (1.0 - knee_frac) * _clamp((v - knee) / (top - knee))

    return floor + span * (_chan(key_presses) + _chan(mouse_events)) / 2.0


def _subscores(w: WindowInput, config: ScoringConfig):
    """Return a dict of sub-scores + gate booleans + flags for one window."""
    interval = max(1, int(w.interval_seconds))
    interval_min = interval / 60.0
    flags: List[str] = []
    app = _app_engagement(w)

    # Browser sub-score — only when the service supplied a per-window ratio.
    browser = _clamp(w.browser_active_ratio) if w.browser_active_ratio is not None else None

    # ── Degraded mode: native hook down — keystrokes/clicks/scrolls unreliable ──
    if not w.hook_active:
        flags.append("LIMITED_TRACKING")
        active_density = _clamp(w.active_seconds / interval)
        movement_rate = _clamp(w.mouse_moves / interval)   # max 1 move-tick/sec
        base = 0.6 * active_density + 0.4 * movement_rate
        return dict(
            kb=0.0, mouse=movement_rate, focus=active_density, screenshot=0.0, app=app,
            browser=browser, base=base, meets_min=False, both_min=False, degraded=True, flags=flags,
        )

    required_keys = config.min_keystrokes_per_min * interval_min
    required_mouse = config.min_mouse_events_per_min * interval_min

    kb = _clamp(w.key_presses / required_keys) if required_keys > 0 else 0.0
    mouse = _clamp(w.mouse_events / required_mouse) if required_mouse > 0 else 0.0
    focus = _clamp(w.active_seconds / interval)

    meets_min = (w.key_presses >= required_keys) or (w.mouse_events >= required_mouse)
    both_min = (w.key_presses >= required_keys) and (w.mouse_events >= required_mouse)

    # Screenshot correlation — OR-based: full credit when a screenshot exists AND
    # at least one input channel is strong (so a heads-down dev/designer isn't
    # penalised). Empty screenshot or missing screenshot is flagged.
    if w.has_screenshot:
        screenshot = max(kb, mouse)
        if kb < 0.05 and mouse < 0.05:
            flags.append("EMPTY_SCREENSHOT_ANOMALY")
    else:
        screenshot = 0.0
        if kb >= 1.0 or mouse >= 1.0:
            flags.append("MISSING_SCREENSHOT_HIGH_ACTIVITY")

    # Base engagement (continuity neighbourhood input) — excludes continuity
    # itself. Browser joins the normalisation only when present, so a window
    # with no browser data scores exactly as before.
    nw = config.normalized_weights(include_browser=browser is not None)
    sub_total = nw["keyboard"] + nw["mouse"] + nw["focus"] + nw["screenshot"] + nw["app"]
    weighted = (
        nw["keyboard"] * kb + nw["mouse"] * mouse + nw["focus"] * focus
        + nw["screenshot"] * screenshot + nw["app"] * app
    )
    if browser is not None:
        sub_total += nw["browser"]
        weighted += nw["browser"] * browser
    base = 0.0 if sub_total <= 0 else weighted / sub_total

    return dict(
        kb=kb, mouse=mouse, focus=focus, screenshot=screenshot, app=app, browser=browser,
        base=base, meets_min=meets_min, both_min=both_min, degraded=False, flags=flags,
    )


def score_windows(windows: List[WindowInput], config: Optional[ScoringConfig] = None) -> List[WindowScore]:
    """Score an ordered sequence of windows (continuity uses neighbours)."""
    config = config or DEFAULT_CONFIG
    n = len(windows)
    if n == 0:
        return []

    subs = [_subscores(w, config) for w in windows]
    base_eng = [s["base"] for s in subs]

    nw = config.normalized_weights()
    nwb = config.normalized_weights(include_browser=True)  # used per-window when browser data exists
    r = max(0, int(config.continuity_neighbors))
    results: List[WindowScore] = []

    for i, w in enumerate(windows):
        s = subs[i]
        kb, mouse, focus, screenshot, app = s["kb"], s["mouse"], s["focus"], s["screenshot"], s["app"]
        browser = s.get("browser")

        if w.on_break:
            results.append(WindowScore(
                interval_seconds=int(w.interval_seconds), score=0.0, label=LABEL_BREAK,
                classification=CLASS_BREAK, weight=0.0, keyboard_score=kb, mouse_score=mouse,
                focus_score=focus, continuity_score=0.0, screenshot_score=screenshot,
                app_engagement_score=app, fully_active=False, on_break=True, flags=list(s["flags"]),
                active_credit_seconds=0, window_start=w.window_start, display_score=0.0,
            ))
            continue

        # Continuity: mean base-engagement over the neighbourhood, excluding break
        # windows (a break gap must not penalise the work around it).
        neigh = [base_eng[j] for j in range(i - r, i + r + 1)
                 if 0 <= j < n and not windows[j].on_break]
        continuity = sum(neigh) / len(neigh) if neigh else s["base"]

        if s["degraded"]:
            score = 100.0 * (0.7 * s["base"] + 0.3 * continuity)
            score = min(score, config.degraded_cap)
            fully_active = False
        elif browser is not None:
            # Browser-aware combination (weights renormalised to include browser).
            score = 100.0 * (
                nwb["keyboard"] * kb + nwb["mouse"] * mouse + nwb["focus"] * focus
                + nwb["continuity"] * continuity + nwb["screenshot"] * screenshot
                + nwb["app"] * app + nwb["browser"] * browser
            )
            if s["both_min"]:
                score *= (1.0 + config.dual_input_bonus)
            score = _clamp(score, 0.0, 100.0)
            fully_active = (score >= config.fully_active_threshold) and s["meets_min"]
        else:
            score = 100.0 * (
                nw["keyboard"] * kb + nw["mouse"] * mouse + nw["focus"] * focus
                + nw["continuity"] * continuity + nw["screenshot"] * screenshot + nw["app"] * app
            )
            # Dual-input bonus — rewards (but does not require) keyboard AND mouse.
            if s["both_min"]:
                score *= (1.0 + config.dual_input_bonus)
            score = _clamp(score, 0.0, 100.0)
            # OR gate affects only the FULLY ACTIVE *label* (combined score >=
            # threshold AND at least one input channel meets its minimum) — not a
            # hard score cap, so a heads-down dev/writer/designer isn't slammed by
            # a cliff at exactly 50. Zero-input intervals self-limit (~40 max)
            # because focus/screenshot derive from the same input.
            fully_active = (score >= config.fully_active_threshold) and s["meets_min"]

        score = _clamp(score, 0.0, 100.0)
        label, classification, weight = classify_score(score)
        active_credit = int(round((score / 100.0) * int(w.interval_seconds)))
        # DISPLAYED band score — keyboard + mouse only (independent of the
        # weighted score above, which still drives active_credit / durations).
        disp = display_score(w.key_presses, w.mouse_events, w.interval_seconds, config)

        results.append(WindowScore(
            interval_seconds=int(w.interval_seconds), score=round(score, 2), label=label,
            classification=classification, weight=weight,
            keyboard_score=round(kb, 4), mouse_score=round(mouse, 4), focus_score=round(focus, 4),
            continuity_score=round(continuity, 4), screenshot_score=round(screenshot, 4),
            app_engagement_score=round(app, 4), fully_active=fully_active, on_break=False,
            flags=list(s["flags"]), active_credit_seconds=active_credit, window_start=w.window_start,
            display_score=round(disp, 2),
        ))

    return results


def score_window(w: WindowInput, config: Optional[ScoringConfig] = None) -> WindowScore:
    """Convenience: score a single window in isolation (no neighbours)."""
    return score_windows([w], config)[0]


def display_active_pct(
    user_id, day_key: str, activity_band: Optional[float] = None,
    config: Optional[ScoringConfig] = None,
) -> int:
    """DISPLAYED Active Time the dashboard shows, in [display_floor, 100].

    Spec (product, presentation): the number must (a) never drop below the
    floor, (b) be individual — not the same value repeated across many cards,
    and (c) change day to day. A pure keyboard+mouse score can't guarantee (b)
    because many people genuinely have similar low input and collapse onto the
    same value.

    So this blends the real activity band (upward bias — busier people trend
    higher) with a DETERMINISTIC per-(user, day) component that spreads people
    across the range and breaks ties. It is deterministic: stable for the whole
    IST day, varies between people, and changes daily. The real metrics live in
    ``raw_active_pct`` / ``active_seconds`` and are never passed through this.
    """
    config = config or DEFAULT_CONFIG
    floor = float(config.display_floor)
    span = max(1e-9, 100.0 - floor)
    # Deterministic 0..1 unique per (user, day): same all day, changes daily.
    digest = hashlib.md5(f"{int(user_id or 0)}|{day_key}".encode()).hexdigest()
    jitter = (int(digest[:8], 16) % 100000) / 100000.0
    act_frac = 0.0 if activity_band is None else _clamp((float(activity_band) - floor) / span)
    blended = 0.6 * act_frac + 0.4 * jitter      # 60% real activity, 40% spread
    return int(round(floor + span * blended))


# ──────────────────────────────────────────────────────────────────────────
# Minute expansion (per-minute classification — the spec's "active minute model")
# ──────────────────────────────────────────────────────────────────────────
def expand_minutes(window_scores: List[WindowScore]) -> List[MinuteScore]:
    """Expand each scored window into its constituent minutes (coverage-accurate).

    A window's active credit is distributed across the minutes it spans (a 30s
    ACTIVE window yields one minute carrying 30 active-seconds), so summed minute
    active-seconds equal ``score% × interval`` at any interval length.
    """
    minutes: List[MinuteScore] = []
    for ws in window_scores:
        frac = ws.score / 100.0   # intensity-weighted minute active-fraction
        remaining = max(0, int(ws.interval_seconds))
        k = 0
        if remaining == 0:
            remaining = 1
        while remaining > 0:
            secs = min(60, remaining)
            mstart = (ws.window_start + timedelta(minutes=k)) if ws.window_start else None
            minutes.append(MinuteScore(
                classification=ws.classification,
                score=ws.score,
                active_seconds=0 if ws.on_break else int(round(frac * secs)),
                on_break=ws.on_break,
                minute_start=mstart,
            ))
            remaining -= secs
            k += 1
    return minutes


# ──────────────────────────────────────────────────────────────────────────
# Daily aggregation (anti-inflation cap + coverage applied here)
# ──────────────────────────────────────────────────────────────────────────
def aggregate_day(
    window_scores: List[WindowScore],
    worked_seconds: Optional[int] = None,
    config: Optional[ScoringConfig] = None,
) -> DailyScore:
    """Roll scored windows up to a daily Active Time %.

    ``worked_seconds`` is the clocked working time minus approved breaks (the
    denominator). Coverage (tracked/worked) is enforced structurally: any worked
    time not covered by a window counts as idle, so active_pct can never exceed
    the coverage ratio — no extra coverage penalty is applied (that would
    double-count). A LOW_COVERAGE flag is raised below ``coverage_min_pct``.
    """
    config = config or DEFAULT_CONFIG
    minutes = expand_minutes(window_scores)

    minute_active_seconds = sum(m.active_seconds for m in minutes if not m.on_break)

    seconds_by_class = {CLASS_ACTIVE: 0, CLASS_PARTIAL: 0, CLASS_LOW: 0, CLASS_IDLE: 0}
    for ws in window_scores:
        if ws.on_break:
            continue
        if ws.classification in seconds_by_class:
            seconds_by_class[ws.classification] += ws.interval_seconds
    counts = {k: int(round(v / 60.0)) for k, v in seconds_by_class.items()}

    tracked_seconds = sum(ws.interval_seconds for ws in window_scores if not ws.on_break)
    break_seconds = sum(ws.interval_seconds for ws in window_scores if ws.on_break)

    if worked_seconds is None:
        worked_seconds = tracked_seconds
    worked_seconds = max(0, int(worked_seconds))

    active_seconds = min(minute_active_seconds, worked_seconds)
    idle_seconds = max(0, worked_seconds - active_seconds)

    raw_active_pct = (100.0 * active_seconds / worked_seconds) if worked_seconds > 0 else 0.0
    # DISPLAYED Active Time = average of per-window keyboard+mouse band scores
    # (floor..100) — individual per person, never below the floor. raw_active_pct
    # and active_seconds stay untouched for duration reports.
    disp_scores = [ws.display_score for ws in window_scores if not ws.on_break]
    if disp_scores:
        active_pct = _clamp(sum(disp_scores) / len(disp_scores), config.display_floor, 100.0)
    else:
        active_pct = 0.0

    coverage_ratio = _clamp(tracked_seconds / worked_seconds) if worked_seconds > 0 else (1.0 if tracked_seconds else 0.0)

    flags: List[str] = []
    for ws in window_scores:
        for f in ws.flags:
            if f not in flags:
                flags.append(f)
    if worked_seconds > 0 and (coverage_ratio * 100.0) < config.coverage_min_pct:
        flags.append("LOW_COVERAGE")

    return DailyScore(
        worked_seconds=worked_seconds,
        active_seconds=active_seconds,
        idle_seconds=idle_seconds,
        break_seconds=break_seconds,
        active_pct=round(active_pct, 2),
        raw_active_pct=round(raw_active_pct, 2),
        coverage_ratio=round(coverage_ratio, 4),
        minutes_active=counts[CLASS_ACTIVE],
        minutes_partial=counts[CLASS_PARTIAL],
        minutes_low=counts[CLASS_LOW],
        minutes_idle=counts[CLASS_IDLE],
        window_count=len([ws for ws in window_scores if not ws.on_break]),
        flags=flags,
    )

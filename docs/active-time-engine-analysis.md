# Temveris — Active Time Engine: Phase 1 Codebase Analysis Report

**Status:** Analysis complete. No code changed yet.
**Scope:** End‑to‑end audit of how "Active Time %" is captured, stored, aggregated, exposed, and rendered — and why it is inaccurate.

---

## 1. Tech Stack & Architecture

| Layer | Technology | Key locations |
|---|---|---|
| Desktop tracker | **Electron `^29.4.6`** (main + remote‑web renderer) | `desktop-tracker/main.js` (2683 lines), `preload.js`, `browser-tracker.js` |
| Input capture | **`uiohook-napi` `^1.5.4`** (optional native global hook) + PowerShell/osascript foreground detection | `main.js:176-316`, `main.js:1775-1871` |
| Screenshots | Electron **`desktopCapturer`** (NOT `screenshot-desktop`, which is a stale dep) | `main.js:1920-1979` |
| Backend | **FastAPI** + **SQLAlchemy** (Supabase Postgres via PgBouncer in prod; SQLite for tests) | `backend/app/*`, `backend/database/models.py`, `backend/main.py` |
| Realtime | Native **WebSocket** `/ws?token=` (the old `socket.js` singleton was deleted and replaced by an in‑component, token‑authed, auto‑reconnecting, rAF‑batched socket) | `backend/websocket_manager.py`, `frontend/src/pages/AdminDashboard.jsx:3657-3941` |
| Frontend | **React + Vite**, Tailwind | `frontend/src/pages/AdminDashboard.jsx`, `frontend/src/components/admin/MyTeamTab.jsx`, `frontend/src/pages/Dashboard.jsx` |
| Background jobs | `asyncio.create_task` workers in FastAPI **lifespan** (no Celery/APScheduler/cron) | `backend/main.py:325-571` |

**Data flow today:**
```
Electron tracker  --POST /tracking/screenshot (every screenshot_interval)-->  Activity row (1 row = 1 tick)
                  --POST /tracking/sync (60s heartbeat)----------------------> LiveStatus (current app)
                  --GET  /tracking/ping (60s)---------------------------------> {clocked_in, on_break}
Browser ext/track --POST /browser/heartbeat, /browser/event------------------> BrowserActivity

Admin dashboard   --GET /employees/team (WS push + 2s debounce + 120s poll)--> seconds counters
                     (frontend divides active_today_seconds / worked_today_seconds = "Active %")
```

---

## 2. Current "Active Time %" Flow & Formula

**The backend never emits a percentage.** It ships two seconds counters and the **frontend divides them**. Per the code comment at `backend/app/employees/router.py:2610-2615`:

> *"The percentage is computed on the frontend as `active_today_seconds / worked_today_seconds`."*

### The numerator (`active_seconds`) — computed in **three duplicated places**
`employees/router.py:1957-1971` (`/me/day-summary`), `employees/router.py:2664-2677` + clamp at `:2794` (`/employees/team`), and `activity_log/router.py:868-886` (`/activity-log/summary`). All share this loop:

```python
tick = min(screenshot_interval, gap_to_next_row_for_same_user)   # tick length
idle_in_tick = clamp(row.idle_seconds, 0, tick)
if row.is_active:
    active_seconds += tick - idle_in_tick
active_seconds = min(active_seconds, worked_seconds)             # final clamp
```

### `is_active` is a single‑OR boolean, set at ingest — `tracking/router.py:799-811`
```python
is_active = (mouse_clicks>0 or key_presses>0 or mouse_moves>0 or mouse_scrolls>0 or active_seconds>0)
```
And idle‑within‑tick collapses to **0** on *any* evidence — `tracking/router.py:824-839`:
```python
active_in_tick = clamp(active_seconds, 0, interval)
stored_idle_seconds = interval - active_in_tick
# if ANY input evidence exists -> force active_in_tick = interval, idle = 0
```

### What is actually stored per tick (`models.Activity`, `models.py:234-274`)
`mouse_clicks`, `key_presses`, `is_active`, `idle_seconds` (idle‑within‑tick), `productivity_score` (dead), screenshot URL. **`mouse_moves`, `mouse_scrolls`, `active_seconds`, `interval_seconds` are discarded after computing `is_active`/idle.** One `Activity` row = one screenshot interval = the screenshot itself.

### The displayed "%"
Frontend: `active_today_seconds / worked_today_seconds` (team), `active_seconds / worked_seconds` (day‑summary), `totals.active_seconds / totals.idle_seconds` (activity log). Several surfaces also re‑derive Idle %, Productive % client‑side from different fields.

---

## 3. Root‑Cause Diagnosis (why the numbers are wrong)

| Symptom | Root cause | Evidence |
|---|---|---|
| **Low‑activity user shows 80–100%** | `is_active` fires on **one** mouse twitch / 1 second of OS‑idle < 3s; idle‑within‑tick is then **forced to 0**, so the reader credits the **entire tick** (up to 30 min) as fully active. No intensity/density floor exists. | `tracking/router.py:805-811`, `:830-837`; `main.js:1724-1732` (3s idle window) |
| **Some users show 100% all day** | The clamp `active = min(active, worked)`. Because nearly every tick is "fully active," raw active ≥ worked, so the clamp pins it to **exactly worked → 100%**. The clamp meant to prevent over‑credit *manufactures* perfect scores. | `employees/router.py:1971`, `:2794` |
| **Continuously‑working user shows 20–30%** | Tracker pauses (lock, network loss, status flips) and **native‑hook‑down** scenarios create rows that look idle or missing. `worked` keeps ticking on wall‑clock while `active` does not. Admin time‑adjustments add to `worked` only. | `main.js:876-877,1688-1689,2363-2366`; `main.js:2343-2347` (hook down → counts=0); `employees/router.py:1978` |
| **Break users still show high %** | By design breaks reduce *both* numerator (no rows) and denominator (subtracted), so the ratio stays frozen at whatever it was → "On Break · 95% active". Plus a stale full‑active tick can straddle the break boundary. | `employees/router.py:2610-2615`, `:2777,2789`; `tracking/router.py:830-837` |
| **Activity ≠ screenshots** | There is **no correlation** between the screenshot interval's keystroke/mouse counts and the `is_active` boolean. A screenshot with 0 keys/0 clicks can still be `is_active=True` (via `mouse_moves` or `active_seconds`) and credited a full tick. No minimum‑activity threshold exists anywhere — only **maximum** caps (12 keys/s, 10 clicks/s). | `tracking/router.py:534-535,759-772`; `is_active` rule |

### Secondary issues found
- **`interval_seconds` column is dead** — added by migration `f3b8c2a91d07` to store true tick length, but **never written and never read**. Aggregators still infer tick from gap‑to‑next‑row. (`models.py:259`, `tracking/router.py:956-973`)
- **`productivity_score` is mathematically broken & unused** — `activity_proxy / max(300, activity_proxy)` = `1.0` for any interval ≥ 300 proxy points. (`tracking/router.py:918-931`)
- **Four disagreeing idle thresholds**: 3 s (active accumulation), 3 min (tracker `is_idle`), 15 min (idle alert), 20/30 min (auto‑clockout). None reconcile.
- **Two disagreeing "Productive %"**: weighted app‑category at `app_review/router.py:408` vs unweighted browser time‑share at `summarizer.py:178`.
- **`depth_score` ignores keyboard/mouse** — `focus.py:171-176` sums keystrokes/clicks but doesn't use them; based only on browser tab data (desktop‑only users get zero focus).
- **Org‑wide team scan can't use the index** — `/employees/team` scans all users' Activity rows for the day with no `user_id` filter; the composite index is `user_id`‑leading. (`employees/router.py:2634-2648`)
- **No per‑minute / per‑window / per‑day activity aggregate tables** exist. Everything is recomputed on‑read from raw `Activity` rows. No scheduled aggregation job (intelligence materialise is on‑demand only).

---

## 4. Complete File Inventory (everything that touches Active Time)

### Backend
| Concern | File:line |
|---|---|
| Ingest, `is_active`, idle‑within‑tick, caps, dead `productivity_score`, Activity write | `backend/app/tracking/router.py` (507‑543, 759‑772, 799‑839, 918‑973) |
| Active/worked seconds loops, clamps, team payload, idle alert, tick cache | `backend/app/employees/router.py` (1821‑1841, 1844‑1995, 2001‑2071, 2610‑2891) |
| Activity‑log summary aggregation, productivity buckets, tick cache | `backend/app/activity_log/router.py` (243‑262, 842‑902, 977‑997) |
| Browser activity ingest + `active_pct` | `backend/app/browser/router.py` (157‑192, 367‑368) |
| App→category map + `CATEGORY_WEIGHT` | `backend/app/app_review/productivity.py` (266‑327) |
| Domain→category (different vocab) | `backend/app/browser/productivity.py` |
| Focus / depth_score | `backend/app/intelligence/focus.py` (27‑29, 67‑188) |
| Daily summary "productive %" (time‑share) | `backend/app/intelligence/summarizer.py` (178‑179) |
| Break governance / allowances / status | `backend/app/breaks/governance.py`, `monitor.py`, `router.py` |
| Authoritative break‑seconds recompute | `backend/app/employees/router.py:2001` |
| Settings (key/value table, `/settings`, `/settings/update`) | `backend/app/settings/router.py` (37‑147); `models.AppSettings` `models.py:727` |
| Screenshot read/serve/timelapse/retention | `backend/app/screenshots/service.py`, `router.py`; cleanup `backend/main.py:159-319` |
| DB models | `backend/database/models.py` (Activity 234, ClockSession 138, BreakSession 173, LiveStatus 283, BrowserActivity 316, FocusSession 375, DailyIntelligenceSummary 430, IntegrityScore 699, AppSettings 727) |
| Background workers (lifespan) | `backend/main.py:325-571` (break monitor 60s; no activity‑aggregation job) |

### Desktop tracker
| Concern | File:line |
|---|---|
| Global hook load/start/stop, keydown/mousedown/wheel handlers, burst cap (12/s) | `main.js:176-316` |
| Mouse‑move poll + active‑seconds (3s OS‑idle), 1s timer | `main.js:1707-1734` |
| Foreground app (PowerShell/osascript, 10s cache) | `main.js:1773-1871` |
| Screenshot capture + upload payload + retries | `main.js:1920-1979`, `2318-2488` |
| Counters + reset | `main.js:145-149`, `2343-2354`, `1743-1753` |
| Intervals (screenshot 30s, status 60s, heartbeat 60s, activity 1s, settings 5m) | `main.js:85-89` |
| Settings fetch/apply | `main.js:2031-2081` |
| Browser tracker deltas | `desktop-tracker/browser-tracker.js` |

### Frontend
| Concern | File:line |
|---|---|
| Team Directory cards (Grid + List), status→color theme | `frontend/src/components/admin/MyTeamTab.jsx` (13, 28‑42, 170‑212, 252‑363) |
| Status config / `effectiveStatus` / shift logic / `ProductivityRing` (unused) | `frontend/src/components/admin/adminShared.jsx` (16‑42, 81‑159, 264‑393) |
| Counts, filters, WebSocket, polling, MyTeamTab wiring | `frontend/src/pages/AdminDashboard.jsx` (3046, 3564‑3941, 3977‑3994, 4092‑4358, 5184‑5272, 6394) |
| Employee active/idle/productive tiles | `frontend/src/pages/Dashboard.jsx` (1098‑1143, 1874‑1888, 3328‑3373) |
| Activity Log timeline/summary | `frontend/src/components/ActivityLog.jsx` (438, 493, 558‑563) |
| Realtime config / health | `frontend/src/config.js:4-5`, `frontend/src/context/ConnectionContext.jsx` |
| i18n | `frontend/src/context/LanguageContext.jsx`, `frontend/src/components/adminTranslations.js` |

---

## 5. Key Findings That Change the Redesign

1. **The Team Directory cards do NOT currently render an Active Time %** — they show a *hours‑worked ÷ 8h45m* progress bar (`MyTeamTab.jsx:13,182-185`). A `ProductivityRing` exists in `adminShared.jsx:361-393` but is unused. The screenshots the user shared (with "77% Active Time" etc.) are the **older** design we need to restore.
2. **The screenshot interval is configurable and large in prod** — default `30s`, but settings allow up to `3600s` and prod reportedly runs long intervals. A flat "50 keystrokes per screenshot" threshold means radically different rates at 30 s vs 1800 s. **The threshold must scale with interval length** (i.e., be a per‑minute rate) to behave correctly — see Decision 1.
3. **The native global hook is an *optional* dependency** — when it fails to load, `mouse_clicks`/`mouse_scrolls`/`key_presses` are forced to `0` and only poll‑based `mouse_moves` + `active_seconds` survive. A naive 50/50 minimum would mark every such machine as 0% — see Decision 2.
4. **`active_seconds` is never persisted** — it's recomputed on every request. The redesign should **materialise** per‑window and per‑day scores (new tables) for accuracy + 1000‑worker scale.
5. **Settings plumbing has two patterns** — the validated `DEFAULTS`‑gated `/settings` router (must add keys there) and ad‑hoc `AppSettings` readers. New configurable thresholds/weights must be added to `DEFAULTS` to be editable via the canonical PUT.

---

## 6. Proposed Target Architecture (for approval)

```
                          ┌─────────────────────────────────────────────────┐
   Electron tracker       │  POST /tracking/screenshot  (per interval)        │
   (per-interval counts)  │  payload: key_presses, mouse_clicks, mouse_moves, │
        │                 │  mouse_scrolls, active_seconds, interval_seconds, │
        ▼                 │  idle_seconds, app, window, image                 │
  ┌──────────────┐        └───────────────────────┬─────────────────────────┘
  │ Raw Activity │ (1 row/interval, as today)     │
  │   rows       │◄───────────────────────────────┘
  └──────┬───────┘
         │  Activity Engine (NEW, server-side, deterministic & configurable)
         ▼
  ┌──────────────────────┐   per fixed window (= interval):
  │  activity_windows     │   - normalize counts to per-minute rates
  │  (NEW table)          │   - keyboard/mouse/continuity/screenshot/focus sub-scores
  │  score 0..100 + label │   - enforce MIN_KEYSTROKES / MIN_MOUSE (interval-scaled)
  └──────┬───────────────┘   - classify: Inactive/Low/Moderate/Active/Highly Active
         │
         ▼  minute expansion + continuity penalty
  ┌──────────────────────┐   each minute: ACTIVE / PARTIALLY_ACTIVE / LOW / IDLE
  │ activity_minute_scores│   continuity engine penalizes burst-then-idle patterns
  │  (NEW table)          │   break-aware: minutes inside a break are frozen/excluded
  └──────┬───────────────┘
         │  aggregate
         ▼
  ┌──────────────────────┐   per (user, day): active_seconds, idle_seconds,
  │ activity_daily_scores │   worked_seconds, active_pct (capped, anti-inflation),
  │  (NEW table, cached)  │   window_label histogram
  └──────┬───────────────┘
         │
         ▼  served as authoritative active_pct (single source of truth)
  GET /employees/team  ──►  WS push + cache  ──►  Team Directory cards (colored, % ring)
```

**Principles:** server‑side authoritative score (frontend stops dividing), incremental per‑window materialisation on ingest + a reconcile worker, smart caching, configurable weights + thresholds via `AppSettings`/`DEFAULTS`, break‑aware, anti‑inflation caps, screenshot‑correlated.

---

*This report is Phase 1 deliverable #1. Implementation deliverables follow below.*

---

## 7. Implementation Summary (delivered)

**Decisions applied:** per‑minute‑rate thresholds (your "50 per 30s" → `100/min`, scaled by interval); degraded scoring + `LIMITED_TRACKING` flag when the native hook is down; phased build.

### Engine (pure, configurable, deterministic) — v2, role-fair
- [app/active_time/engine.py](../backend/app/active_time/engine.py) — `score_windows` with **six** weighted sub‑scores: **keyboard 30 / mouse 20 / focus 20 / continuity 15 / screenshot 10 / app‑engagement 5**. `expand_minutes` (intensity‑weighted minute model), `aggregate_day` (anti‑inflation + coverage). Break‑aware, screenshot‑correlated, degraded‑hook mode.
- **Role-fair gate (no AND bias):** `FULLY ACTIVE = combined score ≥ 80 AND (keyboard ≥ min OR mouse ≥ min)`. Meeting **both** minimums adds a `dual_input_bonus` (×1.10). The OR replaces the old rigid `50 keyboard AND 50 mouse`, so keyboard‑heavy (dev/writer) and mouse‑heavy (designer) roles are all fully active. The minimum only gates the *label*, not the score, so there's no cliff at exactly 50.
- **App Engagement (5%):** real foreground app + interaction — an idle‑but‑open app (Chrome 30 min, no input) scores well below an actively‑used one.
- **Coverage** = `tracked_seconds / worked_seconds`, enforced structurally (untracked worked time is idle, so `active_pct ≤ coverage`); surfaced as `coverage_ratio` + a `LOW_COVERAGE` flag rather than double‑penalised.
- [app/active_time/config.py](../backend/app/active_time/config.py) — `ScoringConfig`; all 12 tunables admin‑editable via `AppSettings` (`active_time.*` keys registered in the settings router `DEFAULTS`).

### Storage (deliverable: DB changes + migration)
- `activity_windows`, `activity_minute_scores`, `activity_daily_scores` ([models.py](../backend/database/models.py)) + migration [b2f9c7a41d58](../backend/alembic/versions/b2f9c7a41d58_active_time_engine.py) (single head; **prod needs `alembic upgrade head`**).
- The previously‑dead `activities.interval_seconds` column is now populated on ingest.

### Pipeline (deliverable: tracker + API + aggregation)
- **Ingest** ([tracking/router.py](../backend/app/tracking/router.py)) — one cheap `ActivityWindow` insert per screenshot; reads new `hook_active` payload field.
- **Reconcile worker** ([app/active_time/worker.py](../backend/app/active_time/worker.py)) — 60s lifespan loop; recomputes daily scores for recently‑active users with full continuity; write‑once minute finalisation.
- **API** ([employees/router.py](../backend/app/employees/router.py)) — `/employees/team` + `/me/day-summary` serve authoritative `active_pct`. The org‑wide Activity scan is replaced by **one indexed read** of `activity_daily_scores` + two cheap aggregates (the 1000‑worker scalability fix). Legacy per‑tick fallback retained for pre‑engine days.
- **Tracker** ([desktop-tracker/main.js](../desktop-tracker/main.js)) — screenshot payload now sends `hook_active`.

### UI (deliverable: Team Directory)
- [MyTeamTab.jsx](../frontend/src/components/admin/MyTeamTab.jsx) — grid cards show prominent `active_pct` (matching the prior design); list view gains an Active Time column; `LIMITED_TRACKING` surfaced with a ⚠ + tooltip. Status→colour themes (green/amber/grey/blue/red/purple) already matched the spec.

### Tests (deliverable: automated tests)
- [test_active_time_engine.py](../backend/tests/test_active_time_engine.py) — 22 tests: all 9 product cases + interval‑scaling, degraded hook, continuity, anomaly flags, config edges.
- [test_active_time_service.py](../backend/tests/test_active_time_service.py) — 9 tests: materialisation, recompute, break exclusion, daily map, worker, write‑once minutes.
- Full backend suite green except 16 pre‑existing commercial `qa_matrix` failures (unrelated).


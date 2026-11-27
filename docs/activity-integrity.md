# Activity Integrity (Anti-Fake-Activity Detection)

Detects artificially-generated activity (mouse jigglers, auto-clickers, macro
tools) and reports a risk score to admins. **Detection only — nothing is blocked
or terminated**, and productivity is *not* auto-suppressed (that decision waits
for the deterministic injected-input detector; see "Deferred").

## What ships today (Lean MVP)

| Signal | Where | Notes |
|--------|-------|-------|
| **Known-automation process scan** | `desktop-tracker/main.js` `scanAutomationProcesses()` | Enumerates running processes once/minute, matches an admin-configurable watch-list. Deterministic, ~zero false positives. Only matched names leave the machine. |
| **Static-screen correlation** | `backend/app/integrity/detector.py` `dhash()` | Perceptual (difference) hash per screenshot; flags a screen that stays unchanged across N *active* frames. Uses Pillow+numpy (no new dep). |
| **Mouse-only interval** | detector | Movement but no clicks/keys/scroll. Weak signal — weighted so it alone stays in the *Normal* band (false-positive guard). |

## Data flow

```
tracker  →  POST /tracking/screenshot  →  (bg task) detector.record_interval
   automation_processes[]                    ├─ evaluate() → risk 0..100 + reasons
   tracker_version, os_version               └─ persist ActivityIntegrityEvent
                                                 (ONLY when risk >= report_min_score)
admin  →  GET /integrity/overview  →  Security → Activity Integrity tab
```

`activity_integrity_events` is **sparse**: a row exists only when an interval was
flagged, so "no rows" means clean and the dashboard stays cheap (Phase 13).

## Risk bands

`0–20 Normal · 21–40 Low · 41–60 Medium · 61–80 High · 81–100 Very High`

Default weights (all admin-configurable): known-automation **45** (one hit ⇒
Medium), static-screen **15**, mouse-only **10**, injected-input **30**
(inactive until the native detector ships).

## Configuration

All thresholds are AppSettings keys (`integrity.*`) editable via
`PUT /settings/update`, resolved through the org→team→employee cascade like every
other tracker setting. See `backend/app/integrity/config.py`. The tracker reads
`integrity.enabled` and `integrity.automation_processes` from `GET /settings`.

## Privacy (Phase 14)

Only *matched* automation process names are uploaded — never the full process
list, keystroke values, clipboard, or file contents. The full enumeration is
filtered locally on the client.

## Deferred (config stubs present, weights = enable later)

- **Injected-input detection** (`LLMHF_INJECTED` / Raw Input) — the single best
  deterministic signal, but `uiohook-napi` discards the injected flag. Needs a
  native C++ N-API low-level-hook addon (node-gyp + per-Electron-ABI prebuilds +
  signing). `integrity.weight_injected_input` and the `injected_ratio` path are
  already wired for when it lands.
- **Fuzzy behavioural heuristics** (perfect circles, constant acceleration,
  timing entropy) — deliberately omitted: highest false-positive risk, largely
  redundant once process-scan + injected-flag exist.

## Tests

`backend/tests/test_integrity_detector.py` — scoring bands, mouse-only
false-positive guard, static-screen streak threshold, dHash determinism.
Run: `python -m pytest tests/test_integrity_detector.py`.

## Deploy step

Run `alembic upgrade head` to create `activity_integrity_events`
(migration `a7c1d4e8f2b9`, idempotent `CREATE TABLE IF NOT EXISTS`).

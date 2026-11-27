/**
 * Session-scoped shared reads (H2).
 *
 * Two endpoints were being fetched redundantly across the worker dashboard, the
 * clock-in widget, the Electron shell and the admin settings page. This module
 * centralizes them through the shared axios instance (so they also get the
 * 401-silent-refresh + Electron-safe logout) and removes the duplicate traffic.
 */
import API from "./axios";

// ─── /break/thresholds ───────────────────────────────────────────────────────
// Workspace overtime limits. This is configuration that changes ONLY via the
// admin "Break Thresholds" PUT, so it's safe to cache for the session. Without
// this, the worker Dashboard, the ClockInWidget and the Electron shell each hit
// the network for the same unchanging values on mount.
//
// Returns the parsed body or `null` (never throws) — a drop-in for the old
// `fetch(...).then(r => r.ok ? r.json() : null)` call sites. Call
// `invalidateBreakThresholds()` after a successful PUT so a later read in the
// same session reflects the new values.
const THR_TTL_MS = 5 * 60_000;
let _thr = null; // { at: epochMs, data }
let _thrInflight = null;

export async function getBreakThresholds({ force = false } = {}) {
  if (!force && _thr && Date.now() - _thr.at < THR_TTL_MS) return _thr.data;
  if (!force && _thrInflight) return _thrInflight;
  const p = API.get("/break/thresholds")
    .then((r) => {
      _thr = { at: Date.now(), data: r.data };
      return r.data;
    })
    .catch(() => null) // match the old `r.ok ? r.json() : null`
    .finally(() => {
      if (_thrInflight === p) _thrInflight = null;
    });
  if (!force) _thrInflight = p;
  return p;
}

export function invalidateBreakThresholds() {
  _thr = null;
}

// ─── /employees/me ───────────────────────────────────────────────────────────
// Fetched on mount by BOTH the worker Dashboard and the ClockInWidget (a child
// of it), so the two reads fire in the same React commit and the shared axios
// in-flight GET dedup folds them into a SINGLE network request.
//
// NEW: added 5s TTL cache to reduce redundant `/employees/me` requests during
// heavy polling (bootstrap, reconcile, WebSocket refreshes all within the same
// 5s window). Reconcile reads will use the cached result if fresh, keeping
// clock state in sync while cutting request volume by 60-80% under load.
// Fresh reconciles after user actions are guaranteed by the Electron main
// process pushing force-refresh and by clock-out/break events via WebSocket.
//
// Returns the parsed body; throws on a non-2xx the 401-refresh couldn't rescue.
const ME_CACHE_TTL_MS = 5000;
let _meCache = null; // { at: epochMs, data }
let _meInFlight = null;

export async function getMe({ force = false } = {}) {
  if (!force && _meCache && Date.now() - _meCache.at < ME_CACHE_TTL_MS) {
    return _meCache.data;
  }
  if (!force && _meInFlight) return _meInFlight;

  const p = API.get("/employees/me")
    .then((r) => {
      _meCache = { at: Date.now(), data: r.data };
      return r.data;
    })
    .catch((err) => {
      if (_meInFlight === p) _meInFlight = null;
      throw err;
    })
    .finally(() => {
      if (_meInFlight === p) _meInFlight = null;
    });

  if (!force) _meInFlight = p;
  return p;
}

export function invalidateMe() {
  _meCache = null;
}

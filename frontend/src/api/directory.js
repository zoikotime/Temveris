/**
 * Directory — thin wrapper around `/employees` for the offboarding UI.
 *
 * Every workforce-facing surface in the offboarding module needs to resolve a
 * worker_id back to a real person (name, email, department). Hitting the
 * employees endpoint per-row would be wasteful, so we cache the full directory
 * for the session and expose a tiny lookup helper.
 *
 * The cache TTL is short (60 s) because admins routinely invite / deactivate
 * during the same session — stale enough to be useful, fresh enough that a new
 * hire shows up almost immediately.
 */
import API from "./axios";

const TTL_MS = 60_000;
let _cache = null;             // { at: epochMs, list: Employee[] }
let _inflight = null;          // dedup concurrent loads

/**
 * Fetch the workforce directory once and cache it for the session.
 * Returns the raw rows shipped by `GET /employees`.
 */
export async function listEmployees({ force = false } = {}) {
  const fresh = _cache && Date.now() - _cache.at < TTL_MS;
  if (!force && fresh) return _cache.list;
  if (_inflight) return _inflight;

  _inflight = API.get("/employees")
    .then((r) => {
      const list = Array.isArray(r.data) ? r.data : r.data?.items || [];
      _cache = { at: Date.now(), list };
      return list;
    })
    .finally(() => { _inflight = null; });
  return _inflight;
}

/** Force-refresh the cached directory (e.g. after an invite). */
export const refreshDirectory = () => listEmployees({ force: true });

/**
 * Build a fast `id → employee` lookup from a previously-fetched list. Pass the
 * list rather than re-reading the cache so React components can sync the map
 * with their state and re-render on refresh.
 */
export function buildEmployeeMap(list) {
  const map = new Map();
  (list || []).forEach((e) => {
    if (e?.id != null) map.set(Number(e.id), e);
  });
  return map;
}

/** Distinct department list, sorted, empty values dropped — used by filters. */
export function distinctDepartments(list) {
  const set = new Set();
  (list || []).forEach((e) => {
    const d = (e?.department || "").trim();
    if (d) set.add(d);
  });
  return Array.from(set).sort((a, b) => a.localeCompare(b));
}

/**
 * Display label for a worker. Falls back gracefully when:
 *   • the directory hasn't loaded yet,                    → "Worker #123"
 *   • the worker_id is null,                              → "—"
 *   • the worker has been deleted from the directory.     → "Worker #123 (removed)"
 */
export function workerLabel(workerId, map) {
  if (workerId == null) return "—";
  const emp = map?.get(Number(workerId));
  if (!emp) return `Worker #${workerId}`;
  return emp.name || emp.email || `Worker #${workerId}`;
}

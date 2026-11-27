/**
 * ECO v2 day read — client for GET /api/eco/day.
 *
 * WHAT THIS IS A CLIENT FOR
 * -------------------------
 * The backend answers with an ALREADY-RENDERED day: `status` is the locked
 * §28.1 Status label, `body` is the approved explanation where one exists.
 * Neither is computed here, and neither may be. This module transports; the
 * backend decides.
 *
 * WHY 404 IS DELIBERATELY UNINTERPRETABLE
 * ---------------------------------------
 * The endpoint returns 404 for three different causes on purpose:
 *
 *   * the ECO v2 rollout is OFF for the tenant,
 *   * the rollout is in SHADOW, where §44.2 forbids a Worker-facing surface,
 *   * no ECO day record exists for that date.
 *
 * They are indistinguishable BY DESIGN — telling them apart would disclose the
 * tenant's rollout state and whether a record exists. So this module maps all
 * three to one `UNAVAILABLE` outcome and never issues a second request to
 * narrow it down. A caller that wants to know which cause applied is asking a
 * question the product declines to answer.
 *
 * Mirrors the typed-error shape `api/holidays.js` already uses, and rides
 * `authFetch` so a 401 refreshes and replays instead of surfacing as a failure.
 */
import { API_BASE } from "../config";
import { authFetch } from "./authFetch";
import { readMirror } from "./authStorage";

/** Outcomes a caller may branch on. Nothing here names a rollout state. */
export const ECO_DAY_ERROR = {
  /** 404 — no working-time record is available for that day. One outcome for
   *  all three backend causes; see the module docstring. */
  UNAVAILABLE: "UNAVAILABLE",
  /** 403 — the caller may not read this record. The backend deliberately does
   *  not say why, and neither does this. */
  FORBIDDEN: "FORBIDDEN",
  /** The signed-in user could not be identified locally. */
  NO_IDENTITY: "NO_IDENTITY",
  /** Network failure, timeout, 5xx, or an unrecognised response. */
  UNKNOWN: "UNKNOWN",
};

export class EcoDayApiError extends Error {
  constructor(kind, status = null) {
    super(kind);
    this.name = "EcoDayApiError";
    this.kind = kind;
    this.status = status;
  }
}

/**
 * Today's date as YYYY-MM-DD in the BROWSER'S LOCAL calendar.
 *
 * Deliberately not `toISOString().slice(0, 10)`, which is UTC: east of
 * Greenwich that returns tomorrow's date late in the evening, and west of it
 * yesterday's early in the morning — so a Worker would open the page and be
 * shown the wrong day. Same construction `DashboardSummaryView` and
 * `LeaveSummaryModule` already use.
 */
export const todayLocalISO = (d = new Date()) => {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
};

/** The signed-in Worker's own id. The page never reads anybody else's day. */
export const currentWorkerId = () => {
  const raw = readMirror("user_id");
  const id = Number(raw);
  return raw && Number.isFinite(id) && id > 0 ? id : null;
};

/**
 * Fetch one day for the SIGNED-IN Worker.
 *
 * Takes no user id: the id comes from the session, so no caller can ask for
 * somebody else's day. (The backend refuses that anyway — this just removes
 * the parameter that would tempt anyone to try.) Sends no `organization_id`
 * and no `audience`: the tenant comes from the caller's token and the audience
 * is derived server-side from the authority decision.
 *
 * Resolves to the response body, or throws `EcoDayApiError`.
 */
export async function fetchMyEcoDay(workDate) {
  const userId = currentWorkerId();
  if (!userId) throw new EcoDayApiError(ECO_DAY_ERROR.NO_IDENTITY);

  const query = new URLSearchParams({
    user_id: String(userId),
    work_date: workDate,
  });

  let res;
  try {
    res = await authFetch(`${API_BASE}/api/eco/day?${query.toString()}`);
  } catch {
    // authFetch already retried transient failures and refreshed a 401.
    throw new EcoDayApiError(ECO_DAY_ERROR.UNKNOWN);
  }

  if (res.ok) return res.json();
  if (res.status === 404) throw new EcoDayApiError(ECO_DAY_ERROR.UNAVAILABLE, 404);
  if (res.status === 403) throw new EcoDayApiError(ECO_DAY_ERROR.FORBIDDEN, 403);
  throw new EcoDayApiError(ECO_DAY_ERROR.UNKNOWN, res.status);
}

export default fetchMyEcoDay;

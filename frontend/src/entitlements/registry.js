/**
 * Phase 3.7 — sidebar feature-key registry (SPEC.txt §7.1).
 *
 * The single, documented place that maps a navigation item to its backend
 * capability key and turns a backend presentation into render decisions. There
 * is no entitlement logic here: the backend `/api/v1/entitlements/projection`
 * is authoritative for *which* presentation a capability gets. This module only
 * maps nav → key and presentation → "do I render / lock / read-only / setup".
 *
 * IMPORTANT
 *  - Only the authoritative 39 backend capability keys appear here. No
 *    frontend-only capability names, no plan logic, no role logic — the backend
 *    projection already accounts for plan, lifecycle, jurisdiction and the
 *    actor's role.
 *  - LOCK vs HIDE is decided by the backend (via `is_upsellable` + role), never
 *    here. This registry does not encode any Product policy.
 */

// Presentation values, mirrored from the backend vocabulary. Kept as a frozen
// map so a typo fails loudly rather than silently.
export const PRESENTATION = Object.freeze({
  SHOW: "show",
  LOCK: "lock",
  HIDE: "hide",
  READ_ONLY: "read_only",
  CONFIGURATION_REQUIRED: "configuration_required",
});

/**
 * Navigation item key (NAV_ITEMS[].k) → backend capability key.
 *
 * Items with no meaningful commercial capability (account/profile/download)
 * are intentionally absent: an unmapped item always renders, which preserves
 * today's behaviour for universal navigation.
 *
 * Screenshots maps to the *viewer* capability (evidence.screenshots.view), not
 * capture — the sidebar opens the viewer, which is available on every plan.
 * The capture/masking CONFIGURATION_REQUIRED controls live inside that tab and
 * are out of scope for the sidebar.
 */
export const NAV_CAPABILITY = Object.freeze({
  overview:          "dashboard.overview.view",
  myteam:            "team.directory.view",
  screenshots:       "evidence.screenshots.view",
  timesheets:        "time.attendance.view",
  timelapse:         "time.playback.basic",
  reports:           "reports.standard",
  offboarding:       "offboarding.standard",
  activitylog:       "activity.log.view",
  appshistory:       "productivity.apps_history",
  browsercategories: "productivity.website_categories",
  manageleave:       "leave.manage",
  leavesummary:      "leave.manage",
  manageholiday:     "holidays.primary",
  payroll:           "payroll.calculator",
  billing:           "billing.portal",
  settings:          "workspace.settings.core",
});

/**
 * The backend presentation for a nav item, or null when the item is not mapped
 * to a capability (universal item) or the projection has not resolved a value.
 *
 * @param {string} navKey        NAV_ITEMS[].k
 * @param {object|null} projection  { [capabilityKey]: presentation }
 */
export function presentationForNav(navKey, projection) {
  const cap = NAV_CAPABILITY[navKey];
  if (!cap) return null;                 // universal item — always rendered
  if (!projection) return null;          // not loaded — caller applies fallback
  return projection[cap] || null;
}

/**
 * Whether a nav item should be rendered at all.
 *
 * Fallback (unmapped item, or projection not yet available) renders the item —
 * this equals pre-3.7 behaviour and is safe because the backend does not
 * enforce entitlements, so a shown item can never grant access that the server
 * would otherwise deny. Only an explicit HIDE from the backend removes an item.
 */
export function shouldRenderNav(navKey, projection) {
  return presentationForNav(navKey, projection) !== PRESENTATION.HIDE;
}

export function isLocked(navKey, projection) {
  return presentationForNav(navKey, projection) === PRESENTATION.LOCK;
}

export function isReadOnly(navKey, projection) {
  return presentationForNav(navKey, projection) === PRESENTATION.READ_ONLY;
}

/**
 * CONFIGURATION_REQUIRED is a setup state, never an upsell. Callers must render
 * a configuration affordance for this — not an upgrade prompt.
 */
export function needsConfiguration(navKey, projection) {
  return presentationForNav(navKey, projection) === PRESENTATION.CONFIGURATION_REQUIRED;
}

/**
 * Build a fast lookup from a raw projection response.
 * Accepts the endpoint shape { capabilities: [{ capability, presentation }] }
 * and returns { [capability]: presentation }.
 */
export function indexProjection(response) {
  const out = {};
  const rows = response && Array.isArray(response.capabilities) ? response.capabilities : [];
  for (const row of rows) {
    if (row && row.capability) out[row.capability] = row.presentation;
  }
  return out;
}

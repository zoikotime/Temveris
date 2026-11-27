/**
 * Pure view logic for the Manage Holidays page.
 *
 * Extracted from AdminDashboard so the rules that were actually broken can be
 * tested without mounting a 7000-line component behind six context providers:
 *
 *   * merging the organization's own holidays with the read-only catalog
 *     preview (the preview used to REPLACE them, so a holiday an admin had just
 *     added became invisible the moment they picked a country);
 *   * deciding what the page renders — the old error branch short-circuited the
 *     whole view, so one unsupported country hid the organization's holidays and
 *     froze the Table/Calendar toggle until a full page reload.
 */

/** A stable identity for a holiday: two distinct holidays may share a date. */
export function holidayKey(holiday) {
  return `${holiday?.date || ""}|${(holiday?.name || "").toLowerCase()}`;
}

/** Adoption statuses. Mirrors app/holidays/collisions.py. */
export const ADOPT_STATUS = {
  NEW: "new",
  ALREADY_COVERED: "already_covered",
  EXACT_DUPLICATE: "exact_duplicate",
  DATE_CONFLICT: "date_conflict",
};

/** Statuses that are already handled and need nothing from the admin. */
const SETTLED = [ADOPT_STATUS.ALREADY_COVERED, ADOPT_STATUS.EXACT_DUPLICATE];

/**
 * Merge stored holidays with a SERVER-CLASSIFIED catalog plan.
 *
 * `planEntries` come from `GET /holidays/catalog/plan`, each carrying a
 * `status`. This function no longer decides what is already present — it used
 * to, by de-duplicating on date+name, and it was wrong: the server matched on
 * (country, date, name), every legacy holiday has a NULL country, so the two
 * disagreed and "13 available to add" actually wrote 20 rows. One source of
 * truth now, and it is the one that does the writing.
 *
 * Returns:
 *   rows       — what the table and calendar render: the organization's own
 *                rows, then catalog entries not already present (`_readonly`)
 *   addable    — genuinely new AND eligible; the "Add all" set
 *   conflicts  — need an explicit admin decision
 *   covered    — already present under this or another name (shown, not added)
 *   blocked    — new but filtered out by the optional/regional toggles
 */
export function mergeHolidayRows(orgRows = [], planEntries = []) {
  const own = Array.isArray(orgRows) ? orgRows : [];
  const plan = Array.isArray(planEntries) ? planEntries : [];

  const decorated = plan.map((entry) => ({
    ...entry,
    id: entry.key || `catalog-${entry.date}-${entry.name}`,
    _readonly: true,
  }));

  const addable = decorated.filter(
    (e) => e.status === ADOPT_STATUS.NEW && e.eligible !== false);
  const blocked = decorated.filter(
    (e) => e.status === ADOPT_STATUS.NEW && e.eligible === false);
  const conflicts = decorated.filter((e) => e.status === ADOPT_STATUS.DATE_CONFLICT);
  const covered = decorated.filter((e) => SETTLED.includes(e.status));

  // Entries the organization already has are NOT re-listed in the table — its
  // own row is already there, with real Edit/Delete actions.
  const preview = [...addable, ...blocked, ...conflicts];

  return { rows: [...own, ...preview], preview, addable, blocked, conflicts, covered };
}

/**
 * Header counts, straight from the plan. Never derived in the browser, so the
 * number on screen is the number the server will act on.
 */
export function adoptionCounts(plan) {
  const c = (plan && plan.counts) || {};
  return {
    new: c.will_add ?? 0,
    alreadyCovered: c.already_covered ?? 0,
    exactDuplicates: c.exact_duplicate ?? 0,
    conflicts: c.date_conflict ?? 0,
    notEligible: c.not_eligible ?? 0,
    total: c.total ?? 0,
  };
}

/** Per-row label and whether the row offers an action. */
export function adoptRowState(entry) {
  if (!entry || !entry._readonly) return { label: null, action: null };
  if (entry.status === ADOPT_STATUS.DATE_CONFLICT) {
    return { label: "Review conflict", action: "review", tone: "warn" };
  }
  if (entry.status === ADOPT_STATUS.EXACT_DUPLICATE
      || entry.status === ADOPT_STATUS.ALREADY_COVERED) {
    return { label: "Already covered", action: null, tone: "muted" };
  }
  if (entry.eligible === false) {
    return { label: "Not included", action: null, tone: "muted" };
  }
  return { label: "Add", action: "add", tone: "brand" };
}

/**
 * What the page should render.
 *
 * `error` is intentionally NOT a terminal state. It becomes a banner ABOVE the
 * content, so the organization's own holidays stay on screen and the view
 * toggle keeps working while a country lookup is failing.
 */
export function holidayPageState({ loading, error, rows = [] }) {
  if (loading) return { mode: "loading", showBanner: false, showContent: false };
  return {
    mode: rows.length === 0 ? "empty" : "content",
    // The banner and the content coexist — this is the fix for the error trap.
    showBanner: !!error,
    showContent: true,
  };
}

/**
 * The state changes a country/year switch must make BEFORE the new request.
 *
 * The bug this encodes: the previous code cleared `holidayCountryError` only on
 * its own success path, and the clear-country path returned before reaching it.
 * A failed lookup therefore survived Clear, the view toggle, and even selecting
 * a country that worked.
 */
export function selectionResetPatch({ countryCode, year }) {
  return {
    holidayCountryError: null,
    holidayCatalogInfo: null,
    holidayCountryRows: [],
    // A country/year change always re-reads both sides of the page.
    shouldFetchOrgHolidays: true,
    shouldFetchCatalog: !!countryCode && !!year,
  };
}

/**
 * Does this error justify offering a Retry button?
 * Only transient ones — retrying an unsupported country would never succeed and
 * offering it would be a lie.
 */
export function isRetryableHolidayError(kind) {
  return kind === "provider_unavailable" || kind === "network_error";
}

/**
 * Upcoming holidays for a summary card: on/after `todayIso`, soonest first.
 * Deduplicated on date AND name so two holidays sharing a date both survive.
 */
export function upcomingHolidays(rows = [], todayIso, limit = 6) {
  const byKey = new Map();
  (Array.isArray(rows) ? rows : []).forEach((h) => {
    if (h?.date) byKey.set(holidayKey(h), h);
  });
  return [...byKey.values()]
    .filter((h) => (h.date || "") >= todayIso)
    .sort((a, b) => (a.date || "").localeCompare(b.date || ""))
    .slice(0, limit);
}

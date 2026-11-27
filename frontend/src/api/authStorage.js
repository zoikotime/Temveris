/**
 * Single source of truth for where the auth token lives.
 *
 * "Remember this device" on the login page picks the storage tier:
 *   • localStorage   → survives browser close (30-day JWT, default)
 *   • sessionStorage → cleared when the last tab/window closes
 *
 * Every read checks both tiers so old sessions still resolve while the user
 * is migrating. Writes go to whichever tier matches the `remember` flag.
 *
 * Mirror data (name / email / role) lives in the same tier as the token —
 * this is what PrivateRoute and the legacy components read from.
 */
const TOKEN_KEYS  = ["zt_token", "token"];
const MIRROR_KEYS = ["name", "email", "role", "user_name", "user_email", "profile_picture", "user_id", "organization_id"];
const REMEMBER_FLAG_KEY = "zt_remember";

const safeLocal = () => {
  try { return typeof localStorage !== "undefined" ? localStorage : null; } catch { return null; }
};
const safeSession = () => {
  try { return typeof sessionStorage !== "undefined" ? sessionStorage : null; } catch { return null; }
};

export const readToken = () => {
  const ls = safeLocal(), ss = safeSession();
  for (const k of TOKEN_KEYS) {
    const v = (ls && ls.getItem(k)) || (ss && ss.getItem(k));
    if (v) return v;
  }
  return null;
};

const _isRemembered = () => {
  const ls = safeLocal();
  return ls ? ls.getItem(REMEMBER_FLAG_KEY) !== "0" : true;
};

// Public accessor — refresh.js needs to know which tier to persist a rotated
// access token into so it lands in the same place as the original login.
export const isRemembered = () => _isRemembered();

const _activeStorage = () => (_isRemembered() ? safeLocal() : safeSession()) || safeLocal();

export const writeToken = (token, { remember = true } = {}) => {
  const target = remember ? safeLocal() : safeSession();
  const other  = remember ? safeSession() : safeLocal();
  if (target) {
    for (const k of TOKEN_KEYS) target.setItem(k, token);
  }
  // Wipe the same keys from the other tier so two stale tokens can't coexist.
  if (other) {
    for (const k of TOKEN_KEYS) other.removeItem(k);
  }
  // Persist the remember choice itself in localStorage so subsequent writes
  // (e.g. mirrorUserToStorage) target the right tier even across reloads.
  try { safeLocal()?.setItem(REMEMBER_FLAG_KEY, remember ? "1" : "0"); } catch { /* private-mode browsers throw on storage access — ignore */ }
  // A session is (re)starting — clear the in-flight logout guard. Desktop never
  // reloads the JS context between sign-out and the next sign-in, so without
  // this the guard would stay latched and block a legitimate second logout.
  if (typeof window !== "undefined") window.__ztLoggingOut = false;
};

export const clearAuthStorage = () => {
  for (const store of [safeLocal(), safeSession()]) {
    if (!store) continue;
    for (const k of [...TOKEN_KEYS, ...MIRROR_KEYS]) {
      try { store.removeItem(k); } catch { /* private-mode browsers throw on storage access — ignore */ }
    }
  }
  try { safeLocal()?.removeItem(REMEMBER_FLAG_KEY); } catch { /* private-mode browsers throw on storage access — ignore */ }
  // Clear workspace settings cache so a different org's user logging in on the
  // same tab never sees the previous org's logo or settings from stale cache.
  try { safeSession()?.removeItem("zt_workspace_settings"); } catch {}
};

export const mirrorUserToStorage = (u) => {
  if (!u) return;
  const store = _activeStorage();
  if (!store) return;
  try {
    if (u.id)              store.setItem("user_id", String(u.id));
    if (u.name)            store.setItem("name", u.name);
    if (u.email)           store.setItem("email", u.email);
    if (u.role)            store.setItem("role", String(u.role).toLowerCase());
    if (u.profile_picture) store.setItem("profile_picture", u.profile_picture);
    else if (u.profile_picture === null) store.removeItem("profile_picture");
    // organization_id is null for super_admin — remove the key in that case so
    // stale values from a previous session don't linger across role switches.
    if (u.organization_id != null) store.setItem("organization_id", String(u.organization_id));
    else if ("organization_id" in u) store.removeItem("organization_id");
  } catch { /* private-mode browsers throw on storage access — ignore */ }
};

export const readMirror = (key) => {
  const ls = safeLocal(), ss = safeSession();
  return (ls && ls.getItem(key)) || (ss && ss.getItem(key)) || null;
};

export const TOKEN_STORAGE_KEYS = TOKEN_KEYS;

/**
 * The ONE place that decides how a logged-out renderer reaches the login screen.
 *
 * Every non-router logout path (the axios 401 interceptor, AuthContext.logout)
 * must call this instead of touching window.location itself — otherwise two
 * paths each fire their own hard navigation and race, flashing the window white.
 *
 * Web  → clear storage, then a single hard nav to "/".
 * Desktop → the Electron MAIN process owns navigation. We hand off to it and do
 *   NOT hard-navigate or clearAuthStorage here: a renderer window.location change
 *   reloads the remote SPA (white screen), and clearing storage trips main's
 *   "localStorage emptied → logout" watcher — both are the glitch we removed.
 *   Main revokes, wipes the encrypted session, clears localStorage, and drives
 *   the window to /login exactly once.
 *
 * NOTE: the smooth in-document path (ElectronDashboard's Logout button) does its
 * own React-Router navigate() and calls electronAuth.logout({ rendererNavigates:
 * true }) so main skips the reload. That is the only path that should NOT use
 * this helper, because only it holds a router.
 */
export const redirectToLogin = ({ notice } = {}) => {
  if (typeof window === "undefined") return;
  if (!beginLogout()) return;
  if (window.isDesktopApp === true) {
    try { window.electronAuth?.logout?.(); } catch { /* ignore */ }
    return;
  }
  clearAuthStorage();
  // Set AFTER clearAuthStorage, which wipes the auth keys from both stores —
  // otherwise the reason the user was signed out is erased before they can read
  // it, and an organization suspension looks like a random session drop.
  if (notice) setLogoutNotice(notice);
  if (window.location?.pathname !== "/") window.location.assign("/");
};

// ─── Logout notice ───────────────────────────────────────────────────────────
// A one-shot message explaining WHY the session ended, survives the hard nav to
// "/" and is consumed once by the login page. sessionStorage (not local) so it
// dies with the tab and can never resurface in a later session.
const LOGOUT_NOTICE_KEY = "zt_logout_notice";

export const setLogoutNotice = (message) => {
  try { safeSession()?.setItem(LOGOUT_NOTICE_KEY, String(message)); } catch { /* private mode */ }
};

/** Read and clear the pending logout notice. Returns "" when there is none. */
export const takeLogoutNotice = () => {
  try {
    const store = safeSession();
    const msg = store?.getItem(LOGOUT_NOTICE_KEY) || "";
    if (msg) store.removeItem(LOGOUT_NOTICE_KEY);
    return msg;
  } catch { return ""; }
};

/**
 * Idempotency latch for the whole logout flow. The first caller wins and gets
 * `true`; any concurrent/duplicate caller gets `false` (and, in dev, a warning)
 * so it bails instead of firing a second teardown + navigation that would race
 * the first. Cleared on the next login (see writeToken).
 *
 * The smooth desktop path (ElectronDashboard) and the shared redirectToLogin()
 * both gate on this, so no two logout paths can ever navigate at once.
 */
export const beginLogout = () => {
  if (typeof window === "undefined") return true;
  if (window.__ztLoggingOut) {
    // eslint-disable-next-line no-undef
    if (typeof import.meta !== "undefined" && import.meta.env?.DEV) {
      console.warn("[auth] logout already in progress — ignoring duplicate invocation.");
    }
    return false;
  }
  window.__ztLoggingOut = true;
  return true;
};

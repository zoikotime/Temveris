/**
 * Temveris — Workspace Settings Context
 * =======================================
 * Single source of truth for tenant workspace settings (name, logo,
 * contact emails, branding colors). Loaded once after login and cached
 * in sessionStorage so page refreshes don't flash empty state.
 *
 * Pattern mirrors ThemeContext / AuthContext exactly:
 *   createContext → Provider → useCallback/useMemo → export hook
 *
 *   const { workspace, workspaceName, workspaceLogo, refresh } = useWorkspace();
 *
 * Tenant isolation: every fetch uses the JWT so the backend always
 * scopes the response to current_user.organization_id. Org A can
 * never receive Org B's settings through this context.
 *
 * Skips fetch for super_admin (no org) and unauthenticated users.
 */
import React, {
  createContext, useCallback, useContext,
  useEffect, useMemo, useRef, useState,
} from "react";
import { API_BASE } from "../config";
import { readToken } from "../api/authStorage";
import { authFetch } from "../api/authFetch";
import { useAuth } from "./AuthContext";
import { setOrgTimeZone } from "../lib/orgTime";

// ── Session cache ────────────────────────────────────────────────────────────
const WS_CACHE_KEY = "zt_workspace_settings";

const readCached = () => {
  try {
    const raw = sessionStorage.getItem(WS_CACHE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
};

const writeCached = (data) => {
  try { sessionStorage.setItem(WS_CACHE_KEY, JSON.stringify(data)); } catch {}
};

const clearCached = () => {
  try { sessionStorage.removeItem(WS_CACHE_KEY); } catch {}
};

// ── Context ──────────────────────────────────────────────────────────────────
const WorkspaceContext = createContext({
  workspace:            null,
  loading:              false,
  loadFailed:           false,
  refresh:              () => {},
  workspaceName:        "",
  workspaceLogo:        null,
  hrEmail:              null,
  supportEmail:         null,
  primaryBrandColor:    null,
  secondaryBrandColor:  null,
  orgTimeZone:          null,
});

// ── Provider ─────────────────────────────────────────────────────────────────
export function WorkspaceProvider({ children }) {
  const [workspace,  setWorkspace]  = useState(readCached);  // instant first paint from cache
  // The cached copy must set the zone too, or the first paint after a refresh
  // formats in the fallback zone until the network call returns.
  if (workspace?.default_timezone) setOrgTimeZone(workspace.default_timezone);
  const [loading,    setLoading]    = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);

  // Reactive token from AuthContext — WorkspaceProvider is nested inside
  // AuthProvider so useAuth() is always available here.
  // Using the React state value (not readToken() from storage) means the
  // useEffect below re-runs automatically on same-tab login/logout without
  // relying on the storage event (which only fires in OTHER browser tabs).
  const { token: authToken } = useAuth();

  // Holds the in-flight promise (not just a boolean) so a caller that arrives
  // mid-flight can await the SAME request instead of silently getting nothing
  // back — the old boolean guard made a Retry click a no-op whenever a fetch
  // happened to already be running.
  const inFlight = useRef(null);

  const fetchSettings = useCallback((opts = {}) => {
    const token = readToken();
    if (!token) return Promise.resolve();

    // Note: GET /org/settings uses get_current_user (not an admin gate) and
    // returns synthesized defaults when organization_id is None, so it never
    // 403s — including for super_admin. We must NOT early-return here on role,
    // or the consumer (WorkspaceProfileTab) hangs forever on its loading
    // skeleton (settings stays null while loadFailed stays false).

    // Deduplicate concurrent calls — mirrors the guard in AuthContext.fetchMe().
    // `force` (the user pressing Retry) bypasses it so an explicit retry always
    // reaches the network.
    if (inFlight.current && !opts.force) return inFlight.current;

    setLoading(true);
    setLoadFailed(false);

    let p;
    p = (async () => {
      try {
        // authFetch (not raw fetch): attaches the token from EITHER storage
        // tier, and on 401 performs the same single-flight silent refresh +
        // replay the axios interceptor does. Without it an expired access
        // token turned into a permanent "Could not load workspace profile"
        // screen while the session was in fact still alive.
        const res = await authFetch(`${API_BASE}/org/settings`);

        if (res.ok) {
          const data = await res.json();
          setWorkspace(data);
          // Point every time formatter at THIS workspace's clock. Done here
          // because this is the one place the setting arrives, and the
          // formatters are module-level (see lib/orgTime.js for why).
          setOrgTimeZone(data?.default_timezone);
          writeCached(data);
          setLoadFailed(false);
        } else {
          // Still failing after the refresh + replay. Flag it so consumers can
          // offer a retry, but do NOT wipe `workspace`/the cache: a genuinely
          // dead session is already being redirected to login by authFetch
          // (which clears this cache on the way out), so blanking here only
          // ever destroys a good last-known copy during a transient blip.
          setLoadFailed(true);
        }
      } catch {
        // AbortError (timeout) or network failure, already retried once inside
        // authFetch — mark failed so callers can retry.
        setLoadFailed(true);
      } finally {
        if (inFlight.current === p) inFlight.current = null;
        setLoading(false);
      }
    })();
    inFlight.current = p;
    return p;
  }, []);                // no deps — reads token from storage at call time

  // Re-fetch on a change of SESSION, not on a change of token string.
  //
  // hasSession (a boolean) — not authToken (the JWT itself) — is the dependency,
  // and that distinction is load-bearing. AuthContext now tracks silent token
  // rotation, so the raw token string changes every time a 401 triggers a
  // refresh. Keying the effect on the string would make each rotation re-run it:
  //
  //   /org/settings 401s → authFetch refreshes → new token → AuthContext state
  //   updates → this effect re-runs → 401 again → refresh again → …
  //
  // i.e. any PERSISTENTLY-401ing request paired with a HEALTHY refresh endpoint
  // becomes an unbounded loop hammering /auth/refresh. That is not hypothetical:
  // get_current_user has 401 paths a fresh token cannot fix (a decode failure, a
  // missing user_id claim, a vanished user row). Depending on the boolean means a
  // rotation stays true→true and changes nothing, while the transitions this
  // effect actually cares about still fire:
  //   • Mount with an existing session token → load cached/fresh logo
  //   • Same-tab login (false → true)        → fetch the new org's settings
  //   • Logout (true → false)                → clear workspace and cache
  // A same-tab switch between two different orgs' tokens without an intervening
  // logout would not be seen here, but no such path exists: redirectToLogin()
  // clears storage (including this cache) and hard-navigates, and the storage
  // listener below covers the other-tab login case explicitly.
  const hasSession = !!authToken;
  useEffect(() => {
    if (!hasSession) {
      setWorkspace(null);
      clearCached();
      return;
    }
    fetchSettings();
  }, [hasSession, fetchSettings]);

  // Clear workspace on logout and re-fetch on login — listens to the same
  // localStorage events AuthContext uses for cross-tab sync.
  useEffect(() => {
    const onStorage = (e) => {
      if (e.key === "zt_token" || e.key === "token") {
        if (!e.newValue) {
          // Token removed → logout
          setWorkspace(null);
          clearCached();
        } else {
          // Token appeared → new login in another tab
          // Clear stale cache first so a different org's settings are never shown.
          clearCached();
          setWorkspace(null);
          fetchSettings();
        }
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, [fetchSettings]);

  // Public refresh — call this after any PUT /org/settings save so the
  // context (and therefore every consumer) reflects the latest data. Forced,
  // because this is always an explicit "I want current data" request (a save
  // just landed, or the user pressed Retry) and must never fold into an
  // older in-flight read.
  const refresh = useCallback(() => fetchSettings({ force: true }), [fetchSettings]);

  // ── Derived display values with fallbacks ─────────────────────────────────
  const workspaceName       = workspace?.company_name        || "";
  const workspaceLogo       = workspace?.logo_url            || null;
  const hrEmail             = workspace?.hr_email            || null;
  const supportEmail        = workspace?.support_email       || null;
  const primaryBrandColor   = workspace?.primary_brand_color || null;
  const secondaryBrandColor = workspace?.secondary_brand_color || null;
  const orgTimeZone         = workspace?.default_timezone    || null;

  const value = useMemo(() => ({
    workspace,
    loading,
    loadFailed,
    refresh,
    workspaceName,
    workspaceLogo,
    hrEmail,
    supportEmail,
    primaryBrandColor,
    secondaryBrandColor,
    orgTimeZone,
  }), [
    workspace, loading, loadFailed, refresh,
    workspaceName, workspaceLogo, hrEmail, supportEmail,
    primaryBrandColor, secondaryBrandColor, orgTimeZone,
  ]);

  return (
    <WorkspaceContext.Provider value={value}>
      {children}
    </WorkspaceContext.Provider>
  );
}

// ── Consumer hook ─────────────────────────────────────────────────────────────
export function useWorkspace() {
  const ctx = useContext(WorkspaceContext);
  if (!ctx) throw new Error("useWorkspace must be used within WorkspaceProvider");
  return ctx;
}

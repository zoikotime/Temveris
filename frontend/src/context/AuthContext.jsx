/**
 * AuthContext — single source of truth for the signed-in user.
 *
 * Solves three real problems we had spread across the app:
 *   1. Profile data was read from localStorage in a dozen components, so a
 *      profile-picture or name update only showed up after a hard refresh.
 *   2. Every page's logout button had its own copy of the cleanup logic, and
 *      they didn't all clear the same keys.
 *   3. There was no central place to react to a 401 and bounce the user out.
 *
 * Public surface:
 *   const { user, loading, refreshUser, logout, login, isAuthenticated } = useAuth();
 *
 *   user: { id, name, email, role, designation, department, team, profile_picture } | null
 *   refreshUser():  re-fetches GET /auth/me and updates context + storage mirror
 *   logout():       clears tokens + storage and navigates to /
 *   login(token, userMaybe, { remember? }):
 *     persists token (localStorage if remember, sessionStorage otherwise),
 *     fetches /auth/me if no user given. `remember` defaults to true so
 *     existing call sites keep their current behaviour.
 *
 * Storage choice is delegated to api/authStorage.js — see TOKEN_KEYS there.
 */
import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import API from "../api/axios";
import {
  readToken,
  writeToken,
  clearAuthStorage,
  mirrorUserToStorage,
  redirectToLogin,
} from "../api/authStorage";
import { refreshAccessToken, TOKEN_REFRESHED_EVENT } from "../api/refresh";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [token, setTokenState] = useState(() => readToken());
  const [user, setUser] = useState(null);
  // Start in "loading" even with no token: on a fresh page load we attempt a
  // silent refresh (remember-me cookie on web, safeStorage token in Electron)
  // before deciding the user is logged out — this is the auto-login path.
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const fetchInFlight = useRef(false);
  const didInit = useRef(false);

  const fetchMe = useCallback(async () => {
    if (fetchInFlight.current) return null;
    fetchInFlight.current = true;
    try {
      const res = await API.get("/auth/me");
      const u = res?.data || null;
      setUser(u);
      mirrorUserToStorage(u);
      setError(null);
      return u;
    } catch (e) {
      // 401 is handled globally by the axios interceptor (clears token).
      // Other errors keep the previous user value so transient network blips
      // don't sign the user out visually.
      if (e?.response?.status === 401) {
        // DESKTOP: the Electron main process is the single owner of session
        // lifecycle — it holds the encrypted refresh token and performs the
        // logout (reloading the window to /login on a hard 401). If we ALSO
        // clearAuthStorage() here, the main process's localStorage watcher
        // sees the token vanish and fires its own logout+reload, producing the
        // white-screen flicker. So on desktop we leave storage intact and defer
        // entirely to main. On web there's no such owner, so we clear as before.
        const isDesktop = typeof window !== "undefined" && window.isDesktopApp === true;
        if (!isDesktop) {
          setUser(null);
          clearAuthStorage();
          setTokenState(null);
        }
      } else {
        setError(e?.message || "Failed to load profile");
      }
      return null;
    } finally {
      fetchInFlight.current = false;
    }
  }, []);

  // Initial load — restore the session once on mount.
  //   1. If we already hold an access token → hydrate the user from it.
  //   2. Otherwise try a silent refresh (remember-me). If it yields a token,
  //      hydrate. If not, the user is genuinely logged out.
  // This is what makes "reopen the browser/app and you're still in" work.
  //
  // CRITICAL: `setLoading(false)` MUST always run once the bootstrap settles,
  // or the app hangs on the "Welcoming you back…" splash. Under React
  // StrictMode (dev) the effect mounts → cleans up → remounts: the remount
  // bails on the `didInit` guard, so only the FIRST run can clear loading. A
  // previous version gated `setLoading(false)` behind a `cancelled` flag that
  // the cleanup had already flipped true — so loading never resolved and the
  // splash stuck forever. We now clear loading in a `finally`, unconditionally.
  // The `didInit` guard still prevents a duplicate /auth/me round-trip.
  useEffect(() => {
    if (didInit.current) return;
    didInit.current = true;

    (async () => {
      try {
        let t = readToken();
        if (!t) {
          const restored = await refreshAccessToken();
          if (restored) {
            t = restored;
            setTokenState(restored);
          }
        }
        if (!t) {
          setUser(null);
          return;
        }
        await fetchMe();
      } finally {
        // Always resolve the splash — even if a network blip or an unexpected
        // throw occurs above, the user should land on the login form, never a
        // perpetual spinner.
        setLoading(false);
      }
    })();
  }, [fetchMe]);

  // Cross-tab sync: if the user signs out in another tab, drop our user too.
  // Listening on `storage` covers localStorage changes (token wipe in another
  // tab), which is the case that actually matters — sessionStorage is per-tab
  // by definition so there's nothing to sync there.
  useEffect(() => {
    const onStorage = () => {
      const t = readToken();
      setTokenState(t);
      if (!t) setUser(null);
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  // Same-tab token rotation. A silent refresh (api/refresh.js) writes the new
  // access token straight to storage — invisible to React, and the `storage`
  // event only fires in OTHER tabs. Without this listener `token` here stays
  // the expired string forever, so consumers keyed on it (WorkspaceContext
  // re-fetches its settings when the token changes) never learn the session was
  // renewed and stay stuck on whatever error state the 401 left behind.
  useEffect(() => {
    const onRefreshed = (e) => {
      const t = e?.detail?.token || readToken();
      if (t) setTokenState(t);
    };
    window.addEventListener(TOKEN_REFRESHED_EVENT, onRefreshed);
    return () => window.removeEventListener(TOKEN_REFRESHED_EVENT, onRefreshed);
  }, []);

  const login = useCallback(async (newToken, userMaybe, opts = {}) => {
    const remember = opts.remember !== false;   // default true — preserves prior behaviour
    if (newToken) {
      writeToken(newToken, { remember });
      setTokenState(newToken);
    }
    if (userMaybe) {
      setUser(userMaybe);
      mirrorUserToStorage(userMaybe);
      return userMaybe;
    }
    return await fetchMe();
  }, [fetchMe]);

  const logout = useCallback(async ({ redirect = true } = {}) => {
    // Best-effort server-side revoke: kills the refresh token + clears the
    // HttpOnly cookie so the persistent session can't be resurrected. Never
    // let a network hiccup block the local sign-out.
    try { await API.post("/auth/logout"); } catch { /* offline / already gone */ }

    // Reset in-memory auth state. Storage + navigation are delegated to the one
    // shared pipeline so web and desktop each perform EXACTLY ONE navigation and
    // never race (redirectToLogin: web clears storage + navigates; desktop hands
    // off to the Electron main process, which owns both).
    setUser(null);
    setTokenState(null);

    if (redirect) {
      redirectToLogin();
    } else {
      // Caller will navigate itself — just clear local state. (Desktop still
      // hands storage/session teardown to main via redirectToLogin elsewhere.)
      clearAuthStorage();
    }
  }, []);

  const refreshUser = fetchMe;

  // Optimistic local update — used by the profile editor so the avatar in
  // the topnav flips the moment the user picks a new picture, before the
  // PATCH round-trips. The next refreshUser() will reconcile with server.
  const updateUserLocal = useCallback((patch) => {
    setUser((prev) => {
      const next = { ...(prev || {}), ...patch };
      mirrorUserToStorage(next);
      return next;
    });
  }, []);

  const value = useMemo(() => ({
    user,
    token,
    loading,
    error,
    isAuthenticated: !!token,
    login,
    logout,
    refreshUser,
    updateUserLocal,
  }), [user, token, loading, error, login, logout, refreshUser, updateUserLocal]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    // Defensive default — lets non-wrapped trees (e.g. Login) call useAuth
    // without crashing during the migration. They'll get a no-op API and
    // storage-derived best-effort values.
    return {
      user: null,
      token: readToken(),
      loading: false,
      error: null,
      isAuthenticated: !!readToken(),
      login: async () => null,
      logout: () => { redirectToLogin(); },
      refreshUser: async () => null,
      updateUserLocal: () => {},
    };
  }
  return ctx;
}

export default AuthContext;

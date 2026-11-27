"use strict";

const { contextBridge, ipcRenderer } = require("electron");

// ── Restore full session SYNCHRONOUSLY before React boots ─────────────────────
// ipcRenderer.sendSync blocks until main replies — guaranteed before
// any <script> executes, so React PrivateRoute sees the token on first render.
const session = ipcRenderer.sendSync("preload:get-session");
if (session && session.token) {
  try {
    localStorage.setItem("zt_token", session.token);
    localStorage.setItem("token",    session.token);
    if (session.name)    localStorage.setItem("name",    session.name);
    if (session.email)   localStorage.setItem("email",   session.email);
    if (session.role)    localStorage.setItem("role",    session.role);
    if (session.user_id) localStorage.setItem("user_id", session.user_id);
    console.log("[preload] Session hydrated for:", session.name || session.email);
  } catch (e) {
    console.warn("[preload] localStorage hydration failed:", e.message);
  }
} else {
  console.log("[preload] No saved session — showing login.");
}

// ── Expose safe APIs to the renderer ──────────────────────────────────────────
contextBridge.exposeInMainWorld("isDesktopApp", true);

contextBridge.exposeInMainWorld("electronWindow", {
  minimize: () => ipcRenderer.send("window:minimize"),
  close:    () => ipcRenderer.send("window:close"),
});

// ── Let React push session to main after login (fast path, no polling lag) ────
contextBridge.exposeInMainWorld("electronAuth", {
  // Call from your React login success callback:
  //   window.electronAuth?.saveSession({ token, name, email, role, user_id })
  saveSession: (s) => ipcRenderer.send("auth:save-session", s),

  // Call from your React logout handler:
  //   window.electronAuth?.logout()                         // main drives nav
  //   window.electronAuth?.logout({ rendererNavigates: true }) // SPA drives nav
  //
  // Pass { rendererNavigates: true } when the renderer has ALREADY done a smooth
  // in-document React-Router navigate() to /login — main then only tears down
  // the session/tracking and skips the hard reload (that reload is the
  // white-screen glitch). Omit it (or pass nothing) when the renderer can't
  // navigate itself (401 interceptor, tray, watchdog) and main must load /login.
  logout: (opts) => ipcRenderer.send("auth:logout", opts),

  // Silent token refresh. The long-lived refresh token lives ONLY in the main
  // process (OS-encrypted safeStorage) — the renderer never sees it. The axios
  // 401 interceptor calls this; main performs POST /auth/refresh, rotates the
  // stored token, and returns the fresh access token.
  //   const { access_token } = await window.electronAuth.refreshToken();
  refreshToken: () => ipcRenderer.invoke("auth:refresh"),

  // Browser-style "Save password" — main process encrypts via safeStorage
  // (DPAPI / Keychain / libsecret) and persists alongside the session file.
  //   window.electronAuth?.saveCredentials({ email, password })
  saveCredentials: (c) => ipcRenderer.send("auth:save-credentials", c),

  // Synchronous so the React login page can prefill on first render without
  // a flash of empty inputs. Returns { email, password } | null.
  getCredentials: () => {
    try { return ipcRenderer.sendSync("auth:get-credentials"); }
    catch { return null; }
  },

  // Wipe the saved password (called when the user unticks "Remember" before
  // submitting the login form).
  clearCredentials: () => ipcRenderer.send("auth:clear-credentials"),
});

// ── Let React push clock-in/out status to main immediately ────────────────────
// Call after any clock-in, clock-out, break start/end in the React app:
//   window.electronTracking?.updateStatus("working")       // clocked in
//   window.electronTracking?.updateStatus("on_break")      // on break
//   window.electronTracking?.updateStatus("not_clocked_in") // clocked out
contextBridge.exposeInMainWorld("electronTracking", {
  updateStatus: (status) => ipcRenderer.send("tracking:status-update", status),

  // Main → renderer "sync now" push. The main process fires this the instant a
  // session-state moment happens that it owns — session restore, login, silent
  // token refresh, wake-from-sleep — so the dashboard can re-fetch employee
  // state IMMEDIATELY instead of waiting for its poll cadence. The callback
  // receives a short reason string ("restore" | "login" | "token-refresh" |
  // "resume"). Returns an unsubscribe fn for useEffect cleanup.
  onForceRefresh: (cb) => {
    if (typeof cb !== "function") return () => {};
    const handler = (_event, reason) => {
      try { cb(reason); } catch { /* renderer-side errors must not crash IPC */ }
    };
    ipcRenderer.on("tracking:force-refresh", handler);
    return () => {
      try { ipcRenderer.removeListener("tracking:force-refresh", handler); } catch {}
    };
  },
});

// ── Native auto-update bridge ─────────────────────────────────────────────────
// Drives the in-app updater popup (ElectronDashboard.jsx) directly from
// electron-updater, so the renderer never has to fall back to a browser
// download. The renderer:
//   1. window.electronUpdater.onStatus(cb)          // subscribe to events
//   2. window.electronUpdater.download()            // start download
//   3. window.electronUpdater.quitAndInstall()      // restart + install
//
// Status payloads are always { state, ...extra } — see emitUpdaterStatus in
// main.js for the exact shape per state.
contextBridge.exposeInMainWorld("electronUpdater", {
  // Trigger a manual check (the main process also auto-checks on launch +
  // every 4 h). Resolves to { ok, version, reason? }.
  check: () => ipcRenderer.invoke("updater:check"),

  // Start the in-app download. Resolves once electron-updater has accepted
  // the request; the renderer should watch `progress` / `downloaded` events
  // via onStatus for the actual UI.
  download: () => ipcRenderer.invoke("updater:download"),

  // Tear everything down and let NSIS swap the binary. The main process
  // stops tracking + clears timers BEFORE calling autoUpdater.quitAndInstall,
  // so this is fire-and-forget from the renderer's side.
  quitAndInstall: () => ipcRenderer.send("updater:quit-and-install"),

  // Subscribe to status events. Returns an unsubscribe function so callers
  // can clean up in a React useEffect return without leaking listeners.
  onStatus: (cb) => {
    if (typeof cb !== "function") return () => {};
    const handler = (_event, payload) => {
      try { cb(payload); } catch { /* renderer-side errors must not crash IPC */ }
    };
    ipcRenderer.on("updater:status", handler);
    return () => {
      try { ipcRenderer.removeListener("updater:status", handler); } catch {}
    };
  },
});
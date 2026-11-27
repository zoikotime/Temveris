"use strict";

/**
 * main.js — Temveris Desktop (Electron)
 *
 * ROOT CAUSE OF "shows Good Morning but then login page":
 *   The old code called loadSavedSession() AFTER loadURL(), so the preload's
 *   sendSync("preload:get-session") fired before savedSession was populated,
 *   returning null. localStorage stayed empty → React showed login.
 *
 * FIX: loadSavedSession() is called FIRST. savedSession is ready before
 *   loadURL() so the preload's synchronous reply carries the full session.
 *
 * OTHER FIXES:
 *   - auth:save-session IPC  → React pushes token to main right after login
 *   - auth:logout IPC        → React triggers logout cleanly
 *   - extractSession debounced 400ms (no rapid-fire on SPA nav)
 *   - tokenTimer kept at 10s as safety net (not primary path)
 *   - "Good Morning Admin" greeting removed from tray
 *   - Tray status labels are plain strings, no emojis that break on Linux
 */

const {
  app,
  BrowserWindow,
  Tray,
  Menu,
  nativeImage,
  Notification,
  shell,
  ipcMain,
  powerMonitor,
  dialog,
  screen: electronScreen,
  desktopCapturer,
  systemPreferences,
  safeStorage,
} = require("electron");
const path = require("path");
const fs = require("fs");
const { execFile } = require("child_process");
const { promisify } = require("util");
const execFileP = promisify(execFile);
const AutoLaunch = require("auto-launch");
// electron-updater is lazy-loaded inside setupAutoUpdater() — see note there.
// Requiring it at top-level evaluates an `AppUpdater` singleton that touches
// `app.getVersion()` immediately, which crashes the dev-mode `electron .`
// launch ("Cannot read properties of undefined (reading 'getVersion')") on
// certain electron@29 / node 20.x combos. Packaged builds always have the
// app fully initialised, so the late require there is safe.
let autoUpdater = null;
const browserTracker = require("./browser-tracker");

// node-fetch is ESM-only, so this CommonJS file must load it via dynamic
// import(). Previously every network function called `await import("node-fetch")`
// on its own — Node caches the module so this never re-read disk, but each call
// still allocated a microtask + re-resolved the namespace's `.default` getter on
// every screenshot upload / heartbeat / poll. Memoize the resolved fetch fn once
// so all call sites await the SAME promise. Behavior is identical (same module,
// same default export); we only avoid repeating the resolution work.
let _fetchPromise;
function getFetch() {
  return (_fetchPromise ||= import("node-fetch").then((m) => {
    const raw = m.default;
    // node-fetch v3 REMOVED the `timeout` option that v2 honoured. Every
    // `timeout:` passed by the ~11 call sites in this file was therefore
    // SILENTLY IGNORED, so a request hung until the OS TCP timeout (30 s to
    // minutes) instead of the seconds intended. That is what let pollStatus
    // stall past its own 60 s interval and pile up failures.
    //
    // Translating the option to v3's AbortSignal HERE rather than rewriting
    // every call site keeps the existing style, fixes all of them at once,
    // and leaves no site able to "forget" the timeout later.
    return function fetchWithTimeout(url, opts = {}) {
      const { timeout, ...rest } = opts;
      // An explicit caller-supplied signal wins — never override it.
      if (!timeout || rest.signal) return raw(url, rest);
      const ac = new AbortController();
      const timer = setTimeout(() => ac.abort(), timeout);
      // .finally() clears the timer on BOTH settle paths and re-throws the
      // original rejection, so existing try/catch blocks behave unchanged and
      // no unhandled rejection is introduced.
      return raw(url, { ...rest, signal: ac.signal }).finally(() =>
        clearTimeout(timer),
      );
    };
  }));
}

// Screen capture uses Electron's native desktopCapturer (no .NET, no temp
// .bat compilation, no AV-flagged child process). The previous dependency
// (screenshot-desktop) failed silently on machines without legacy .NET
// Framework or where AV quarantined its compiled helper exe — that was the
// root cause of "screenshots stopped for some employees".

// ─── APP IDENTITY (Windows taskbar grouping + correct icon) ──────────────────
// Without this Windows associates the running window with electron.exe's
// AUMID, so the taskbar shows the generic Electron cog icon instead of the
// Temveris icon embedded in our packaged installer. The value MUST match
// the `appId` field in package.json so Squirrel / NSIS uninstaller, jump
// list, and pinned-shortcut grouping all line up.
if (process.platform === "win32") {
  try { app.setAppUserModelId("com.temveris.app"); } catch {}
}

// ─── CONFIG ───────────────────────────────────────────────────────────────────
// Placeholder default until the real domain exists (see TEMVERIS_TODO.md).
const WEBSITE_URL = process.env.ZOIKO_URL || "https://app.temveris.example";
const API_BASE = WEBSITE_URL;
// Fallbacks used only until GET /settings answers (and if it never does).
// They MUST equal the backend system defaults in app/settings/router.py —
// they were 30s / 3min, so a workspace whose settings fetch failed screenshotted
// 60x more often than any admin had configured, and flagged idle 5x sooner.
let SCREENSHOT_INTERVAL = 1800 * 1000; // system default: 1800 s
const STATUS_POLL_INTERVAL = 60 * 1000; // 60 s
const TOKEN_POLL_INTERVAL = 60 * 1000; // 60 s

// Refresh the access token this long BEFORE it expires, so a transient network
// failure at refresh time still leaves a valid token + a full retry window
// (rather than an already-expired token and an immediate 401). 30-min tokens →
// refresh at the ~25-min mark. See armProactiveRefresh()/triggerRefresh().
const PROACTIVE_REFRESH_BUFFER_MS = 5 * 60 * 1000;
const PROACTIVE_REFRESH_MIN_MS = 60 * 1000;            // never schedule sooner than this
const PROACTIVE_REFRESH_FALLBACK_MS = 20 * 60 * 1000;  // used when the JWT exp can't be read
// Backoff schedule for re-attempting a refresh after a TRANSIENT failure
// (network blip / 5xx). Self-heals without waiting for the next 60s status poll.
const REFRESH_RETRY_BACKOFF_MS = [5000, 15000, 45000, 120000];
// 3 s (was 1 s). mouseMoveCount / activeSeconds are AGGREGATE counters that are
// only consumed once per screenshot interval (~30 s), so coarser sampling barely
// changes accuracy while cutting main-process wakeups ~3x — a meaningful laptop
// battery win (fewer wakeups = deeper CPU idle C-states).
const ACTIVITY_POLL_MS = 3 * 1000;
const HEARTBEAT_INTERVAL = 60 * 1000; // 60 s — increased from 30s to reduce load

// ─── STATE ────────────────────────────────────────────────────────────────────
let mainWindow = null;
let tray = null;
let screenshotTimer = null;
let activityTimer = null;
let statusTimer = null;
let tokenTimer = null;
let heartbeatTimer = null; // lightweight foreground-app ping (no screenshot)
let settingsTimer = null; // periodic settings re-fetch while tracking
let isTracking = false;
let currentStatus = "not_clocked_in";
let authToken = null;
// Long-lived rotating refresh token — kept ONLY in memory + OS-encrypted on
// disk (zt_auth.json `refresh_blob`). Never written in plaintext, never sent
// to the renderer. `refreshInFlight` makes refreshAccessToken() single-flight
// so a renderer 401 and a pollStatus 401 can't both spend the (single-use)
// token at once and trip server-side reuse detection.
let refreshToken = null;
let refreshInFlight = null;
// Proactive (before-expiry) refresh + transient-failure backoff retry. These
// keep the access token fresh so a 401 — and the logout risk clustered around
// it — is the exception, not the steady state. See armProactiveRefresh().
let proactiveRefreshTimer = null;
let refreshRetryTimer = null;
let refreshRetryAttempt = 0;
let isQuiting = false;
let extractDebounce = null;
// Consecutive times extractSession() has seen localStorage with no token while
// we still hold one in memory. A token can briefly vanish mid-refresh (the
// renderer rewrites zt_token) or during the un-desktop-gated clearAuthStorage()
// in the web axios interceptor; logging out on the FIRST empty read caused a
// spurious loadURL("/login") — a white-screen reload + forced re-clock-in. We
// require the empty state to persist across N CONSECUTIVE checks before
// treating it as a real logout, so a refresh race can never log the user out.
let emptyTokenStreak = 0;
const EMPTY_TOKEN_LOGOUT_THRESHOLD = 3; // consecutive empty reads required to log out
// Set by loadSavedSession() when it restores an access token but no refresh
// token (a v1.0.7-format file, or a safeStorage decrypt failure). Drives the
// one-time silent re-auth migration so the first 401 doesn't force a re-login.
let _sessionNeedsRefreshTokenMigration = false;
let screenshotEnabled = true; // respects admin "screenshot_enabled" setting
let screenshotBlur = false; // respects admin "screenshot_blur" setting
let idleThresholdMins = 15; // system default; admin "idle_threshold_minutes" overrides
// Activity-tracking flags — server-driven via GET /settings (effective per-user
// through the org→team→employee cascade). Default ON to match legacy behaviour;
// updated in fetchAndApplySettings and gated at each collection point.
let trackMouse = true;
let trackKeyboard = true;
let trackApps = true;
let trackBrowser = true;
let trackUrls = true;
let captureFailCount = 0; // consecutive capture failures for user notification

// ─── ACTIVITY INTEGRITY (anti-fake-activity) ──────────────────────────────────
// Phase-4 report), not a hardening change.
let pollFailCount = 0; // consecutive pollStatus failures
// Guards against two pollStatus() calls overlapping. setInterval does not await
// its async callback, so a slow poll used to let the next one start on top of
// it — each able to increment pollFailCount independently.
let _pollInFlight = false;
// One-shot latch for the "network unavailable" notification, so a long outage
// notifies once rather than every 60 s. Cleared on the first successful poll.
let _networkPauseNotified = false;
let isScreenLocked = false; // true while Windows lock screen / macOS login window is shown
let networkMonitorTimer = null;
let offlineSince = null; // timestamp when network was first detected as unavailable

let lastActiveAt             = Date.now(); // updated each activity poll we see input
// Snapshot of recent activity — surfaced in the tray "Diagnostics" menu so a
// user/admin can see live what the tracker thinks without attaching to logs.
let lastDiagnostics = {
  idleSec: 0,
  lastHeartbeatAt: 0,
  lastActiveAt: Date.now(),
};

// ─── ACTIVITY COUNTERS ────────────────────────────────────────────────────────
// Reset every screenshot interval — see resetActivityCounters().
let keyCountApp = 0; // keys typed inside the Electron window
let keyCountGlobal = 0; // keys typed anywhere (any HID, any focused app)
let mouseMoveCount = 0; // 1-sec polls where cursor position changed
let mouseClickGlobal = 0; // mousedown events (real clicks) anywhere on the system
let mouseScrollGlobal = 0; // wheel events anywhere on the system
let activeSeconds = 0; // 1-sec polls where the OS reported the user was not idle
let lastMousePos = null;
const KEY_BURST_WINDOW_MS = 1000;
const KEY_BURST_CAP = 12;
let keyBurstWindowTs = 0;
let keyBurstCount = 0;
// Wall-clock start of the current interval — sent as `interval_seconds` so the
// backend can compute idle-within-tick accurately rather than guessing from the
// configured screenshot interval. Set on every reset so the first interval
// after a clock-in / unlock / settings change measures the right window.
let intervalStartTs = Date.now();

// ─── GLOBAL INPUT HOOK (uiohook-napi) ─────────────────────────────────────────
// Without a system-wide hook the only keystrokes Electron sees are those
// typed into the Temveris window itself — so an employee using an
// external keyboard inside their actual work app (browser, IDE, Excel)
// produces zero `keyCountApp` and looks idle. uiohook-napi taps the OS
// input stack so we count *every* keypress and mouse click regardless of
// which device produced it or which app is focused (BUG-012).
//
// The require is wrapped in try/catch and the listeners are guarded so
// if the prebuilt binary is missing on this platform/architecture the
// tracker falls back to the previous heuristic instead of crashing on
// startup. This keeps the historical install path working even when the
// optional native dep can't be loaded (e.g. unusual Linux distros, ARM
// macOS without native build tools, locked-down corporate machines).
let uIOhook = null;
let uioStarted = false;
let UiohookKeyEnum = null;
try {
  // eslint-disable-next-line global-require
  const mod = require("uiohook-napi");
  uIOhook = mod && mod.uIOhook;
  UiohookKeyEnum = mod && mod.UiohookKey;
} catch (err) {
  console.warn(
    "[input-hook] uiohook-napi unavailable — falling back to in-window key " +
      "events + system idle heuristic. External-device keystrokes outside " +
      "the Temveris window will be approximated, not exact. Reason:",
    err && err.message,
  );
  uIOhook = null;
}

// Named global-input handlers. These MUST be module-level NAMED functions (not
// inline arrows) so stopGlobalInputHook can remove the EXACT same references.
// Previously these were anonymous arrows that could never be removed, so every
// clock-in / break-resume re-registered a fresh listener and each physical
// key/click/scroll was counted once PER accumulated listener — the v1.0.11
// count-inflation bug. recordGlobalKeydown (defined below) is the keydown one.
function _onGlobalMousedown() {
  if (isTracking && trackMouse) mouseClickGlobal++;
}
// Wheel events count as "mouse activity" too — a worker scrolling through
// docs/code/tickets is clearly working even with no clicks or keystrokes.
// Without this, heavy reading/scrolling stretches were misclassified as idle.
function _onGlobalWheel() {
  if (isTracking && trackMouse) mouseScrollGlobal++;
}

// Debug helper: how many of each listener uiohook currently holds. Lets us SEE
// the count stay flat (1 each while tracking, 0 each when stopped) across
// clock/break cycles instead of climbing. Returns -1 if listenerCount is
// unavailable on this build.
function _inputHookListenerCounts() {
  try {
    if (uIOhook && typeof uIOhook.listenerCount === "function") {
      return {
        keydown: uIOhook.listenerCount("keydown"),
        mousedown: uIOhook.listenerCount("mousedown"),
        wheel: uIOhook.listenerCount("wheel"),
      };
    }
  } catch {}
  return { keydown: -1, mousedown: -1, wheel: -1 };
}

// Detach our three handlers. Passing the SAME named references we registered
// removes exactly our listeners and nothing else; removeListener is a no-op for
// handlers that aren't currently attached, so this is always safe to call.
function _detachInputHookListeners() {
  if (!uIOhook) return;
  try { uIOhook.removeListener("keydown", recordGlobalKeydown); } catch {}
  try { uIOhook.removeListener("mousedown", _onGlobalMousedown); } catch {}
  try { uIOhook.removeListener("wheel", _onGlobalWheel); } catch {}
}

function startGlobalInputHook() {
  if (!uIOhook || uioStarted) return;
  try {
    // Defensive idempotency: strip any prior copies BEFORE re-adding, so a
    // start can never leave two of the same listener attached even if a stop
    // was skipped or the uiohook singleton survived something unexpected.
    _detachInputHookListeners();
    uIOhook.on("keydown", recordGlobalKeydown);
    uIOhook.on("mousedown", _onGlobalMousedown);
    uIOhook.on("wheel", _onGlobalWheel);
    uIOhook.start();
    uioStarted = true;
    const c = _inputHookListenerCounts();
    console.log(
      `[input-hook] listeners attached — keydown=${c.keydown} ` +
        `mousedown=${c.mousedown} wheel=${c.wheel} (expect 1 each)`,
    );
  } catch (err) {
    console.warn("[input-hook] start failed:", err && err.message);
    uIOhook = null;
  }
}

function stopGlobalInputHook() {
  if (!uIOhook || !uioStarted) return;
  try {
    uIOhook.stop();
  } catch {}
  // Remove the exact listeners we attached so a later start (Clock In / Break /
  // Lunch Break / Resume / Clock Out cycle) can't stack a second set — the bug
  // that multiplied key/click/scroll counts by the number of cycles.
  _detachInputHookListeners();
  uioStarted = false;
  const c = _inputHookListenerCounts();
  console.log(
    `[input-hook] listeners removed — keydown=${c.keydown} ` +
      `mousedown=${c.mousedown} wheel=${c.wheel} (expect 0 each)`,
  );
}

// Built once at load — modifier / lock keycodes we don't count as real
// keystrokes. Sourced from the uiohook enum when available, plus the known
// raw scancodes (Ctrl/Alt/Shift/Meta/Caps/Num/Scroll, left+right) as a
// fallback for builds where the enum is incomplete. A Set keeps the keydown
// hot path allocation-free — this is checked on every system keypress.
const MODIFIER_KEYCODES = (() => {
  const K = UiohookKeyEnum || {};
  return new Set(
    [
      K.Ctrl, K.CtrlRight,
      K.Alt, K.AltRight,
      K.Shift, K.ShiftRight,
      K.Meta, K.MetaRight,
      K.CapsLock, K.NumLock, K.ScrollLock,
      29, 3613, 56, 3640, 42, 54, 3675, 3676, 58, 69, 70,
    ].filter((v) => typeof v === "number"),
  );
})();

function isModifierOnlyKey(keycode) {
  return !keycode || MODIFIER_KEYCODES.has(keycode);
}

function allowKeyBurstCount() {
  const now = Date.now();
  if (!keyBurstWindowTs || now - keyBurstWindowTs >= KEY_BURST_WINDOW_MS) {
    keyBurstWindowTs = now;
    keyBurstCount = 0;
  }
  if (keyBurstCount >= KEY_BURST_CAP) return false;
  keyBurstCount++;
  return true;
}

function recordGlobalKeydown(e = {}) {
  if (!isTracking) return;
  if (!trackKeyboard) return;
  if (isModifierOnlyKey(e.keycode)) return;
  if (!allowKeyBurstCount()) return;
  keyCountGlobal++;
}

function shouldCountAppInput(input = {}) {
  if (!isTracking) return false;
  if (input.type && input.type !== "keyDown") return false;
  const key = String(input.key || "");
  return !/^(Control|Ctrl|Shift|Alt|Meta|Command|CapsLock|NumLock|ScrollLock)$/i.test(key);
}

// ─── SESSION PERSISTENCE ──────────────────────────────────────────────────────
// Stored on disk: { token, name, email, role, user_id, saved_at }
// Preload reads this synchronously via "preload:get-session" BEFORE React boots.
const SESSION_FILE = path.join(app.getPath("userData"), "zt_auth.json");
let savedSession = null;

function saveSession(s) {
  if (!s || !s.token) return;

  console.log("[Session] Saving session");

  // Distinguish the THREE cases so the log is diagnostic, not just a yes/no:
  //   • caller supplied a fresh refresh_token  → "true (from caller)"
  //   • caller didn't, but we already had one  → "true (preserved)"
  //   • neither                                → "false (NOT PERSISTED — first
  //     401 will trip 'no refresh token' and the session will be destroyed)"
  // Without the breakdown it's impossible to tell whether the receiver dropped
  // a token the caller actually sent, vs. the caller never sent one.
  const supplied = !!s.refresh_token;
  const preserved = !supplied && !!refreshToken;
  const refresh = s.refresh_token || refreshToken || null;
  const refreshPresent = !!refresh;
  if (supplied) {
    console.log("[Session] Refresh token present: true (from caller)");
  } else if (preserved) {
    console.log("[Session] Refresh token present: true (preserved from prior save)");
  } else {
    console.warn(
      "[Session] Refresh token present: false — zt_auth.json will be saved " +
      "WITHOUT refresh_blob. The next 401 in the renderer will hit the " +
      "'no refresh token' branch in refreshAccessToken() and destroy the session.",
    );
  }

  refreshToken = refresh;
  authToken = s.token;

  // The refresh token is the only true long-lived secret here — seal it with
  // OS-level safeStorage (DPAPI / Keychain / libsecret) so a copied
  // zt_auth.json on another machine is useless. The access token is left as-is
  // (short-lived; the preload hydrates it synchronously into localStorage).
  let refresh_blob = null;
  if (refresh && safeStorage.isEncryptionAvailable()) {
    try {
      refresh_blob = safeStorage.encryptString(String(refresh)).toString("base64");
    } catch (e) {
      console.warn("[Session] refresh encrypt failed:", e.message);
    }
  }

  savedSession = {
    token:   s.token,
    name:    s.name    || (savedSession && savedSession.name)    || "",
    email:   s.email   || (savedSession && savedSession.email)   || "",
    role:    s.role    || (savedSession && savedSession.role)    || "employee",
    user_id: s.user_id || (savedSession && savedSession.user_id) || "",
    saved_at: Date.now(),
  };

  try {
    // Persist the non-secret fields + the ENCRYPTED refresh token. Never write
    // the raw refresh token to disk.
    fs.writeFileSync(
      SESSION_FILE,
      JSON.stringify({ ...savedSession, refresh_blob }),
      "utf-8",
    );
    console.log(
      `[Session] Saved → ${savedSession.name || savedSession.email || "user"} ` +
      `(refresh_blob written: ${!!refresh_blob})`,
    );
  } catch (e) {
    console.warn("[Session] Save failed:", e.message);
  }
  // Surface the final on-disk state so a grep of the log immediately shows
  // whether THIS save left the file in a recoverable shape.
  console.log(`[Session] Refresh token persisted: ${refreshPresent && !!refresh_blob}`);

  // A new access token just landed — (re)schedule the proactive refresh so it
  // fires a few minutes before THIS token expires, and clear any pending
  // transient-failure backoff (a successful save means we're healthy again).
  armProactiveRefresh();
}

function loadSavedSession() {
  try {
    if (!fs.existsSync(SESSION_FILE)) {
      console.log("[Session] No saved session file (zt_auth.json absent)");
      return null;
    }
    const d = JSON.parse(fs.readFileSync(SESSION_FILE, "utf-8"));
    if (!d || !d.token) {
      console.warn("[Session] zt_auth.json present but malformed — no token field");
      return null;
    }

    savedSession = {
      token: d.token, name: d.name, email: d.email,
      role: d.role, user_id: d.user_id, saved_at: d.saved_at,
    };
    authToken = d.token;

    // Decrypt the long-lived refresh token back into memory.
    refreshToken = null;
    const hadBlob = !!d.refresh_blob;
    if (hadBlob && safeStorage.isEncryptionAvailable()) {
      try {
        refreshToken = safeStorage.decryptString(Buffer.from(d.refresh_blob, "base64"));
      } catch (e) {
        console.warn("[Session] refresh decrypt failed:", e.message);
      }
    }

    console.log(
      `[Session] Session restored → ${d.name || d.email || "user"} ` +
      `(saved_at: ${d.saved_at ? new Date(d.saved_at).toISOString() : "unknown"})`,
    );
    // Differentiate the "no blob on disk" case from the "blob present but
    // decrypt failed / safeStorage unavailable" case — the second one
    // indicates an OS-level keyring problem and would otherwise look identical
    // in logs to a session that simply was never persisted with a refresh
    // token in the first place.
    if (refreshToken) {
      console.log("[Session] Refresh token restored: true");
    } else if (hadBlob) {
      console.warn(
        "[Session] Refresh token restored: false — refresh_blob existed on " +
        "disk but decrypt failed (safeStorage unavailable or key changed). " +
        "The session will be unable to silently renew.",
      );
    } else {
      console.warn(
        "[Session] Refresh token restored: false — zt_auth.json was saved " +
        "WITHOUT refresh_blob (v1.0.7-format file). Will attempt silent " +
        "credential-based migration before the first 401.",
      );
    }

    // A restored access token with NO refresh token is a "half session": a
    // v1.0.7-format file (no refresh_blob) or a safeStorage decrypt failure.
    // Flag it so createWindow can run the one-time silent re-auth migration
    // (see migrateLegacySessionIfNeeded) BEFORE the access token expires and a
    // 401 would otherwise force a manual re-login + re-clock-in.
    _sessionNeedsRefreshTokenMigration = !!(authToken && !refreshToken);

    return savedSession;
  } catch (e) {
    console.warn("[Session] Load failed:", e.message);
    return null;
  }
}

// Every workforce value the tracker holds belongs to ONE organization, resolved
// for ONE employee through the org>team>employee cascade. On a shared machine a
// logout must drop them: otherwise the next person to sign in is tracked under
// the previous tenant's policy until their own /settings fetch lands — and if
// that fetch fails (offline, 401, backend down) it never lands at all.
function resetResolvedPolicy() {
  SCREENSHOT_INTERVAL = 1800 * 1000;   // system defaults, per settings/router.py
  screenshotEnabled = true;
  screenshotBlur = false;
  idleThresholdMins = 15;
  trackMouse = true;
  trackKeyboard = true;
  trackApps = true;
  trackBrowser = true;
  trackUrls = true;
  console.log("[session] Resolved policy reset to system defaults");
}

function clearSession() {
  savedSession = null;
  authToken = null;
  refreshToken = null;
  resetResolvedPolicy();
  clearProactiveTimers(); // stop firing proactive/backoff refreshes after logout
  try {
    if (fs.existsSync(SESSION_FILE)) fs.unlinkSync(SESSION_FILE);
  } catch {}
  console.log("[session] Cleared");
}

// ─── REMEMBERED CREDENTIALS (browser-style "Save password") ───────────────────
// Encrypted with Electron's safeStorage — DPAPI on Windows, Keychain on macOS,
// libsecret on Linux. The plaintext password never touches disk. We deliberately
// DO NOT clear this on logout (mirrors Chrome/Vivaldi: saved passwords survive a
// site logout) — it's only wiped when the user explicitly unticks "Remember for
// 30 days" before signing in again.
const CREDS_FILE = path.join(app.getPath("userData"), "zt_creds.json");

function saveCredentials({ email, password } = {}) {
  if (!email || !password) return;
  if (!safeStorage.isEncryptionAvailable()) {
    console.warn("[creds] OS encryption unavailable — not persisting password");
    return;
  }
  try {
    const blob = safeStorage.encryptString(String(password)).toString("base64");
    fs.writeFileSync(
      CREDS_FILE,
      JSON.stringify({ email, blob, saved_at: Date.now() }),
      "utf-8",
    );
    console.log("[creds] Saved for", email);
  } catch (e) {
    console.warn("[creds] Save failed:", e.message);
  }
}

function loadSavedCredentials() {
  try {
    if (!fs.existsSync(CREDS_FILE)) return null;
    if (!safeStorage.isEncryptionAvailable()) return null;
    const d = JSON.parse(fs.readFileSync(CREDS_FILE, "utf-8"));
    if (!d || !d.email || !d.blob) return null;
    const password = safeStorage.decryptString(Buffer.from(d.blob, "base64"));
    return { email: d.email, password };
  } catch (e) {
    console.warn("[creds] Load failed:", e.message);
    return null;
  }
}

function clearCredentials() {
  try {
    if (fs.existsSync(CREDS_FILE)) fs.unlinkSync(CREDS_FILE);
    console.log("[creds] Cleared");
  } catch {}
}

// ─── LEGACY SESSION MIGRATION (v1.0.7 → v1.0.12) ──────────────────────────────
// v1.0.7 wrote zt_auth.json with NO refresh token (the feature didn't exist).
// After updating, loadSavedSession restores the still-valid (~30 min) access
// token but has no refresh token, so the FIRST 401 would call performLogout()
// and force a manual re-login + re-clock-in. If the user saved their
// credentials ("Remember for 30 days"), we silently re-authenticate in the
// BACKGROUND — before that 401 — to obtain a fresh refresh token and rewrite
// zt_auth.json in the new format, with NO login screen and NO interruption to
// tracking/attendance. Purely additive: uses the existing /auth/login-json
// endpoint and saveSession(); no backend or schema change.
// Silent credential-based re-auth (the user ticked "Remember for 30 days", so
// the password is sealed in safeStorage). Returns a STRUCTURED result so the
// caller can tell a recoverable blip apart from a dead end:
//   { ok: true }                          → fresh access + refresh token saved
//   { ok: false, reason: "no_creds" }     → nothing saved to re-auth with
//   { ok: false, reason: "rejected" }     → 401/403, password no longer valid
//   { ok: false, reason: "transient" }    → network / 5xx — safe to retry later
//   { ok: false, reason: "bad_response" } → 200 but tokens missing (server cfg)
// Only "no_creds"/"rejected" are terminal; the rest must NOT force a logout.
async function attemptCredentialLogin(reason = "migration") {
  const creds = loadSavedCredentials();
  if (!creds) {
    console.log(`[migrate] ${reason}: no saved credentials — cannot silently re-auth`);
    return { ok: false, reason: "no_creds" };
  }
  try {
    const fetchFn = await getFetch();
    const res = await fetchFn(`${API_BASE}/auth/login-json`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        // Required so the backend returns refresh_token in the body (desktop
        // clients don't get the HttpOnly cookie).
        "X-Client-Type": "desktop",
      },
      body: JSON.stringify({ email: creds.email, password: creds.password, remember: true }),
      timeout: 15000,
    });
    if (!res.ok) {
      // 401/403 = creds no longer valid (password changed) → terminal. 5xx /
      // timeout = transient. We never log out from in here — the caller does,
      // and only on a terminal reason; the existing access token keeps the user
      // working until it expires.
      const terminal = res.status === 401 || res.status === 403;
      console.warn(`[migrate] ${reason}: silent re-auth failed (status ${res.status}) — leaving current session intact`);
      return { ok: false, reason: terminal ? "rejected" : "transient" };
    }
    const data = await res.json();
    if (!data || !data.access_token || !data.refresh_token) {
      console.warn(`[migrate] ${reason}: login response missing tokens (refresh_token requires X-Client-Type: desktop)`);
      return { ok: false, reason: "bad_response" };
    }
    const u = data.user || {};
    saveSession({
      token:         data.access_token,
      refresh_token: data.refresh_token,
      name:          u.name  || (savedSession && savedSession.name)    || "",
      email:         u.email || creds.email,
      role:          u.role  || (savedSession && savedSession.role)    || "employee",
      user_id:       u.id    || (savedSession && savedSession.user_id) || "",
    });
    injectTokenToRenderer(data.access_token);
    console.log(`[migrate] ${reason}: silent re-auth OK — fresh refresh token obtained and zt_auth.json upgraded to new format`);
    return { ok: true };
  } catch (e) {
    console.warn(`[migrate] ${reason}: silent re-auth error (kept current session):`, e?.message);
    return { ok: false, reason: "transient" };
  }
}

// Backward-compatible boolean wrapper — migrateLegacySessionIfNeeded() and any
// other caller that only cares "did it work?" keep their existing contract.
async function loginWithSavedCredentials(reason = "migration") {
  const r = await attemptCredentialLogin(reason);
  return r.ok;
}

// One-time, non-blocking upgrade for a "half session" flagged by
// loadSavedSession. Safe to call fire-and-forget after the window has loaded —
// it never logs the user out and never touches tracking/attendance.
async function migrateLegacySessionIfNeeded() {
  if (!_sessionNeedsRefreshTokenMigration) return;
  _sessionNeedsRefreshTokenMigration = false; // run at most once per launch
  if (refreshToken) return; // a concurrent refresh already produced one
  console.log(
    "[migrate] Half session detected (access token present, no refresh token) — " +
    "attempting silent v1.0.7→v1.0.12 upgrade",
  );
  const ok = await loginWithSavedCredentials("v1.0.7→v1.0.12 session migration");
  if (!ok) {
    console.warn(
      "[migrate] Silent upgrade not completed — the user keeps working on the " +
      "existing access token; if it expires before a refresh token is obtained " +
      "(no saved credentials), they will be asked to log in once.",
    );
  }
}

// ─── AUTO-LAUNCH ──────────────────────────────────────────────────────────────
new AutoLaunch({ name: "Temveris", isHidden: true }).enable().catch(() => {});

// ─── AUTO-UPDATER ────────────────────────────────────────────────────────────
// Native electron-updater flow (VS Code / Slack / Discord style):
//   1. On launch we check Supabase for a newer build.
//   2. The renderer popup confirms with the user, then triggers
//      `autoUpdater.downloadUpdate()` over IPC — the download streams *inside*
//      Electron, never through the browser, so Chrome's "Dangerous file"
//      warning and SmartScreen prompts never appear.
//   3. When the download finishes, the renderer swaps to "Restart and Update".
//   4. That button calls `autoUpdater.quitAndInstall(true, true)` — NSIS runs
//      silently, replaces the binary, and relaunches Temveris. No Task
//      Manager, no manual installer, no Retry button.
//
// `autoDownload` is FALSE so the renderer drives when bytes start moving.
// `autoInstallOnAppQuit` stays TRUE as a safety net — if the user just quits
// after a download completes, NSIS still applies the update on next launch.

let updateAvailable = false;
let updateDownloaded = false;
let updateInfo = null;
let updaterDownloadInFlight = false;
// Handle for the periodic 4h update-check interval so it can be cleared on quit
// (previously the setInterval handle was discarded and the timer leaked).
let updateCheckTimer = null;

// Broadcast a single normalized status payload to the renderer. Every shape
// the renderer sees is `{ state, ... }` so the React state machine has one
// switch, not five. Safe to call before mainWindow exists — the send is just
// dropped.
function emitUpdaterStatus(state, extra = {}) {
  try {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send("updater:status", { state, ...extra });
    }
  } catch {}
}

// ─── [BlinkInvestigation] MAIN-PROCESS DIAGNOSTIC LOGGING ────────────────────
// TEMPORARY. Records every main-process action that can blank or reload the
// renderer (loadURL, logout, nav-cage redirect, confirmed-empty-token logout)
// to BOTH the main stdout log AND the renderer's localStorage ring buffer (via
// window.__blinkLog), so support gets ONE unified export from DevTools. Each
// entry carries ts / userId / electronVersion / route, matching the renderer
// schema. Returns a promise so a caller about to loadURL can await it and
// guarantee the entry is persisted before the renderer is torn down. Remove
// once the root cause (A–E) is confirmed.
function blinkLogMain(event, detail = {}) {
  let route = "";
  try {
    route =
      mainWindow && !mainWindow.isDestroyed()
        ? mainWindow.webContents.getURL()
        : "";
  } catch {}
  const entry = {
    ts: new Date().toISOString(),
    event,
    source: "main",
    userId: (savedSession && savedSession.user_id) || "unknown",
    electronVersion: app.getVersion(),
    route,
    ...detail,
  };
  try {
    console.log("[BlinkInvestigation]", event, JSON.stringify(entry));
  } catch {}
  try {
    if (mainWindow && !mainWindow.isDestroyed()) {
      return mainWindow.webContents
        .executeJavaScript(
          `window.__blinkLog && window.__blinkLog(${JSON.stringify(entry)});`,
        )
        .catch(() => {});
    }
  } catch {}
  return Promise.resolve();
}

function setupAutoUpdater() {
  // Only run in a packaged build — dev mode has no published release to check
  // and calling checkForUpdates() under `electron .` throws noisy errors.
  if (!app.isPackaged) {
    console.log("[Updater] Skipping auto-update check in dev mode.");
    return;
  }

  // Lazy-require — see top-of-file note. Safe here because we've gated on
  // app.isPackaged and we're inside app.whenReady → setupAutoUpdater.
  try {
    autoUpdater = require("electron-updater").autoUpdater;
  } catch (e) {
    console.warn("[Updater] failed to load electron-updater:", e.message);
    return;
  }
  // Renderer drives the download; main keeps the post-quit install safety net.
  autoUpdater.autoDownload = false;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.logger = console;

  autoUpdater.on("checking-for-update", () => {
    console.log("[Updater] Checking for updates");
    emitUpdaterStatus("checking");
  });

  autoUpdater.on("update-available", (info) => {
    console.log(`[Updater] Update available v${info.version}`);
    updateAvailable = true;
    updateInfo = info;
    emitUpdaterStatus("available", { info });
  });

  autoUpdater.on("update-not-available", (info) => {
    console.log("[Updater] App is up to date");
    emitUpdaterStatus("not-available", { info });
  });

  autoUpdater.on("download-progress", (progress) => {
    const pct = Math.round(progress.percent || 0);
    const kbps = Math.round((progress.bytesPerSecond || 0) / 1024);
    console.log(`[Updater] Downloading ${pct}%`);
    if (tray) tray.setToolTip(`Temveris — Updating ${pct}%`);
    emitUpdaterStatus("progress", {
      percent: pct,
      bytesPerSecond: progress.bytesPerSecond || 0,
      transferred: progress.transferred || 0,
      total: progress.total || 0,
      kbps,
    });
  });

  autoUpdater.on("update-downloaded", (info) => {
    console.log(`[Updater] Download complete v${info.version}`);
    updateDownloaded = true;
    updateInfo = info;
    updaterDownloadInFlight = false;
    // Tray menu surfaces the "Restart & install update v…" item once a build
    // is staged on disk — keeps the legacy entry-point working.
    updateTray();
    emitUpdaterStatus("downloaded", { info });
    if (Notification.isSupported()) {
      new Notification({
        title: "Temveris update ready",
        body: `Version ${info.version} is ready to install. Click "Restart and Update" in the app.`,
        icon: path.join(__dirname, "assets", "icon.png"),
      }).show();
    }
  });

  autoUpdater.on("error", (err) => {
    const message = err?.message || String(err);
    console.warn("[Updater] Error:", message);
    updaterDownloadInFlight = false;
    updateTray(); // restore the tooltip in case it was showing a %
    // Background update failures (e.g. net::ERR_NETWORK_IO_SUSPENDED when the
    // machine sleeps mid-check) must fail silently — no user-facing popup.
    // Logged above for debugging. User-initiated download failures still
    // surface via the updater:download catch below, so the "Update now"
    // button never gets stuck.
    // emitUpdaterStatus("error", { message });
  });

  // Initial check after a short delay so startup isn't slowed, then every 4 h.
  setTimeout(() => {
    autoUpdater.checkForUpdates().catch(() => {});
  }, 10_000);

  updateCheckTimer = setInterval(
    () => {
      autoUpdater.checkForUpdates().catch(() => {});
    },
    4 * 60 * 60 * 1000,
  );
}

// Shared "tear everything down, then quit-and-install" path used by both the
// renderer button and the tray menu item. Centralising it guarantees we stop
// the screenshot + heartbeat + browser-tracker timers BEFORE NSIS tries to
// overwrite the running binary — otherwise a stray timer firing mid-swap can
// keep file handles open on Windows and stall the install.
function performQuitAndInstall() {
  if (!autoUpdater || !updateDownloaded) {
    console.warn("[Updater] quitAndInstall requested but no update is staged");
    emitUpdaterStatus("error", { message: "No update is staged for install." });
    return;
  }
  console.log("[Updater] Installing update");
  emitUpdaterStatus("installing");

  isQuiting = true; // suppress the "still clocked in" warning on window close
  try { stopTracking(); } catch {}
  try { clearInterval(statusTimer); } catch {}
  try { clearInterval(tokenTimer); } catch {}
  try { clearInterval(networkMonitorTimer); } catch {}

  // Tiny delay so the renderer's "Installing update…" state paints before the
  // window is torn down. 200 ms is imperceptible to a human but covers one
  // React commit + a frame.
  setTimeout(() => {
    console.log("[Updater] Restarting application");
    try {
      // isSilent=true, isForceRunAfter=true → NSIS runs without UI and
      // Temveris relaunches itself once the new binary is in place.
      autoUpdater.quitAndInstall(true, true);
    } catch (e) {
      console.warn("[Updater] quitAndInstall failed:", e?.message);
      isQuiting = false; // restore so the user can interact again
      emitUpdaterStatus("error", { message: e?.message || "Install failed" });
    }
  }, 200);
}

// ─── IPC HANDLERS (registered BEFORE app.whenReady) ──────────────────────────

// Preload reads session synchronously — must be registered early
ipcMain.on("preload:get-session", (event) => {
  // savedSession is already populated by loadSavedSession() before loadURL()
  event.returnValue = savedSession || null;
});

// React pushes session after login — fastest possible path (no polling delay)
ipcMain.on("auth:save-session", (_event, sessionData) => {
  if (!sessionData || !sessionData.token) return;
  console.log(
    "[ipc] auth:save-session →",
    sessionData.name || sessionData.email,
  );
  saveSession(sessionData);
  startStatusPolling();
  updateTray();
  // Tell the dashboard to pull fresh clock/attendance state immediately on
  // login instead of waiting for its bootstrap/poll cadence. (The renderer's
  // own mount-time bootstrap also covers login; this is the belt-and-suspenders
  // push for the already-mounted re-login case.)
  pushForceRefresh("login");
});

// React triggers logout from inside the SPA. The renderer tells us whether it
// is navigating itself:
//   • rendererNavigates: true  — ElectronDashboard's Logout button already did a
//     smooth React-Router navigate("/login"). Main must NOT hard-reload the
//     window (that reload is the white-screen glitch) — it only tears down the
//     session + tracking.
//   • omitted/false — the renderer can't navigate itself (e.g. the 401
//     interceptor via redirectToLogin). Main drives the window to /login.
ipcMain.on("auth:logout", (_event, opts) => {
  const rendererNavigates = !!(opts && opts.rendererNavigates);
  console.log("[ipc] auth:logout (rendererNavigates=%s)", rendererNavigates);
  performLogout({ navigate: !rendererNavigates });
});

// Save email+password after a successful login when "Remember" is checked.
ipcMain.on("auth:save-credentials", (_event, creds) => {
  saveCredentials(creds);
});

// Synchronous read used by the renderer to prefill the login form on mount.
// sendSync is acceptable here because the JSON file is tiny and decrypt is
// near-instant — the renderer is blocked for well under a frame.
ipcMain.on("auth:get-credentials", (event) => {
  event.returnValue = loadSavedCredentials();
});

// Renderer calls this when the user submits the login form with "Remember"
// unchecked, so a previously-saved password is forgotten.
ipcMain.on("auth:clear-credentials", () => {
  clearCredentials();
});

// Silent token refresh — invoked by the renderer's axios 401 interceptor.
// Returns { access_token } on success or null when the session is over. The
// refresh token never leaves the main process.
ipcMain.handle("auth:refresh", async () => {
  return await refreshAccessToken();
});

// React pushes clock-in/out status immediately (no 60s poll lag)
ipcMain.on("tracking:status-update", (_event, status) => {
  const valid = ["working", "on_break", "not_clocked_in"];
  if (!valid.includes(status)) return;
  console.log("[ipc] tracking:status-update →", status);
  if (status === currentStatus) return;
  currentStatus = status;
  if (currentStatus === "working" && !isTracking) startTracking();
  else if (currentStatus !== "working" && isTracking) stopTracking();
  updateTray();
});

// Window controls
ipcMain.on("window:minimize", () => mainWindow?.minimize());
ipcMain.on("window:close", () => {
  if (!isQuiting) mainWindow?.hide();
});

// ─── UPDATER IPC ─────────────────────────────────────────────────────────────
// All three handlers are no-ops in dev mode (autoUpdater is null until
// setupAutoUpdater runs inside a packaged build) — that keeps `electron .`
// launches from throwing when the renderer subscribes on mount.

ipcMain.handle("updater:check", async () => {
  if (!autoUpdater) {
    return { ok: false, reason: "updater-not-initialised" };
  }
  try {
    console.log("[Updater] Checking for updates (manual)");
    const result = await autoUpdater.checkForUpdates();
    return { ok: true, version: result?.updateInfo?.version || null };
  } catch (e) {
    console.warn("[Updater] manual check failed:", e?.message);
    return { ok: false, reason: e?.message || "check-failed" };
  }
});

ipcMain.handle("updater:download", async () => {
  if (!autoUpdater) {
    emitUpdaterStatus("error", { message: "Updater not initialised (dev mode?)" });
    return { ok: false, reason: "updater-not-initialised" };
  }
  if (updateDownloaded) {
    // Re-emit downloaded so a freshly-mounted renderer can pick up the
    // ready-to-install state without having to wait for another event.
    emitUpdaterStatus("downloaded", { info: updateInfo });
    return { ok: true, alreadyDownloaded: true };
  }
  if (updaterDownloadInFlight) {
    return { ok: true, alreadyInFlight: true };
  }
  try {
    updaterDownloadInFlight = true;
    console.log("[Updater] Downloading update");
    await autoUpdater.downloadUpdate();
    return { ok: true };
  } catch (e) {
    updaterDownloadInFlight = false;
    console.warn("[Updater] downloadUpdate failed:", e?.message);
    emitUpdaterStatus("error", { message: e?.message || "Download failed" });
    return { ok: false, reason: e?.message || "download-failed" };
  }
});

ipcMain.on("updater:quit-and-install", () => {
  performQuitAndInstall();
});

// ─── APP MENU ─────────────────────────────────────────────────────────────────
function buildAppMenu() {
  return Menu.buildFromTemplate([
    {
      label: "Temveris",
      submenu: [
        { label: "Go to Login", click: () => performLogout() },
        { type: "separator" },
        {
          label: "Quit",
          accelerator: "CmdOrCtrl+Q",
          click: () => quitApp(),
        },
      ],
    },
    {
      label: "Developer",
      submenu: [
        {
          label: "Toggle DevTools",
          accelerator: "F12",
          click: () => {
            if (!mainWindow) return;
            if (mainWindow.webContents.isDevToolsOpened()) {
              mainWindow.webContents.closeDevTools();
            } else {
              mainWindow.webContents.openDevTools({ mode: "detach" });
            }
          },
        },
        {
          label: "Reload",
          accelerator: "CmdOrCtrl+R",
          click: () => mainWindow?.webContents.reload(),
        },
      ],
    },
  ]);
}

function quitApp() {
  isQuiting = true;
  stopTracking();
  clearInterval(statusTimer);
  clearInterval(tokenTimer);
  app.quit();
}

// ─── WINDOW ───────────────────────────────────────────────────────────────────
function createWindow() {
  Menu.setApplicationMenu(buildAppMenu());

  // On Windows, prefer the multi-size .ico so the taskbar / title bar / Alt-Tab
  // thumbnail all render crisply. Fall back to icon.png elsewhere (mac/linux).
  const winIcon =
    process.platform === "win32"
      ? path.join(__dirname, "assets", "icon.ico")
      : path.join(__dirname, "assets", "icon.png");

  // Compact popup-style window — sized to hug the clock-in card exactly so
  // there's no gradient gutter / "background popup" around the content. The
  // /electron React page now uses a card that fills 100% of the window, so
  // these dimensions are a 1:1 frame around it.
  mainWindow = new BrowserWindow({
    width: 680,
    height: 500,
    minWidth: 600,
    minHeight: 460,
    maxWidth: 780,
    maxHeight: 580,
    resizable: false,
    maximizable: false,
    fullscreenable: false,
    icon: winIcon,
    title: "Temveris",
    autoHideMenuBar: true,
    frame: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,                 // preload only uses require("electron") → sandbox-safe
      devTools: !app.isPackaged,     // DevTools available in dev, disabled in packaged builds
    },
  });

  // ── STAMP APP VERSION INTO THE USER-AGENT ────────────────────────────────
  // Must run BEFORE the first loadURL() so the very first navigation carries
  // it. The React /electron page parses `temveris-desktop/X.Y.Z` out of
  // navigator.userAgent to (a) gate the /desktop/latest fetch and (b) detect
  // the installed version for the in-app "Update available" card
  // (ElectronDashboard.jsx: extractDesktopVersionFromUA + the mount bootstrap).
  // Without this token the update check never even fires and the card's
  // version-comparison path is dead. We APPEND (not replace) so the page still
  // looks like a normal Chrome/Electron client to every other consumer.
  try {
    const baseUA = mainWindow.webContents.getUserAgent();
    if (!/temveris-desktop\//i.test(baseUA)) {
      mainWindow.webContents.setUserAgent(
        `${baseUA} temveris-desktop/${app.getVersion()}`,
      );
      console.log(
        `[ua] stamped user-agent → temveris-desktop/${app.getVersion()}`,
      );
    }
  } catch (e) {
    console.warn("[ua] failed to stamp user-agent:", e && e.message);
  }

  // Right-click → show a minimal context menu with Inspect Element
  mainWindow.webContents.on("context-menu", (e, params) => {
    e.preventDefault();
    Menu.buildFromTemplate([
      {
        label: "Inspect Element",
        click: () => mainWindow.webContents.inspectElement(params.x, params.y),
      },
      { type: "separator" },
      {
        label: "Reload",
        click: () => mainWindow.webContents.reload(),
      },
    ]).popup({ window: mainWindow });
  });

  // Count keystrokes for activity tracking.
  // F12 / Ctrl+Shift+I → toggle DevTools (like Chrome).
  // Ctrl+U (view-source) stays blocked — not useful in Electron.
  mainWindow.webContents.on("before-input-event", (_e, input) => {
    // In-window counting is only a fallback for when the global hook isn't
    // running — when it is, recordGlobalKeydown already captures this key, so
    // counting here too would double-work for a value we discard. Share the
    // same burst cap so a paste spike reads identically on both paths.
    if (!uioStarted && shouldCountAppInput(input) && allowKeyBurstCount()) {
      keyCountApp++;
    }
    const isDevTools =
      input.key === "F12" ||
      (input.control && input.shift && input.key === "I");
    if (isDevTools) {
      _e.preventDefault(); // suppress the default so we control it
      if (mainWindow.webContents.isDevToolsOpened()) {
        mainWindow.webContents.closeDevTools();
      } else {
        mainWindow.webContents.openDevTools({ mode: "detach" });
      }
      return;
    }
    // Block view-source in all modes
    if (input.control && input.key === "U") _e.preventDefault();
  });

  // ── THE CRITICAL FIX ───────────────────────────────────────────────────────
  // loadSavedSession() MUST run before loadURL() so that when the preload
  // fires "preload:get-session" synchronously, savedSession is already set.
  //
  // OLD (broken): loadURL first → preload fires → savedSession is null → login
  // NEW (fixed):  load session → loadURL → preload fires → session ready → dashboard
  // ──────────────────────────────────────────────────────────────────────────
  const session = loadSavedSession(); // ← sets savedSession AND authToken
  if (session) {
    console.log("[app] Restoring session → /electron");
    mainWindow.loadURL(WEBSITE_URL + "/electron");
  } else {
    console.log("[app] No session → /login");
    mainWindow.loadURL(WEBSITE_URL + "/login");
  }

  // Fire-and-forget, non-blocking: if loadSavedSession restored a v1.0.7-format
  // session (access token, no refresh token), silently obtain a refresh token
  // from saved credentials BEFORE the first 401. The window already shows the
  // dashboard via the still-valid access token; this just upgrades the session
  // in the background. Never logs out, never touches tracking/attendance.
  migrateLegacySessionIfNeeded().catch(() => {});

  // After a full page load (hard navigation or refresh)
  mainWindow.webContents.on("did-finish-load", async () => {
    // Redirect raw API "Not Found" JSON to login
    try {
      const txt = await mainWindow.webContents.executeJavaScript(
        "document.body && document.body.innerText",
      );
      if (txt && txt.includes('"detail":"Not Found"')) {
        console.warn('[nav] did-finish-load saw raw \'"detail":"Not Found"\' body — reloading to /login');
        await blinkLogMain("main-loadurl-notfound-redirect", { target: "/login" });
        mainWindow.loadURL(WEBSITE_URL + "/login");
        return;
      }
    } catch {}
    scheduleExtractSession();
    // Cold open / reload / session restore just finished loading the SPA — tell
    // the dashboard to pull current clock/attendance state now instead of
    // waiting for the 0-10s poll stagger + bootstrap cadence.
    pushForceRefresh("restore");
  });

  // After SPA client-side navigation (React Router pushState/replaceState)
  // This catches the login form submit redirect to /electron
  mainWindow.webContents.on("did-navigate-in-page", () => {
    scheduleExtractSession();
  });

  // Keep window title clean
  mainWindow.webContents.on("page-title-updated", (e) => {
    e.preventDefault();
    mainWindow.setTitle("Temveris");
  });

  // Routes that are allowed to render INSIDE the Electron window. The desktop
  // shell is intentionally locked down to the slim /electron tracker plus the
  // auth flows needed to reach it. Any other route (dashboard, admin,
  // reports, settings, leave management, my-team, profile, etc.) is opened
  // in the user's default browser — the full website lives there.
  const ALLOWED_PREFIXES = [
    "/electron",
    "/login",
    "/forgot-password",
    "/set-password",
    "/reset-password",
  ];

  const isAllowedPath = (pathname) => {
    if (!pathname || pathname === "" || pathname === "/") return true;
    return ALLOWED_PREFIXES.some(
      (prefix) => pathname === prefix || pathname.startsWith(prefix + "/"),
    );
  };

  // Where we send the window back to when something tries to escape the cage.
  // After login we want /electron; before login we want /login.
  const safeLandingPath = () => (authToken ? "/electron" : "/login");

  // Intercept full-page navigations (clicks, redirects, address-bar loads)
  // and bounce anything outside the allow-list to the user's real browser.
  mainWindow.webContents.on("will-navigate", (_e, url) => {
    try {
      const u = new URL(url);

      // Ignore navigations to other origins (handled by setWindowOpenHandler / OS)
      if (u.origin !== new URL(WEBSITE_URL).origin) return;

      const p = u.pathname;

      if ((p === "" || p === "/") && !authToken) {
        _e.preventDefault();
        blinkLogMain("main-loadurl-willnav-root", { from: p, target: "/login" });
        mainWindow.loadURL(WEBSITE_URL + "/login");
        return;
      }

      if (!isAllowedPath(p)) {
        _e.preventDefault();
        blinkLogMain("main-navcage-willnav-external", { path: p });
        shell.openExternal(url);
      }
    } catch {}
  });

  // SPA navigations (react-router pushState) don't trigger will-navigate.
  // did-navigate-in-page fires AFTER the URL change — if the new path is
  // not whitelisted, kick the user back to a safe page and open the
  // requested page in their browser instead.
  mainWindow.webContents.on("did-navigate-in-page", (_e, url) => {
    try {
      const u = new URL(url);
      if (u.origin !== new URL(WEBSITE_URL).origin) return;

      if (!isAllowedPath(u.pathname)) {
        console.log(
          `[nav] in-page navigation to out-of-cage path "${u.pathname}" — ` +
          `opening externally and returning to ${safeLandingPath()}`,
        );
        // [BlinkInvestigation] candidate D — the nav-cage hard-reloads the
        // window when the SPA navigates in-page to a non-whitelisted path.
        blinkLogMain("main-navcage-redirect", {
          path: u.pathname,
          target: safeLandingPath(),
        });
        shell.openExternal(url);
        mainWindow.loadURL(WEBSITE_URL + safeLandingPath());
      }
    } catch {}
  });

  // External links (target=_blank, window.open) → system browser
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  // X button — SYNCHRONOUS handler so e.preventDefault() is always honoured.
  // Async handlers on BrowserWindow 'close' are unreliable: if the first
  // await yields before e.preventDefault() takes effect in some Electron
  // builds the window closes before the dialog can appear.
  // Solution: prevent synchronously, then kick off async logic via setImmediate.
  mainWindow.on("close", (e) => {
    if (isQuiting) return;
    e.preventDefault(); // ← must be synchronous, no await above this
    setImmediate(() => handleWindowClose());
  });
}

// ─── CLOSE HANDLER (async, runs after e.preventDefault is guaranteed) ─────────
async function handleWindowClose() {
  if (!mainWindow) return;

  // Fast path: no token → just hide
  if (!authToken) {
    mainWindow.hide();
    return;
  }

  // Check local tracking state first (instant, no network).
  // isTracking is the most reliable signal: it is set to true only when the
  // screenshot/heartbeat timers are actually running.
  const locallyTracking = isTracking;

  // If already stopped locally, do a quick server confirm to be safe,
  // but skip the dialog on network error (don't false-positive after clock-out).
  if (!locallyTracking) {
    try {
      const fetchFn = await getFetch();
      const res = await fetchFn(`${API_BASE}/tracking/ping`, {
        headers: { Authorization: `Bearer ${authToken}` },
        timeout: 3000,
      });
      if (res.status === 200) {
        const data = await res.json();
        if (data.clocked_in) {
          // Server disagrees — user is still clocked in, reconcile and warn
          currentStatus = data.on_break ? "on_break" : "working";
          if (!isTracking) startTracking();
          updateTray();
          // fall through to the dialog below
        } else {
          // Server confirms not clocked in → hide silently
          mainWindow.hide();
          return;
        }
      } else {
        // Non-200 (e.g. 401) → just hide
        mainWindow.hide();
        return;
      }
    } catch {
      // Network unavailable and locally not tracking → assume safe to hide
      mainWindow.hide();
      return;
    }
  }

  // At this point the user is clocked in (locally or confirmed by server).
  // For a belt-and-suspenders check, if locally tracking, do a quick ping
  // to see if the user already clocked out from the web dashboard.
  if (locallyTracking) {
    try {
      const fetchFn = await getFetch();
      const res = await fetchFn(`${API_BASE}/tracking/ping`, {
        headers: { Authorization: `Bearer ${authToken}` },
        timeout: 3000,
      });
      if (res.status === 200) {
        const data = await res.json();
        if (!data.clocked_in) {
          // User already clocked out from dashboard — sync and hide silently
          currentStatus = "not_clocked_in";
          stopTracking();
          updateTray();
          mainWindow.hide();
          return;
        }
      }
    } catch {
      // Network error while locally tracking → be conservative, show the dialog
    }
  }

  // ── Show the warning dialog ──────────────────────────────────────────────
  const choice = dialog.showMessageBoxSync(mainWindow, {
    type: "warning",
    buttons: ["Go back", "Clock out & close"],
    defaultId: 0,
    cancelId: 0,
    title: "You are still clocked in",
    message: "Please clock out and close the application.",
    detail:
      "You are currently tracked as working. Closing Temveris without " +
      "clocking out can leave an open session if your system shuts down " +
      "or the network drops.\n\n" +
      "If you continue, Temveris will clock you out automatically as a " +
      "safety measure.",
  });

  if (choice === 0) return; // "Go back" — leave window open

  // "Clock out & close" — attempt clock-out, then hide to tray. Only claim
  // "clocked out" / stop tracking when the backend actually closed the
  // session. If a reason is required (below hours) or the server is
  // unreachable, we stay clocked in and tracking continues — the worker must
  // clock out from the dashboard (with a reason). We never falsely report a
  // clock-out that didn't happen.
  const result = await requestClockOut(8000);
  if (result === "closed" || result === "already_closed") {
    stopTracking();
    if (result === "closed") {
      showNotification("Temveris", "Clocked out. Tracking stopped.");
    }
  } else if (result === "open") {
    showNotification(
      "Temveris",
      "Still clocked in — open Temveris and use Clock Out to provide a reason.",
    );
  } else {
    showNotification(
      "Temveris",
      "Could not reach server — you are still clocked in. Clock out when online.",
    );
  }

  mainWindow.hide();
}

// ─── SESSION EXTRACTION (localStorage → main process) ────────────────────────
// Debounced 400ms so rapid SPA navigation doesn't hammer this.
// This is the FALLBACK path. Primary path is auth:save-session IPC from React.
function scheduleExtractSession() {
  clearTimeout(extractDebounce);
  extractDebounce = setTimeout(extractSession, 400);
}

async function extractSession() {
  if (!mainWindow) return;
  try {
    const s = await mainWindow.webContents.executeJavaScript(`
      (function() {
        var t = localStorage.getItem("zt_token") || localStorage.getItem("token") || "";
        if (!t) return null;
        return {
          token:   t,
          name:    localStorage.getItem("name")    || "",
          email:   localStorage.getItem("email")   || "",
          role:    localStorage.getItem("role")     || "employee",
          user_id: localStorage.getItem("user_id") || "",
        };
      })()
    `);

    if (s && s.token && s.token !== authToken) {
      // New token found in localStorage — user just logged in via the web form
      emptyTokenStreak = 0;
      console.log("[extract] New login detected:", s.name || s.email);
      saveSession(s);
      startStatusPolling();
      updateTray();
    } else if (s && s.token) {
      // Token present and matches what we hold — healthy steady state.
      emptyTokenStreak = 0;
    } else if (!s && authToken) {
      // Token missing from localStorage while we still hold one. This is the
      // genuine "React logged out" signal — BUT it's also the transient state
      // during a silent token refresh (the renderer momentarily rewrites
      // zt_token) or a stray clearAuthStorage(). Confirm the empty state
      // persists across EMPTY_TOKEN_LOGOUT_THRESHOLD consecutive checks before
      // logging out, so a refresh race never reloads the window / clocks the
      // user out.
      emptyTokenStreak += 1;
      console.warn(
        `[extract] token missing (streak ${emptyTokenStreak}/${EMPTY_TOKEN_LOGOUT_THRESHOLD})` +
        (emptyTokenStreak >= EMPTY_TOKEN_LOGOUT_THRESHOLD
          ? " — confirmed, logging out"
          : " — deferring (likely a mid-refresh race)"),
      );
      if (emptyTokenStreak >= EMPTY_TOKEN_LOGOUT_THRESHOLD) {
        emptyTokenStreak = 0;
        await blinkLogMain("main-extract-logout", {
          note: `token empty across ${EMPTY_TOKEN_LOGOUT_THRESHOLD} checks — performing logout`,
        });
        await performLogout();
      } else {
        await blinkLogMain("main-extract-empty-token", {
          streak: emptyTokenStreak,
          threshold: EMPTY_TOKEN_LOGOUT_THRESHOLD,
          note: "token missing — deferring (likely mid-refresh)",
        });
        // Re-check shortly so a real logout still resolves quickly instead of
        // waiting for the next 60s poll.
        setTimeout(() => scheduleExtractSession(), 1500);
      }
    }
  } catch {}
}

// ─── LOGOUT ───────────────────────────────────────────────────────────────────
// `navigate` controls whether main hard-navigates the window to /login:
//   • false — the renderer already navigated (React-Router) to the login view,
//     smoothly and in-document. A hard loadURL("/login") here would tear down
//     that freshly-rendered login screen and re-fetch the entire remote SPA over
//     the network, blanking the window white for the round-trip. That redundant
//     reload IS the logout glitch. So for renderer-initiated logout we only do
//     the session/tracking teardown and let the renderer own navigation.
//   • true (default) — main-initiated logout (tray, empty-token watchdog,
//     refresh failure): the renderer isn't cooperating, so main must drive the
//     window to /login itself.
async function performLogout({ navigate = true } = {}) {
  stopTracking();

  // Best-effort server-side revoke so the persistent refresh token can't be
  // reused after sign-out. Fire before clearSession() wipes it; never block
  // local logout on the network.
  const tokenToRevoke = refreshToken;
  if (tokenToRevoke) {
    try {
      const fetchFn = await getFetch();
      await fetchFn(`${API_BASE}/auth/logout`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Client-Type": "desktop" },
        body: JSON.stringify({ refresh_token: tokenToRevoke }),
        timeout: 6000,
      });
    } catch (e) {
      console.warn("[logout] server revoke failed (continuing):", e?.message);
    }
  }

  clearSession();
  currentStatus = "not_clocked_in";
  updateTray();

  if (!mainWindow) return;

  // Renderer-initiated logout: the SPA already cleared its own localStorage and
  // navigated to /login. Skip the hard reload (and the now-redundant storage
  // wipe) — doing them would blank the window and undo the smooth transition.
  if (!navigate) return;

  mainWindow.show();
  mainWindow.focus();
  try {
    // ✅ Clear session keys ONLY — preserve zt_remembered_email and user prefs
    await mainWindow.webContents.executeJavaScript(`
      (function() {
        var preserveKeys = ['zt_remembered_email', 'zt_theme_dark', 'zt_lang_chosen'];
        var preserved = {};
        preserveKeys.forEach(function(k) {
          var v = localStorage.getItem(k);
          if (v !== null) preserved[k] = v;
        });
        localStorage.clear();
        Object.keys(preserved).forEach(function(k) {
          localStorage.setItem(k, preserved[k]);
        });
      })();
    `);
  } catch {}
  // [BlinkInvestigation] candidate E — main-process logout reloads to /login.
  // Await so the entry is persisted in the renderer buffer before teardown.
  await blinkLogMain("main-logout-loadurl", { target: "/login" });
  mainWindow.loadURL(WEBSITE_URL + "/login");
}

// ─── TRAY ─────────────────────────────────────────────────────────────────────
const TRAY_STATUS = {
  working: { label: "Working — Tracking Active", tip: "Temveris · Working" },
  on_break: {
    label: "On Break — Tracking Paused",
    tip: "Temveris · On Break",
  },
  not_clocked_in: { label: "Not Clocked In", tip: "Temveris" },
};

function createTray() {
  const iconPath = path.join(__dirname, "assets", "icon.png");
  const icon = fs.existsSync(iconPath)
    ? nativeImage.createFromPath(iconPath).resize({ width: 16, height: 16 })
    : nativeImage.createEmpty();

  tray = new Tray(icon);
  tray.on("click", () => {
    mainWindow?.show();
    mainWindow?.focus();
  });
  tray.on("double-click", () => {
    mainWindow?.show();
    mainWindow?.focus();
  });
  updateTray();
}

function _formatAgo(ts) {
  if (!ts) return "never";
  const secs = Math.max(0, Math.floor((Date.now() - ts) / 1000));
  if (secs < 60)  return secs + "s ago";
  if (secs < 3600) return Math.floor(secs / 60) + "m ago";
  return Math.floor(secs / 3600) + "h " + Math.floor((secs % 3600) / 60) + "m ago";
}

// Diagnostic submenu — surfaces live tracker state without requiring a
// console attach. Right-click tray → Diagnostics shows last heartbeat,
// last input, and the auth+network state.
function _buildDiagnosticsSubmenu() {
  return [
    { label: "Tracker:        " + (isTracking ? "RUNNING" : "stopped"), enabled: false },
    { label: "Auth:           " + (authToken ? "signed in" : "no token"), enabled: false },
    { label: "Network:        " + (offlineSince ? "OFFLINE" : "online"),  enabled: false },
    { type: "separator" },
    { label: "Last heartbeat: " + _formatAgo(lastDiagnostics.lastHeartbeatAt), enabled: false },
    { label: "Last input:     " + _formatAgo(lastDiagnostics.lastActiveAt),    enabled: false },
    { type: "separator" },
    {
      label: "Force heartbeat now",
      click: () => sendHeartbeat().catch(() => {}),
    },
    {
      label: "Refresh settings",
      click: () => fetchAndApplySettings().catch(() => {}),
    },
  ];
}

function updateTray() {
  if (!tray) return;
  const s = TRAY_STATUS[currentStatus] || TRAY_STATUS.not_clocked_in;

  // "Restart & install update vX.Y.Z" only appears after electron-updater
  // signals update-downloaded. Without this, a tray-resident app (where
  // window-all-closed is a no-op) would queue an install but never get an
  // app.quit() opportunity to apply it — that's why employees stay stuck on
  // old versions even when an update *has* downloaded.
  const updateMenuItems = updateDownloaded && updateInfo
    ? [
        {
          label: `Restart & install update v${updateInfo.version}`,
          // Go through the shared helper so tracking timers stop cleanly and
          // the renderer (if visible) gets the "Installing update…" state for
          // the few ms before the window tears down.
          click: () => performQuitAndInstall(),
        },
        { type: "separator" },
      ]
    : [];

  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: "Temveris  v" + app.getVersion(), enabled: false },
      { type: "separator" },
      { label: s.label, enabled: false },
      { type: "separator" },
      {
        label: "Open",
        click: () => {
          mainWindow?.show();
          mainWindow?.focus();
        },
      },
      { label: "Diagnostics", submenu: _buildDiagnosticsSubmenu() },
      { type: "separator" },
      ...updateMenuItems,
      ...(authToken
        ? [{ label: "Logout", click: () => performLogout() }]
        : [
            {
              label: "Login",
              click: () => {
                mainWindow?.show();
                mainWindow?.loadURL(WEBSITE_URL + "/login");
              },
            },
          ]),
      { type: "separator" },
      { label: "Quit Temveris", click: () => quitApp() },
    ]),
  );

  tray.setToolTip(s.tip);
}

function showNotification(title, body) {
  try {
    if (Notification.isSupported())
      new Notification({ title, body, silent: true }).show();
  } catch {}
}

// ─── SILENT TOKEN REFRESH ──────────────────────────────────────────────────
// Push a freshly-minted access token back into the renderer's localStorage so
// React's route guards + the axios request interceptor immediately use it.
function injectTokenToRenderer(tok) {
  if (!mainWindow || !tok) return;
  const js = `try {
    localStorage.setItem("zt_token", ${JSON.stringify(tok)});
    localStorage.setItem("token", ${JSON.stringify(tok)});
  } catch (e) {}`;
  mainWindow.webContents.executeJavaScript(js).catch(() => {});
  // The access token just changed (silent refresh / migration) — tell the
  // renderer to re-sync immediately so a post-refresh dashboard never shows
  // stale clock state and the new token is used on the very next request.
  pushForceRefresh("token-refresh");
}

// Main → renderer "sync now" push. Fired the instant a session-state moment the
// main process owns occurs — session restore, login, silent token refresh,
// wake-from-sleep — so the dashboard re-fetches employee state IMMEDIATELY
// rather than waiting for its 15s poll cadence. Sent once now and re-sent
// ~600ms later so a renderer whose onForceRefresh listener registered a beat
// after the first send still receives it; the renderer de-dupes the two (and
// any overlapping bootstrap/WS trigger) into a single /employees/me fetch.
function pushForceRefresh(reason) {
  const send = () => {
    try {
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send("tracking:force-refresh", reason);
      }
    } catch {}
  };
  send();
  setTimeout(send, 600);
}

// ─── PROACTIVE TOKEN REFRESH + TRANSIENT BACKOFF ───────────────────────────────
// The access token is short-lived (~30 min). Historically it was only ever
// renewed REACTIVELY, on a 401 — so it was always allowed to fully expire first,
// and any transient failure at that exact moment (and these cluster: sleep/wake,
// VPN reconnect, captive portals) left the app holding an expired token with no
// slack. These helpers renew it a few minutes EARLY and actively retry after a
// blip, so the token is almost never expired and a 401-driven logout becomes the
// rare exception. Everything funnels through the single-flight
// refreshAccessToken() below, so the single-use rotating token is never spent
// twice.

// Decode a JWT's `exp` (seconds) → epoch ms, or null if unreadable. Pure
// base64url decode of the payload segment — no signature check (the server
// already validated it; we only need the expiry to schedule around it).
function decodeJwtExpMs(token) {
  try {
    const seg = String(token || "").split(".")[1];
    if (!seg) return null;
    const b64 = seg.replace(/-/g, "+").replace(/_/g, "/");
    const payload = JSON.parse(Buffer.from(b64, "base64").toString("utf-8"));
    return typeof payload.exp === "number" ? payload.exp * 1000 : null;
  } catch {
    return null;
  }
}

function clearProactiveTimers() {
  if (proactiveRefreshTimer) { clearTimeout(proactiveRefreshTimer); proactiveRefreshTimer = null; }
  if (refreshRetryTimer)     { clearTimeout(refreshRetryTimer);     refreshRetryTimer = null; }
  refreshRetryAttempt = 0;
}

// (Re)schedule the next proactive refresh based on the CURRENT access token's
// expiry. Called on every successful token change (saveSession), at startup,
// and on resume. Idempotent — always clears the prior timer + resets backoff
// first, so a fresh token means a clean schedule. Half-sessions (access token
// but no refresh token) are armed too: the timer fires the recovery path in
// refreshAccessToken() BEFORE the token expires, so the 401 never happens.
function armProactiveRefresh() {
  clearProactiveTimers();
  if (!authToken) return; // logged out — nothing to keep fresh
  const expMs = decodeJwtExpMs(authToken);
  const delay = expMs
    ? Math.max(PROACTIVE_REFRESH_MIN_MS, expMs - Date.now() - PROACTIVE_REFRESH_BUFFER_MS)
    : PROACTIVE_REFRESH_FALLBACK_MS;
  proactiveRefreshTimer = setTimeout(() => { triggerRefresh("proactive"); }, delay);
}

function scheduleRefreshRetry(reason) {
  if (refreshRetryTimer) return; // one pending retry at a time
  const idx = Math.min(refreshRetryAttempt, REFRESH_RETRY_BACKOFF_MS.length - 1);
  const delay = REFRESH_RETRY_BACKOFF_MS[idx];
  refreshRetryAttempt += 1;
  console.log(`[refresh] transient failure (${reason}) — retry in ${delay}ms (attempt ${refreshRetryAttempt})`);
  refreshRetryTimer = setTimeout(() => {
    refreshRetryTimer = null;
    triggerRefresh("retry");
  }, delay);
}

// Drive a refresh through the single-flight refreshAccessToken() and act on the
// outcome. The return value alone is ambiguous (null means BOTH "transient" and
// "we just logged out"), so we disambiguate by inspecting authToken AFTER the
// call: still set → the session is alive and we only had a blip → back off and
// retry; gone → refreshAccessToken already logged us out → stop. On success,
// saveSession()→armProactiveRefresh() has already re-armed + reset the backoff.
async function triggerRefresh(reason) {
  if (!authToken) return; // already logged out
  let result = null;
  try {
    result = await refreshAccessToken();
  } catch (e) {
    console.warn("[refresh] triggerRefresh error:", e?.message);
  }
  if (result && result.access_token) return;          // success — timer re-armed in saveSession
  if (!authToken) { clearProactiveTimers(); return; } // logged out — stop firing
  scheduleRefreshRetry(reason);                        // transient — self-heal
}

// Exchange the stored refresh token for a new access token (and a rotated
// refresh token). Single-flight: concurrent callers share one round-trip so a
// single-use refresh token is never spent twice. Returns { access_token } or
// null. On a hard 401 from the refresh endpoint we log the user out — the
// persistent session is genuinely over (expired / revoked / reuse-detected).
async function refreshAccessToken() {
  if (!refreshToken) {
    // No refresh token but an access token present = a "half session" (a
    // v1.0.7-format file, or a safeStorage decrypt failure). Historically the
    // first 401 here logged the user straight out. Instead, attempt a silent
    // credential-based recovery FIRST (single-flight, so concurrent 401s don't
    // fire N parallel logins) and only log out when it's genuinely hopeless —
    // never on a transient network/5xx blip.
    if (authToken) {
      if (refreshInFlight) return refreshInFlight;
      refreshInFlight = (async () => {
        try {
          const r = await attemptCredentialLogin("half-session 401 recovery");
          if (r.ok && refreshToken) {
            await blinkLogMain("main-halfsession-recovered", {
              note: "silent re-auth restored a refresh token — logout avoided",
            });
            console.log("[refresh] Half-session recovered via saved credentials — logout avoided");
            return { access_token: authToken };
          }
          if (r.reason === "transient" || r.reason === "bad_response") {
            // Recoverable later — keep the (now-expired) session alive so the
            // backoff retry / next 401 can try again. Returning null while
            // authToken is still set reads as "transient" to every caller.
            console.warn("[refresh] Half-session recovery deferred (transient) — keeping session");
            return null;
          }
          // no_creds / rejected → nothing left to recover with → end the session.
          await blinkLogMain("main-halfsession-logout", { reason: r.reason || "unknown" });
          console.log(`[refresh] Half-session unrecoverable (${r.reason}) — logging out`);
          await performLogout();
          return null;
        } finally {
          refreshInFlight = null;
        }
      })();
      return refreshInFlight;
    }
    // Neither token — already logged out (e.g. AuthContext probing on the login
    // screen). Return quietly so performLogout doesn't loop the bootstrap probe.
    return null;
  }
  if (refreshInFlight) return refreshInFlight;

  refreshInFlight = (async () => {
    try {
      const fetchFn = await getFetch();
      const res = await fetchFn(`${API_BASE}/auth/refresh`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Client-Type": "desktop",
        },
        body: JSON.stringify({ refresh_token: refreshToken }),
        timeout: 15000,
      });

      if (res.status === 401) {
        console.log("[refresh] Refresh token rejected — logging out");
        await performLogout();
        return null;
      }
      if (!res.ok) {
        console.warn("[refresh] Unexpected status", res.status);
        return null; // transient — keep the session, retry on next 401
      }

      const data = await res.json();
      if (!data || !data.access_token) return null;

      // Persist the rotated pair (saveSession re-encrypts the refresh token)
      // and hand the new access token to the renderer.
      saveSession({
        token:         data.access_token,
        refresh_token: data.refresh_token || refreshToken,
      });
      injectTokenToRenderer(data.access_token);
      console.log("[refresh] Access token renewed");
      return { access_token: data.access_token };
    } catch (e) {
      console.warn("[refresh] failed:", e?.message);
      return null; // network blip — don't destroy the session
    } finally {
      refreshInFlight = null;
    }
  })();

  return refreshInFlight;
}

// ─── STATUS POLLING ───────────────────────────────────────────────────────────
function startStatusPolling() {
  if (statusTimer) clearInterval(statusTimer);
  pollStatus();
  statusTimer = setInterval(pollStatus, STATUS_POLL_INTERVAL);
}

async function pollStatus() {
  if (!authToken) return;
  // Only one poll in flight at a time — see _pollInFlight.
  if (_pollInFlight) {
    console.log("[poll] skipped — previous poll still in flight");
    return;
  }
  _pollInFlight = true;
  try {
    const fetchFn = await getFetch();
    const res = await fetchFn(`${API_BASE}/tracking/ping`, {
      headers: { Authorization: `Bearer ${authToken}` },
      timeout: 8000,
    });

    if (res.status === 401) {
      // Access token expired — try a silent refresh via triggerRefresh, which
      // owns the outcome: it renews the token on success, logs out on a hard
      // 401 or an unrecoverable half-session, and KEEPS the session on a
      // transient failure (network blip / 503 "Database temporarily
      // unavailable") — scheduling a fast backoff retry instead of waiting for
      // the next 60s poll. A transient failure must NEVER sign the user out.
      console.log("[poll] 401 — attempting silent refresh");
      await triggerRefresh("poll");
      return; // next poll uses the renewed token (or we've already logged out)
    }
    if (res.status !== 200) return;

    const data = await res.json();
    const next = !data.clocked_in
      ? "not_clocked_in"
      : data.on_break
        ? "on_break"
        : "working";

    pollFailCount = 0;
    _networkPauseNotified = false;

    if (next !== currentStatus) {
      currentStatus = next;
      console.log("[poll] Status →", currentStatus);
      updateTray();
    }
    // Reconcile tracking with the authoritative status on EVERY successful
    // poll, not only on a transition. A network pause stops tracking WITHOUT
    // changing currentStatus, so a transition-only check would leave the
    // tracker permanently paused after connectivity returned.
    if (currentStatus === "working" && !isTracking) {
      console.log("[poll] session still open — resuming tracking");
      startTracking();
    } else if (currentStatus !== "working" && isTracking) {
      // Server says the session is closed (e.g. the backend's abandoned-session
      // sweep already closed it). Stop cleanly; do NOT re-open anything.
      console.log("[poll] server reports not working — stopping tracking");
      stopTracking();
    }
  } catch (err) {
    pollFailCount++;
    const kind = err?.name === "AbortError" ? "timeout" : "network";
    console.warn(
      `[poll] Failed (${pollFailCount}, ${kind}):`,
      err?.name,
      err?.code || "",
      err?.message,
    );
    // Network silence PAUSES capture — it does not end the shift.
    //
    // This used to call clockOutSilently(), i.e. POST /employees/clock-out.
    // That was introduced in c8d5a28 ("screenshots no longer captured when
    // employee is clocked out") purely to stop screenshots; ending attendance
    // was the mechanism, never the goal — and stopTracking() alone achieves it.
    //
    // Closing the shift here is also the wrong layer: this path only runs when
    // the client still has enough connectivity to be polling, i.e. when the
    // employee is most likely still working. A genuinely dead client cannot
    // call anything, and is already handled server-side by
    // _auto_close_abandoned_sessions — which closes at the last VERIFIED
    // heartbeat (so hours are never inflated), skips workers on break, and has
    // a circuit breaker for mass outages that this client path never had.
    // The overnight stale-session job is the final backstop.
    if (pollFailCount >= 5 && isTracking) {
      console.warn(
        "[poll] Network unreachable — pausing tracking. Attendance session left OPEN;",
        "the backend closes genuinely abandoned sessions after its own grace window.",
      );
      stopTracking(); // clears isTracking, so this branch cannot re-fire
      if (!_networkPauseNotified) {
        _networkPauseNotified = true;
        showNotification(
          "Temveris",
          "Network unavailable — tracking paused. You are still clocked in; it resumes automatically.",
        );
      }
    }
  } finally {
    _pollInFlight = false;
  }
}

// ─── ACTIVITY POLLING ─────────────────────────────────────────────────────────
function startActivityPolling() {
  if (activityTimer) return;
  try {
    lastMousePos = electronScreen.getCursorScreenPoint();
  } catch {}
  activityTimer = setInterval(() => {
    if (!isTracking) return;
    try {
      const pos = electronScreen.getCursorScreenPoint();
      if (
        trackMouse &&
        lastMousePos &&
        (pos.x !== lastMousePos.x || pos.y !== lastMousePos.y)
      ) {
        mouseMoveCount++;
      }
      lastMousePos = pos;
    } catch {}
    try {
      const idleSec = powerMonitor.getSystemIdleTime();
      if (idleSec < 3) {
        activeSeconds++;
        lastActiveAt = Date.now();
        lastDiagnostics.lastActiveAt = lastActiveAt;
      }
      lastDiagnostics.idleSec = idleSec;
    } catch {}
  }, ACTIVITY_POLL_MS);
}

function stopActivityPolling() {
  if (activityTimer) {
    clearInterval(activityTimer);
    activityTimer = null;
  }
}

function resetActivityCounters() {
  keyCountApp = 0;
  keyCountGlobal = 0;
  mouseMoveCount = 0;
  mouseClickGlobal = 0;
  mouseScrollGlobal = 0;
  activeSeconds = 0;
  keyBurstWindowTs = 0;
  keyBurstCount = 0;
  intervalStartTs = Date.now();
}

// ─── ANTI-FAKE-ACTIVITY — REMOVED (Phase 5) ─────────────────────────────────
// The tracker enumerated every running process, matched them against a
// watch-list and uploaded the matches. The backend never ingested the field:
// it was written to the payload and dropped. That is a surveillance signal
// with a real privacy cost (a process list is sensitive) and zero product
// value, so Phase 5 removed the collection rather than build a consumer for it.
//
// Reinstating it is a PRODUCT decision, not a refactor: it needs a defined
// meaning for a match, a tenant-scoped store, an authorized-viewer surface,
// retention, and an explicit statement of whether it may affect an employee's
// score.

// ─── FOREGROUND WINDOW DETECTION (Win32) ─────────────────────────────────────
// Returns { appName, windowTitle } for the window the user is actually focused
// on. Uses GetForegroundWindow via PowerShell Add-Type so we report the real
// active app, not whichever process happens to be using the most CPU.
//
// IMPORTANT: this runs ASYNC (execFile, not execFileSync). The previous
// implementation blocked the Electron main process for up to 8s on machines
// where PowerShell startup is slow (heavy A/V, restricted ExecutionPolicy,
// roaming profiles), which froze IPC, uploads, and status polling — that
// was the root cause of "tracking fails for some people".
//
// Results are cached for FOREGROUND_CACHE_MS so the heartbeat (60s) and
// screenshot (30s) ticks don't both spawn PowerShell in quick succession.
let _foregroundCache = null; // { value, ts }
// Bumped 4s → 10s so the 10s browser-tracker poll (BROWSER_POLL_INTERVAL) and
// the 60s heartbeat reuse this cache instead of forcing a fresh PowerShell/
// osascript spawn each time — fewer foreground-detection child processes, same
// reported app/title values.
const FOREGROUND_CACHE_MS = 15 * 1000; // re-query at most every ~15s (was 10s) — lets the 30s screenshot / 60s heartbeat reuse the browser-poll's read instead of each spawning a fresh PowerShell

const FG_PS_SCRIPT = `$sig = @'
[DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
[DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, System.Text.StringBuilder text, int count);
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
'@
Add-Type -MemberDefinition $sig -Name Win32 -Namespace ZtNative | Out-Null
try {
  $hwnd = [ZtNative.Win32]::GetForegroundWindow()
  $sb = New-Object System.Text.StringBuilder 512
  [ZtNative.Win32]::GetWindowText($hwnd, $sb, 512) | Out-Null
  $title = $sb.ToString()
  $procId = 0
  [ZtNative.Win32]::GetWindowThreadProcessId($hwnd, [ref]$procId) | Out-Null
  $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
  $name = if ($proc) { $proc.ProcessName } else { 'Desktop' }
  "$name|$title"
} catch { 'Desktop|Desktop' }`;

// macOS equivalent: AppleScript via osascript queries System Events for the
// frontmost application + window title. Requires Automation permission to
// "System Events" (macOS prompts the first time osascript is called). If the
// user denies, we silently fall back to "Desktop" — same behaviour as before.
const FG_OSASCRIPT = `tell application "System Events"
  set frontProc to first process whose frontmost is true
  set frontName to name of frontProc
  set frontTitle to ""
  try
    set frontTitle to name of front window of frontProc
  end try
end tell
return frontName & "|" & frontTitle`;

async function getForegroundApp() {
  let appName = "Desktop",
    windowTitle = "Desktop";
  if (process.platform !== "win32" && process.platform !== "darwin") {
    return { appName, windowTitle };
  }

  if (
    _foregroundCache &&
    Date.now() - _foregroundCache.ts < FOREGROUND_CACHE_MS
  ) {
    return _foregroundCache.value;
  }

  try {
    let raw = "";
    if (process.platform === "win32") {
      const { stdout } = await execFileP(
        "powershell.exe",
        [
          "-NoProfile",
          "-NonInteractive",
          "-WindowStyle",
          "Hidden",
          "-Command",
          FG_PS_SCRIPT,
        ],
        { encoding: "utf-8", timeout: 8000, windowsHide: true },
      );
      raw = (stdout || "").trim();
    } else {
      const { stdout } = await execFileP(
        "/usr/bin/osascript",
        ["-e", FG_OSASCRIPT],
        { encoding: "utf-8", timeout: 5000 },
      );
      raw = (stdout || "").trim();
    }
    if (raw.length > 0) {
      const idx = raw.indexOf("|");
      if (idx >= 0) {
        const procName = raw.substring(0, idx).trim();
        const title = raw.substring(idx + 1).trim();
        if (procName) appName = procName;
        if (title) windowTitle = title;
      } else {
        windowTitle = raw;
        appName = raw;
      }
    }
  } catch (e) {
    // Native query unavailable / blocked / timed out. Reuse the most recent
    // good value if it's still reasonably fresh so the dashboard doesn't
    // flap to "Desktop" every time the helper misbehaves.
    if (_foregroundCache && Date.now() - _foregroundCache.ts < 60_000) {
      return _foregroundCache.value;
    }
  }
  if (appName.toLowerCase().endsWith(".exe")) appName = appName.slice(0, -4);
  appName = (appName || "Desktop").substring(0, 80);
  windowTitle = (windowTitle || appName).substring(0, 255);
  const value = { appName, windowTitle };
  _foregroundCache = { value, ts: Date.now() };
  return value;
}

// ─── macOS SCREEN RECORDING PERMISSION ───────────────────────────────────────
// On macOS 10.15+ apps cannot capture the screen until the user explicitly
// grants Screen Recording permission in System Settings → Privacy & Security.
// Without it, desktopCapturer silently returns empty thumbnails (the symptom
// is "screenshots show 'image not available'" on the dashboard while activity
// counters still work). Windows has no equivalent gate.
let macScreenPermissionPrompted = false;
function checkMacScreenRecordingPermission() {
  if (process.platform !== "darwin") return "granted";
  try {
    return systemPreferences.getMediaAccessStatus("screen"); // 'granted'|'denied'|'restricted'|'not-determined'|'unknown'
  } catch {
    return "unknown";
  }
}
function promptMacScreenRecordingPermission() {
  if (process.platform !== "darwin") return;
  if (macScreenPermissionPrompted) return;
  macScreenPermissionPrompted = true;
  const choice = dialog.showMessageBoxSync({
    type: "warning",
    buttons: ["Open System Settings", "Later"],
    defaultId: 0,
    cancelId: 1,
    title: "Temveris needs Screen Recording permission",
    message: "Screen Recording permission is required to capture screenshots.",
    detail:
      "macOS blocks screen capture until you grant permission in:\n\n" +
      "System Settings → Privacy & Security → Screen Recording → enable Temveris\n\n" +
      "After enabling, fully quit and reopen Temveris for the change to take effect.\n\n" +
      "Until then, your activity (mouse clicks, keystrokes, app usage) will still be recorded — only the screenshot images will be missing.",
    icon: path.join(__dirname, "assets", "icon.png"),
  });
  if (choice === 0) {
    shell.openExternal(
      "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    );
  }
}

// ─── SCREEN CAPTURE (Electron native desktopCapturer) ────────────────────────
// Returns a PNG Buffer of the primary display. Uses Electron's built-in
// desktopCapturer so there's no .NET Framework requirement, no temp .bat,
// and no compiled child process for AV to quarantine.
//
// The thumbnail is pre-sized to at most 1920x1080 to keep upload payloads
// reasonable on slow networks (the backend re-compresses to WebP regardless).
// Privacy blur for `screenshot_blur`. Downscale hard, then scale back up:
// the resample destroys legible text and recognisable faces while leaving the
// layout identifiable, which is what a monitoring screenshot needs to keep.
//
// Uses Electron's own nativeImage — no new dependency, and no raw buffer ever
// leaves this function. Before Phase 5 the setting was read, reported as
// `blur_applied`, and never acted on: the raw PNG was uploaded. A privacy
// control that reports success without acting is worse than none.
const BLUR_DIVISOR = 24;   // 1920px -> 80px -> back up. Text is unreadable.

function applyPrivacyBlur(pngBuf) {
  try {
    const img = nativeImage.createFromBuffer(pngBuf);
    const { width, height } = img.getSize();
    if (!width || !height) return null;
    const small = img.resize({
      width: Math.max(8, Math.round(width / BLUR_DIVISOR)),
      quality: "good",
    });
    const out = small.resize({ width, height, quality: "good" }).toPNG();
    return out && out.length ? out : null;
  } catch (e) {
    console.warn("[capture] blur failed:", e && e.message);
    return null;   // caller decides — it must NOT fall back to the raw image
  }
}

async function captureScreenshotPng() {
  let width = 1920,
    height = 1080;
  try {
    const primary = electronScreen.getPrimaryDisplay();
    const w = primary.size.width || 1920;
    const h = primary.size.height || 1080;
    const ratio = Math.min(1920 / w, 1080 / h, 1);
    width = Math.max(640, Math.floor(w * ratio));
    height = Math.max(480, Math.floor(h * ratio));
  } catch {}

  // Retry on empty frames. Windows' Desktop Duplication API (which Electron
  // uses under the hood for desktopCapturer) can briefly return an empty
  // thumbnail during DWM-driven transitions — alt-tab, window snap, browser
  // tab switch, taskbar autohide, Win+Tab. The display is composited normally
  // before and after, so we just need the capture to land between the
  // transient moments. Without this retry, every screenshot that fires during
  // those ~100-300ms windows produces an "Image not available" tile on the
  // dashboard even though the user was actively working. 250ms between
  // attempts is enough for DWM to stabilise without making a successful
  // late-attempt feel laggy to the activity timer.
  let lastError = null;
  for (let attempt = 1; attempt <= 3; attempt++) {
    let sources;
    try {
      sources = await desktopCapturer.getSources({
        types: ["screen"],
        thumbnailSize: { width, height },
        fetchWindowIcons: false,
      });
    } catch (err) {
      lastError = err;
      if (attempt < 3) await new Promise((r) => setTimeout(r, 250));
      continue;
    }
    if (!sources || sources.length === 0) {
      lastError = new Error("desktopCapturer returned no screen sources");
      if (attempt < 3) await new Promise((r) => setTimeout(r, 250));
      continue;
    }
    let pick = sources[0];
    try {
      const primaryId = String(electronScreen.getPrimaryDisplay().id);
      pick = sources.find((s) => s.display_id === primaryId) || sources[0];
    } catch {}
    if (!pick.thumbnail || pick.thumbnail.isEmpty()) {
      lastError = new Error("desktopCapturer returned an empty thumbnail");
      if (attempt < 3) await new Promise((r) => setTimeout(r, 250));
      continue;
    }
    if (attempt > 1) {
      console.log(
        `[capture] recovered on attempt ${attempt} after transient empty frame`,
      );
    }
    return pick.thumbnail.toPNG();
  }
  throw lastError || new Error("desktopCapturer failed after 3 attempts");
}

// ─── HEARTBEAT ──────────────────────────────────────────────────────────────
// Lightweight ping to the backend every HEARTBEAT_INTERVAL ms so the admin
// "My Team" dashboard sees the live foreground app without waiting for the
// next screenshot upload. No image, no activity counters — just app_name.
// Windows' lock screen surfaces as the "LockApp" process. Treat that (or any
// explicit lock-screen event) as "don't report — user is away from keyboard".
function isLockScreenActive(appName) {
  if (isScreenLocked) return true;
  const a = (appName || "").toLowerCase();
  return a === "lockapp" || a === "logonui" || a === "loginwindow";
}

async function sendHeartbeat() {
  if (!isTracking || !authToken) return;
  const { appName, windowTitle } = await getForegroundApp();
  if (isLockScreenActive(appName)) return; // paused while locked
  try {
    const fetchFn = await getFetch();
    const res = await fetchFn(`${API_BASE}/tracking/sync`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${authToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        app_name: trackApps ? appName : "",
        window_title: trackApps ? windowTitle : "",
        timestamp: new Date().toISOString(),
        // Lets the admin dashboard surface "who's stuck on old version".
        // Without this we have no way to tell which employees missed an update.
        app_version: app.getVersion(),
      }),
      timeout: 8000,
    });
    if (res && res.ok) {
      // Diagnostics: remember the last successful heartbeat so the tray
      // "Diagnostics" submenu can show "Last heartbeat: 12s ago" — invaluable
      // for debugging the "tracker isn't reporting" symptom without having
      // to attach a console.
      lastDiagnostics.lastHeartbeatAt = Date.now();
    }
  } catch (e) {
    // Heartbeat is best-effort — never log loudly
  }
}

// ─── FETCH SETTINGS FROM BACKEND ─────────────────────────────────────────────
async function fetchAndApplySettings() {
  try {
    const fetchFn = await getFetch();
    // GET /settings is authenticated + organization-scoped on the backend.
    // It used to be public, so this fetch historically sent no Authorization
    // header — after the multitenancy deploy that silently became a 401, which
    // dropped the tracker back to the hard-coded 30s default. Send the access
    // token the tracker already holds so we receive the org's configured value.
    const res = await fetchFn(`${API_BASE}/settings`, {
      headers: authToken ? { Authorization: `Bearer ${authToken}` } : {},
      timeout: 8000,
    });
    if (!res.ok) {
      console.warn(
        `[settings] fetch failed: HTTP ${res.status} — keeping interval ${SCREENSHOT_INTERVAL / 1000}s`,
      );
      return;
    }
    const data = await res.json();
    const s = data?.settings;

    // Respect admin screenshot_enabled toggle
    if (s?.screenshot_enabled !== undefined) {
      const enabled =
        s.screenshot_enabled === "true" || s.screenshot_enabled === true;
      if (enabled !== screenshotEnabled) {
        screenshotEnabled = enabled;
        console.log("[settings] screenshot_enabled →", enabled);
      }
    }

    // Respect admin screenshot_blur toggle (sent as a flag with each upload —
    // backend stores it on the row so the dashboard knows to render the blur).
    if (s?.screenshot_blur !== undefined) {
      const blur = s.screenshot_blur === "true" || s.screenshot_blur === true;
      if (blur !== screenshotBlur) {
        screenshotBlur = blur;
        console.log("[settings] screenshot_blur →", blur);
      }
    }

    // Respect admin idle_threshold_minutes (used to flag the screenshot as
    // captured-while-idle when system idle exceeds the threshold).
    const idleMins = parseInt(s?.idle_threshold_minutes);
    if (idleMins && idleMins > 0 && idleMins !== idleThresholdMins) {
      idleThresholdMins = idleMins;
      console.log("[settings] idle_threshold_minutes →", idleMins);
    }

    // Activity-tracking flags (org→team→employee effective value). All default
    // ON; a flag absent from the payload leaves the current value untouched.
    const flag = (v, cur) =>
      v === undefined ? cur : (v === "true" || v === true);
    trackMouse = flag(s?.track_mouse, trackMouse);
    trackKeyboard = flag(s?.track_keyboard, trackKeyboard);

    trackApps = flag(s?.track_apps, trackApps);
    trackUrls = flag(s?.track_urls, trackUrls);
    const nextBrowser = flag(s?.track_browser, trackBrowser);
    if (nextBrowser !== trackBrowser) {
      trackBrowser = nextBrowser;
      console.log("[settings] track_browser →", trackBrowser);
      if (isTracking) {
        if (trackBrowser) startBrowserTracker();
        else browserTracker.stop().catch(() => {});
      }
    }

    const secs = parseInt(s?.screenshot_interval);
    console.log(
      `[settings] fetched screenshot_interval=${Number.isFinite(secs) ? secs : "?"}s (timer currently ${SCREENSHOT_INTERVAL / 1000}s)`,
    );
    if (secs && secs > 0) {
      const newInterval = secs * 1000;
      if (newInterval !== SCREENSHOT_INTERVAL) {
        SCREENSHOT_INTERVAL = newInterval;
        console.log(
          "[settings] screenshot_interval changed →",
          secs,
          "s — restarting timer",
        );
        // Restart the screenshot timer with the new interval if tracking is active
        if (isTracking && screenshotTimer) {
          clearInterval(screenshotTimer);
          screenshotTimer = setInterval(captureAndUpload, SCREENSHOT_INTERVAL);
          console.log("[settings] screenshot timer restarted at", secs, "s");
        }
      }
    }
  } catch (e) {
    console.warn("[settings] fetch failed:", e.message);
  }
}

// ─── SCREENSHOT TRACKING ──────────────────────────────────────────────────────
async function startTracking() {
  if (isTracking) return;
  await fetchAndApplySettings(); // pull latest interval before starting
  isTracking = true;
  // Every tracking session starts with a clean failure budget. Without this the
  // counter survived across sessions: once it passed the threshold it stayed
  // there until a poll succeeded, so the next session was paused on its FIRST
  // failed poll instead of getting the intended 5-failure grace.
  pollFailCount = 0;
  _networkPauseNotified = false;
  resetActivityCounters();
  startActivityPolling();
  startGlobalInputHook(); // count keystrokes/clicks from any HID device
  console.log(
    `[tracker] STARTED — interval: ${SCREENSHOT_INTERVAL / 1000}s at ${new Date().toISOString()}`,
  );
  // Defensive: clear any previous timer before creating a new one
  if (screenshotTimer) {
    clearInterval(screenshotTimer);
    screenshotTimer = null;
  }
  // First screenshot fires after exactly ONE admin-configured interval — never
  // on clock-in, login, or resume-from-break. e.g. clock-in 9:00 with a 30-min
  // setting → first capture at 9:30, next at 10:00, …
  screenshotTimer = setInterval(() => {
    console.log(
      `[tracker] heartbeat — firing captureAndUpload at ${new Date().toISOString()} (interval=${SCREENSHOT_INTERVAL / 1000}s)`,
    );
    captureAndUpload().catch((e) =>
      console.error("[tracker] captureAndUpload rejected:", e.message),
    );
  }, SCREENSHOT_INTERVAL);
  // Re-fetch settings periodically so interval / enabled / blur / idle changes
  // take effect WITHOUT the employee clocking out. 60 s keeps an admin's "Save"
  // felt within a minute, while a 1-request/min/user load on the org-scoped
  // (and lightweight) /settings endpoint stays negligible.
  if (settingsTimer) clearInterval(settingsTimer);
  settingsTimer = setInterval(fetchAndApplySettings, 60 * 1000); // re-fetch every 60 s

  // Live app heartbeat — runs independently of the screenshot timer so the
  // admin "My Team" dashboard sees the user's foreground app within ~15s,
  // regardless of how long the screenshot interval is configured for.
  if (heartbeatTimer) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
  setTimeout(sendHeartbeat, 3 * 1000); // first ping quickly after start
  heartbeatTimer = setInterval(() => {
    sendHeartbeat().catch(() => {});
  }, HEARTBEAT_INTERVAL);

  // Browser tab intelligence — UIAutomation-based native capture that feeds
  // the backend's /browser/heartbeat + /browser/event pipeline. Without this
  // the admin dashboard only sees "Chrome" / "Edge" rather than the actual
  // tabs (GitHub, Jira, Figma, etc.). Runs in parallel with the screenshot
  // and status timers; failures are swallowed inside the module so they
  // can't crash the tracker.
  if (trackBrowser) startBrowserTracker();

}

// Browser-tab tracking start — extracted so the track_browser flag can toggle
// it live from fetchAndApplySettings without duplicating the options bag.
function startBrowserTracker() {
  browserTracker.start({
    apiBase: API_BASE,
    getAuthToken: () => authToken,
    getForeground: () => getForegroundApp(),
    getCounters: () => ({ keys: keyCountGlobal, clicks: mouseClickGlobal }),
    isTracking: () => isTracking,
    isIdle: () => {
      try { return powerMonitor.getSystemIdleTime() >= idleThresholdMins * 60; }
      catch { return false; }
    },
    // track_urls off → the module sends domain only, no full URL/path/title.
    getCaptureUrls: () => trackUrls,
    log: (msg) => console.log(msg),
  });
}

function stopTracking() {
  if (!isTracking) return;
  isTracking = false;
  if (screenshotTimer) {
    clearInterval(screenshotTimer);
    screenshotTimer = null;
  }
  if (settingsTimer) {
    clearInterval(settingsTimer);
    settingsTimer = null;
  }
  if (heartbeatTimer) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
  stopActivityPolling();
  stopGlobalInputHook();
  // Flush in-flight browser tab session so its duration isn't lost.
  browserTracker.stop().catch(() => {});
  resetActivityCounters();
  console.log("[tracker] STOPPED");
}

// ─── CLOCK-OUT HELPER ─────────────────────────────────────────────────────────
// Stops tracking, updates local state, fires clock-out API, shows notification.
// Used by suspend / shutdown / network-loss paths. Silent on network errors.
// Ask the backend to clock out and report what ACTUALLY happened. We never
// treat this as success unless the backend confirms the session closed.
//
// Controlled Clock-Out returns HTTP 200 with {success:false,
// code:"EARLY_CLOCK_OUT_REASON_REQUIRED"} when the worker is below required
// hours and no reason was supplied — a system/automatic call (this file)
// cannot supply one. That is NOT a close; the session stays open and the
// worker must clock out interactively (with a reason) from the dashboard.
//
// Returns:
//   "closed"         backend closed the session (success:true)
//   "already_closed" server had no open session ("No active session")
//   "open"           reason required → session deliberately left open
//   "error"          offline / non-200 / network failure → unknown, assume open
async function requestClockOut(timeoutMs = 5000) {
  if (!authToken) return "error";
  try {
    const fetchFn = await getFetch();
    const res = await fetchFn(`${API_BASE}/employees/clock-out`, {
      method: "POST",
      // X-Client-Type marks this as the desktop app. REQUIRED: attendance
      // actions are desktop-only server-side, and this is the ONE attendance
      // call made from the main process. The renderer's apiFetch already sends
      // it, but node-fetch here inherits neither that header nor the window's
      // "temveris-desktop/X.Y.Z" User-Agent — so without this line the
      // suspend / shutdown / quit clock-out would be rejected as a browser
      // call and the shift would be left open. Same header the login, refresh
      // and logout calls in this file already send.
      headers: {
        Authorization: `Bearer ${authToken}`,
        "X-Client-Type": "desktop",
      },
      timeout: timeoutMs,
    });
    if (!res || !res.ok) return "error";
    const data = await res.json().catch(() => ({}));
    if (data && data.success === true) return "closed";
    if (data && data.code === "EARLY_CLOCK_OUT_REASON_REQUIRED") return "open";
    // success:false without the reason code == "No active session" etc.
    return "already_closed";
  } catch (err) {
    console.warn("[requestClockOut] failed:", err?.message);
    return "error";
  }
}

// Used by suspend / shutdown / offline / quit paths. Only transitions local
// state to "not clocked in" when the backend CONFIRMS the session is closed —
// never pretends success, never desyncs local vs server, never stops tracking
// on a session that is still open. Safe on flaky networks and below-hours
// workers (they stay genuinely clocked in and clock out properly later).
let _autoClockOutInFlight = false;

async function clockOutSilently(notifyMsg) {
  // Guard against overlapping system triggers (e.g. powerMonitor "shutdown"
  // firing alongside app "before-quit") issuing duplicate clock-out requests.
  if (_autoClockOutInFlight) {
    console.log("[clockOutSilently] skipped — an automatic clock-out is already in flight");
    return;
  }
  _autoClockOutInFlight = true;
  try {
    if (!authToken) {
      // Not authenticated → no backend session to strand; safe to reset locally.
      stopTracking();
      currentStatus = "not_clocked_in";
      updateTray();
      return;
    }
    const result = await requestClockOut(5000);
    if (result === "closed" || result === "already_closed") {
      stopTracking();
      currentStatus = "not_clocked_in";
      updateTray();
      if (result === "closed") {
        console.log("[clockOutSilently] clock-out confirmed by backend");
        if (notifyMsg) showNotification("Temveris", notifyMsg);
      }
    } else {
      // "open" (reason required) or "error" (offline / server unreachable):
      // the session is still open. Do NOT mark clocked-out, do NOT stop
      // tracking — stay recoverable so the worker (or a later interactive
      // clock-out with a reason) resolves it. Prevents the phantom
      // "clocked-out locally / open on server" desync.
      console.warn(
        `[clockOutSilently] backend did not close the session (${result}) — remaining clocked in`,
      );
    }
  } finally {
    _autoClockOutInFlight = false;
  }
}

// ─── POWER EVENT HANDLERS ─────────────────────────────────────────────────────
async function handleSuspend() {
  console.log("[power] suspend/hibernate — clocking out");
  isScreenLocked = true;
  await clockOutSilently("Session saved — clock in again when you're back.");
}

function handleResume() {
  console.log("[power] resume — re-checking session + status");
  isScreenLocked = false;
  offlineSince = null;
  // Timers are frozen while the machine is suspended, so the access token may
  // have expired during sleep. If it's expired (or within the proactive buffer)
  // refresh now — with backoff if the network hasn't reconnected yet — BEFORE
  // polling, so the first post-wake request doesn't 401. Otherwise just re-arm
  // the proactive timer (which sleep paused) and poll. The short delay lets
  // Wi-Fi / VPN re-establish first.
  setTimeout(async () => {
    const expMs = authToken ? decodeJwtExpMs(authToken) : null;
    const nearExpiry = !expMs || (expMs - Date.now()) <= PROACTIVE_REFRESH_BUFFER_MS;
    if (authToken && nearExpiry) {
      await triggerRefresh("resume");
    } else {
      armProactiveRefresh();
    }
    pollStatus();
    // The dashboard is still mounted across sleep/wake (no remount), so push a
    // sync so it reflects any clock/break change that happened while suspended.
    pushForceRefresh("resume");
  }, 5000);
}

async function handleShutdown() {
  console.log("[power] system shutdown — clocking out");
  await clockOutSilently(null); // no notification, OS is shutting down
}

// ─── NETWORK MONITOR ──────────────────────────────────────────────────────────
// Checks connectivity every 30 s. After the grace window it PAUSES tracking —
// it no longer clocks the employee out.
//
// The original rationale was "so the timer doesn't inflate while offline", but
// hours are not derived from this client's timer: the backend closes an
// abandoned session at the last VERIFIED heartbeat, so recorded hours cannot
// inflate no matter how long the client is offline. The local timer is display
// only. Clocking out here ended real shifts over transient wifi drops and was
// the same defect as the pollStatus path — see the comment there.
const OFFLINE_PAUSE_MS = 5 * 60 * 1000; // 5 minutes

async function checkNetwork() {
  if (!isTracking || !authToken) {
    offlineSince = null; // not tracking — no need to monitor
    return;
  }
  try {
    const fetchFn = await getFetch();
    await fetchFn(`${API_BASE}/health`, { timeout: 5000 });
    // Successfully reached server → online
    if (offlineSince !== null) {
      const downSecs = Math.round((Date.now() - offlineSince) / 1000);
      console.log(`[network] Back online after ${downSecs}s`);
      offlineSince = null;
    }
  } catch {
    // Cannot reach server
    if (offlineSince === null) {
      offlineSince = Date.now();
      console.warn("[network] Went offline at", new Date().toISOString());
    } else {
      const offlineMs = Date.now() - offlineSince;
      console.warn(
        `[network] Still offline — ${Math.round(offlineMs / 1000)}s`,
      );
      if (offlineMs >= OFFLINE_PAUSE_MS && isTracking) {
        console.warn(
          "[network] 5 min offline — pausing tracking. Attendance session left OPEN.",
        );
        offlineSince = null;
        stopTracking(); // isTracking=false, so checkNetwork() returns early after this
        if (!_networkPauseNotified) {
          _networkPauseNotified = true;
          showNotification(
            "Temveris",
            "Network unavailable — tracking paused. You are still clocked in; it resumes automatically.",
          );
        }
      }
    }
  }
}

function startNetworkMonitor() {
  if (networkMonitorTimer) clearInterval(networkMonitorTimer);
  networkMonitorTimer = setInterval(
    () => checkNetwork().catch(() => {}),
    30_000,
  );
}

// ─── LOCK / UNLOCK PAUSE ──────────────────────────────────────────────────────
// Fully suspend tracker activity while the workstation is locked. The OS
// "lock-screen" and "unlock-screen" events drive this. Distinct from
// stopTracking(): isTracking stays true so we don't tear down the clock-in
// session — we just stop firing timers, and resume the same intervals on
// unlock. This guarantees zero screenshot/heartbeat/activity work happens
// while the screen is locked.
function pauseTrackerForLock() {
  if (screenshotTimer) {
    clearInterval(screenshotTimer);
    screenshotTimer = null;
  }
  if (heartbeatTimer) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
  stopActivityPolling();
  resetActivityCounters(); // discard any input that leaked in just before lock
  captureFailCount = 0; // start fresh on unlock — old failures don't apply
  console.log("[tracker] PAUSED for lock screen");
}

function resumeTrackerAfterLock() {
  // Only resume if the user is still considered actively tracking. If they
  // were clocked out (manually or via /tracking/ping) while locked, do nothing
  // — pollStatus will pick the new state up on its next tick.
  if (!isTracking) return;
  resetActivityCounters();
  startActivityPolling();
  // Restart the interval timer only — same rule as startTracking(): first
  // capture happens after one full admin-configured interval, never on the
  // unlock event itself.
  // Defensive: clear any previous timers before reassigning so a missed
  // pause (or a double unlock-screen event) can't leave two intervals running
  // — mirrors the guard in startTracking().
  if (screenshotTimer) clearInterval(screenshotTimer);
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  screenshotTimer = setInterval(() => {
    captureAndUpload().catch((e) =>
      console.error("[tracker] captureAndUpload rejected:", e && e.message),
    );
  }, SCREENSHOT_INTERVAL);
  setTimeout(() => sendHeartbeat().catch(() => {}), 3 * 1000);
  heartbeatTimer = setInterval(
    () => sendHeartbeat().catch(() => {}),
    HEARTBEAT_INTERVAL,
  );
  console.log("[tracker] RESUMED after unlock");
}

async function captureAndUpload() {
  if (!isTracking || !authToken) return;

  // Respect admin screenshot_enabled setting
  if (!screenshotEnabled) {
    console.log("[capture] Skipped — screenshots disabled by admin");
    return;
  }

  // Prefer real counts from the global input hook (every device, every
  // app). Fall back to the historical heuristic when the hook isn't
  // running so the tracker still produces non-zero numbers on platforms
  // where the native binary failed to load.
  //
  // - `mc` is true mouse-click count
  // - `mm` is mouse-movement seconds within the interval (1-sec polls
  //   where the cursor moved). Sending this lets the backend treat
  //   reading/scrolling-heavy work as active even when no clicks happen.
  // - `ms` is wheel/scroll event count
  // - `kc` is true keystroke count from any HID device
  // - `as` is the OS-measured active-seconds count for this interval —
  //   the most accurate "minute-level" signal the spec asks for. 1 per
  //   second of the interval where the OS reported idle < 3s.
  // - `iv` is the actual wall-clock length of this interval. The backend
  //   uses it to compute idle-within-tick = max(0, iv - as).
  const hookActive = !!(uIOhook && uioStarted);
  // Activity-tracking flags zero out the corresponding signal when disabled, so
  // no mouse/keyboard data leaves the machine when an admin turns it off.
  const mc = (hookActive && trackMouse) ? mouseClickGlobal : 0;
  const mm = trackMouse ? mouseMoveCount : 0; // poll-based cursor movement
  const ms = (hookActive && trackMouse) ? mouseScrollGlobal : 0;
  const kc = trackKeyboard ? (hookActive ? keyCountGlobal : keyCountApp) : 0;
  const as = activeSeconds;
  const iv = Math.max(1, Math.round((Date.now() - intervalStartTs) / 1000));
  let idleSecs = 0;
  try {
    idleSecs = powerMonitor.getSystemIdleTime();
  } catch {}
  resetActivityCounters();

  // Get the currently focused window + its process name.
  const { appName, windowTitle } = await getForegroundApp();

  // Per-capture fact, not a setting: set only when applyPrivacyBlur() runs.
  let blurAppliedThisCapture = false;

  // Don't capture or upload while the workstation is locked — the user is
  // away from keyboard, and a screenshot of the lock screen is both useless
  // and a (mild) privacy concern. Activity counters already reset above so
  // the next post-unlock upload starts clean.
  if (isLockScreenActive(appName)) {
    console.log("[capture] Skipped — screen is locked");
    return;
  }

  // Capture screenshot via Electron's native desktopCapturer
  let imageBase64 = "";
  try {
    let pngBuf = await captureScreenshotPng();
    if (screenshotBlur) {
      const blurred = applyPrivacyBlur(pngBuf);
      if (blurred) {
        pngBuf = blurred;
        blurAppliedThisCapture = true;
      } else {
        // The org asked for blur and we could not deliver it. Uploading the
        // RAW image would silently break the promise, so skip this capture.
        console.warn("[capture] Skipped — screenshot_blur is on but blurring "
                     + "failed; refusing to upload an unblurred image");
        return;
      }
    }
    imageBase64 = pngBuf.toString("base64");
    captureFailCount = 0; // reset on success
    console.log(
      `[capture] ${appName} | clicks=${mc} moves=${mm} scrolls=${ms} ` +
        `keys=${kc} activeSec=${as}/${iv} idle=${idleSecs}s ` +
        `pngBytes=${pngBuf.length}`,
    );
  } catch (err) {
    captureFailCount++;
    console.error("[capture] Failed:", err && err.message);
    // On macOS, the most common cause is missing Screen Recording permission.
    // Detect that explicitly and surface a useful prompt instead of a generic
    // "screen capture failing" notification.
    const macStatus = checkMacScreenRecordingPermission();
    if (process.platform === "darwin" && macStatus !== "granted") {
      if (captureFailCount === 1) {
        try {
          new Notification({
            title: "Temveris — Screen Recording permission needed",
            body:
              "macOS is blocking screenshot capture. " +
              "Click to open System Settings and enable Temveris under Screen Recording.",
          }).show();
        } catch {}
        promptMacScreenRecordingPermission();
      }
    } else if (captureFailCount === 3) {
      // Notify user after 3 consecutive failures so they know something is wrong
      try {
        new Notification({
          title: "Temveris — Screenshot Error",
          body:
            "Screen capture is failing on this machine. " +
            "Activity is still being recorded.",
        }).show();
      } catch {}
    }
    // Do NOT return — still upload activity data (app, clicks, keys) without
    // the screenshot image so the admin can see the employee is working even
    // when screen capture is unavailable on this machine.
    imageBase64 = "";
  }

  // Apply admin idle_threshold_minutes — flag this capture as "idle" if the
  // system has been idle longer than the configured threshold.
  const wasIdle = idleSecs >= idleThresholdMins * 60;

  const payload = {
    image: imageBase64,
    timestamp: new Date().toISOString(),
    app_name: trackApps ? appName : "",
    window_title: trackApps ? windowTitle : "",
    mouse_clicks: mc,
    mouse_moves: mm,           // 1-sec polls where the cursor moved
    mouse_scrolls: ms,         // wheel events in this interval
    key_presses: kc,
    active_seconds: as,        // OS-measured active-seconds within this interval
    interval_seconds: iv,      // wall-clock length of this interval
    idle_seconds: idleSecs,    // raw OS idle at capture (used for 15-min alert)
    is_idle: wasIdle,
    // Whether the native global input hook (uIOhook) is loaded + running. When
    // false, mouse_clicks/scrolls/key_presses are unreliable (clicks/scrolls
    // forced to 0; keys only from the in-app fallback), so the Active Time
    // Engine scores this interval in "degraded" mode (capped, flagged
    // LIMITED_TRACKING) instead of penalising the worker for a missing binary.
    hook_active: hookActive,
    // `blur_requested` is the org's setting; `blur_applied` is a fact about
    // THIS image and is true only if applyPrivacyBlur() actually ran on it.
    blur_requested: screenshotBlur,
    blur_applied: blurAppliedThisCapture,
    tracker_version: app.getVersion(),
    os_version: `${process.platform} ${require("os").release()}`,
  };

  // Serialize ONCE, before the retry loop — the payload (and the embedded
  // base64 image) never changes between attempts, so re-stringifying it on each
  // of the 3 retries just re-allocates a multi-MB string for no reason. Same
  // body bytes sent on every attempt.
  const payloadBody = JSON.stringify(payload);

  // Upload with 3 retries
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const fetchFn = await getFetch();
      const res = await fetchFn(`${API_BASE}/tracking/screenshot`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${authToken}`,
          "Content-Type": "application/json",
        },
        body: payloadBody,
        timeout: 30000,
      });
      if (res.status === 200) {
        const r = await res.json();
        console.log(`[upload] OK id=${r.id}`);
        // POW record is now created automatically server-side inside /tracking/screenshot —
        // no extra client call needed.
        return;
      }
      if (res.status === 401) {
        // Access token expired mid-session (normal roughly every 30 min). Try a
        // silent refresh and retry the upload with the fresh token instead of
        // tearing the session down. refreshAccessToken logs out only on a hard
        // 401 / missing token; a transient refresh failure keeps the session
        // alive and we simply drop this one screenshot.
        console.log("[upload] 401 — attempting silent refresh");
        const refreshed = await refreshAccessToken();
        if (refreshed && refreshed.access_token) continue; // retry with new token
        return;
      }
      if (res.status === 403) {
        console.warn("[upload] Not clocked in (403) — stopping tracker");
        stopTracking();
        return;
      }
      const body = await res.text().catch(() => "");
      console.warn(
        `[upload] HTTP ${res.status} attempt ${attempt}:`,
        body.substring(0, 200),
      );
    } catch (err) {
      console.warn(`[upload] attempt ${attempt}:`, err.message);
    }
    if (attempt < 3) await new Promise((r) => setTimeout(r, 2000 * attempt));
  }
  console.error("[upload] All 3 attempts failed — screenshot lost");
}

// ─── APP LIFECYCLE ────────────────────────────────────────────────────────────
const gotLock = app.requestSingleInstanceLock();

if (!gotLock) {
  // Another instance is already running — focus it and exit
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow) {
      mainWindow.show();
      mainWindow.focus();
    }
  });

  app.whenReady().then(() => {
    // ── CRITICAL ORDER ──────────────────────────────────────────────────────
    // 1. loadSavedSession() is called inside createWindow() BEFORE loadURL()
    //    so savedSession is populated before the preload fires sendSync.
    // 2. createTray() after window so tray reflects initial state.
    // 3. startStatusPolling() only if we have a token from disk.
    // ────────────────────────────────────────────────────────────────────────
    createWindow();
    Menu.setApplicationMenu(buildAppMenu()); // re-apply after ready
    createTray();

    // ── macOS SCREEN RECORDING PERMISSION (first-launch prompt) ──────────
    // Trigger an early access query so macOS adds Temveris to the Screen
    // Recording list, then check status and prompt the user if not granted.
    // Delayed so it doesn't block window creation.
    if (process.platform === "darwin") {
      setTimeout(() => {
        try {
          // Touching desktopCapturer registers the app in the Screen Recording
          // pane even before the user grants access — so they can find us.
          desktopCapturer.getSources({ types: ["screen"], thumbnailSize: { width: 1, height: 1 } }).catch(() => {});
        } catch {}
        const status = checkMacScreenRecordingPermission();
        console.log("[mac-permission] screen recording status:", status);
        if (status !== "granted") {
          promptMacScreenRecordingPermission();
        }
      }, 3000);
    }

    // ── LOCAL TOKEN SERVER (port 8765) ───────────────────────────────────
    // The web frontend calls POST http://localhost:8765/set-token to push
    // the auth token when the user logs in from a regular browser (not the
    // built-in Electron window). Without this server that call silently
    // fails and the tracker has no token → no screenshots.
    const http = require("http");
    // SEC-1: only honour /set-token from the legitimate Temveris web origin
    // (WEBSITE_URL) or a localhost dev origin. Browsers always attach `Origin`
    // on a cross-origin fetch and cannot forge it, so reflecting ONLY an
    // allow-listed origin (instead of "*") blocks a malicious page the user
    // visits from injecting a token into the running tracker. The legitimate
    // web→tracker push (Dashboard.jsx from WEBSITE_URL / localhost dev) is
    // unaffected — it already sends a matching Origin.
    const isAllowedTokenOrigin = (origin) => {
      if (!origin) return false; // a real browser caller always sends Origin
      try {
        if (origin === new URL(WEBSITE_URL).origin) return true;
        const u = new URL(origin);
        return u.hostname === "localhost" || u.hostname === "127.0.0.1";
      } catch {
        return false;
      }
    };
    const tokenServer = http.createServer((req, res) => {
      const origin = req.headers.origin;
      const allowed = isAllowedTokenOrigin(origin);
      // Reflect ONLY an allow-listed origin (never "*"); unknown origins get no
      // CORS grant and are rejected below.
      if (allowed) {
        res.setHeader("Access-Control-Allow-Origin", origin);
        res.setHeader("Vary", "Origin");
      }
      res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
      res.setHeader("Access-Control-Allow-Headers", "Content-Type");
      if (req.method === "OPTIONS") {
        res.writeHead(allowed ? 204 : 403);
        res.end();
        return;
      }
      if (req.method === "POST" && req.url === "/set-token") {
        if (!allowed) {
          // Token-injection attempt from a non-allow-listed origin — reject.
          console.warn(`[token-server] rejected /set-token from origin: ${origin || "(none)"}`);
          res.writeHead(403, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ ok: false, error: "origin_not_allowed" }));
          return;
        }
        let body = "";
        req.on("data", (chunk) => {
          body += chunk;
        });
        req.on("end", () => {
          try {
            // CRITICAL: pull `refresh_token` off the body. Earlier revisions
            // destructured only {token, name, email, role, user_id}, which
            // dropped the long-lived secret on the floor for any user who
            // logged in via a regular browser and let the tracker pick it up
            // here. The next 401 in the renderer would then hit the "no
            // refresh token but access token present" branch in main's
            // refreshAccessToken() and call performLogout(), deleting
            // zt_auth.json — and the user was bounced to /login on the next
            // restart even though their saved credentials were intact.
            const { token, refresh_token, name, email, role, user_id } = JSON.parse(body);
            if (token && token !== authToken) {
              console.log("[token-server] received new token via HTTP");
              console.log(
                `[token-server] payload includes refresh_token: ${!!refresh_token}`,
              );
              if (!refresh_token) {
                // Loud warning so a `tail`-of-the-log immediately surfaces
                // this misconfiguration. The session WILL be persisted —
                // saveSession's `s.refresh_token || refreshToken || null`
                // fallback still preserves any in-memory refresh token from a
                // prior login — but if this is a cold start with no prior
                // session, the file will be saved without a refresh_blob.
                console.warn(
                  "[token-server] Web caller did NOT send refresh_token. " +
                  "The caller (frontend Dashboard.jsx push to 127.0.0.1:8765) " +
                  "must include `refresh_token` in the JSON body to guarantee " +
                  "long-lived desktop sessions. Saving with whatever refresh " +
                  "token is currently in memory (may be null).",
                );
              }
              saveSession({
                token,
                refresh_token: refresh_token || null,
                name:    name    || "",
                email:   email   || "",
                role:    role    || "employee",
                user_id: user_id || "",
              });
              startStatusPolling();
              updateTray();
            }
          } catch (e) {
            console.warn("[token-server] failed to parse /set-token body:", e?.message);
          }
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ ok: true }));
        });
        return;
      }
      res.writeHead(404);
      res.end();
    });
    tokenServer.listen(8765, "127.0.0.1", () => {
      console.log("[token-server] listening on http://127.0.0.1:8765");
    });
    tokenServer.on("error", (err) => {
      // Port already in use or permission denied — not fatal, fallback to
      // localStorage polling which still works inside the built-in window.
      console.warn("[token-server] could not bind port 8765:", err.message);
    });

    // ── POWER EVENTS (lock / sleep / shutdown) ────────────────────────────
    try {
      // Lock screen — pause screenshots but keep session open
      powerMonitor.on("lock-screen", () => {
        isScreenLocked = true;
        pauseTrackerForLock();
      });
      powerMonitor.on("unlock-screen", () => {
        isScreenLocked = false;
        resumeTrackerAfterLock();
      });

      // Sleep / hibernate — clock out so the timer doesn't run all night
      powerMonitor.on("suspend", () => handleSuspend().catch(() => {}));
      powerMonitor.on("resume", () => handleResume());

      // System shutdown (macOS / Linux) — best-effort clock-out before OS kills us
      powerMonitor.on("shutdown", () => handleShutdown().catch(() => {}));
    } catch (e) {
      console.warn("[powerMonitor] event listeners unavailable:", e.message);
    }

    // Auto-updater — checks GitHub Releases for new versions
    setupAutoUpdater();

    if (authToken) {
      // Fire the FIRST status poll IMMEDIATELY so a restored / freshly-opened
      // session reflects clock state in ~1 network RTT, not after the 0-10s
      // stagger jitter that previously gated even the first poll.
      pollStatus();
      // Stagger only the RECURRING interval so simultaneous 9am opens don't all
      // hit the API on the same tick. startStatusPolling() fires one more poll
      // and installs the 60s interval; the renderer de-dupes any overlap.
      const jitter = Math.floor(Math.random() * 10000);
      setTimeout(() => startStatusPolling(), jitter);
      // Keep the restored session's access token fresh ahead of expiry. The
      // session loaded from disk this launch never went through saveSession,
      // so arm the proactive refresh explicitly here.
      armProactiveRefresh();
    }

    // Network monitor — clocks out after 5 min offline to prevent timer inflation
    startNetworkMonitor();

    // Safety-net: poll localStorage every 10s in case React login
    // doesn't call window.electronAuth.saveSession (backward compat)
    tokenTimer = setInterval(scheduleExtractSession, TOKEN_POLL_INTERVAL);
  });

  app.on("window-all-closed", () => {
    // Do NOT quit — stay in tray so tracking continues
  });

  app.on("activate", () => {
    // macOS: re-show window when dock icon clicked
    if (!mainWindow) createWindow();
    else {
      mainWindow.show();
      mainWindow.focus();
    }
  });

  app.on("before-quit", () => {
    isQuiting = true;
    clearInterval(statusTimer);
    clearInterval(tokenTimer);
    clearInterval(networkMonitorTimer);
    clearInterval(updateCheckTimer); // stop the 4h auto-update check timer
    clearProactiveTimers();          // stop proactive refresh + backoff timers
    // Best-effort clock-out on clean quit (fire-and-forget; process exits shortly)
    if (isTracking && authToken) {
      clockOutSilently(null).catch(() => {});
    }
    stopTracking();
  });
}

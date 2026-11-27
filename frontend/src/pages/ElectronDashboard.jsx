/**
 * ElectronDashboard — the slim, locked-down view rendered inside the
 * Temveris desktop (Electron) shell.
 *
 * Why this page exists:
 *   The full /dashboard page bundles sidebars, screenshots, admin links,
 *   reports, and a hundred other things that have no business inside the
 *   tracking shell. This page is intentionally tiny: timer, status,
 *   clock-in/out, break — nothing else. Heavier work (reports, leave,
 *   admin) opens in the user's real browser via window.open(), which the
 *   Electron main process forwards to shell.openExternal.
 *
 * Backend endpoints used (read-only list — see Dashboard.jsx for canonical
 * implementations):
 *   GET  /employees/me               – current state
 *   POST /employees/clock-in
 *   POST /employees/clock-out
 *   POST /employees/start-break
 *   POST /break/resume               – body {reason, activity}
 */

import { useEffect, useRef, useState, useCallback } from "react";
import { useWorkspace } from "../context/WorkspaceContext";
import { useNavigate } from "react-router-dom";
import { beginLogout } from "../api/authStorage";
import toast from "react-hot-toast";
import {
  LogOut,
  HelpCircle,
  Sun,
  Moon,
  FileText,
  CalendarDays,
  ArrowUpRight,
  User,
  Mail,
  MessageSquare,
  X,
  CheckCircle2,
  AlertCircle,
  Coffee,
  UtensilsCrossed,
  Download,
  Loader2,
} from "lucide-react";
import logo from "../assets/Temveris_Logo_PNG.png";
import logoWhite from "../assets/Temveris_Logo_White.svg";
import { API_BASE, WS_URL, PUBLIC_ORIGIN, fetchWithRetry } from "../config";
import EarlyClockOutModal, {
  EARLY_CLOCK_OUT_ENABLED,
  EARLY_REASON_MIN,
  EARLY_REASON_MAX,
} from "../components/EarlyClockOutModal";
import { getBreakThresholds, invalidateMe } from "../api/session";
// TEMPORARY: blink/reload investigation. Remove with the rest of the
// instrumentation once the root cause is confirmed.
import { blinkLog, isChunkMessage } from "../lib/blinkLog";

const WEBSITE_ORIGIN = PUBLIC_ORIGIN;
const DARK_STORAGE_KEY = "zt_electron_dark";

// Read the access token from BOTH storage tiers. "Remember this device" =
// unchecked stores the token in sessionStorage (see api/authStorage.js), so a
// localStorage-only read returned null for non-remembered logins — which made
// loadStatus() and every attendance call silently bail until the Electron main
// process happened to inject a token into localStorage on a later tick. That
// gap is what made a re-login take ~12s to refresh the worked-time display.
const getToken = () => {
  try {
    return (
      localStorage.getItem("zt_token") ||
      localStorage.getItem("token") ||
      sessionStorage.getItem("zt_token") ||
      sessionStorage.getItem("token") ||
      null
    );
  } catch {
    return null;
  }
};

// ── Silent token refresh (single-flight) ─────────────────────────────────────
// Access tokens expire ~every 30 min. Without a silent refresh, the FIRST
// attendance action or /employees/me poll after expiry 401s — which previously
// bounced the user to /login (white-screen reload). The long-lived refresh
// token lives ONLY in the Electron main process (OS-encrypted safeStorage); we
// ask it over IPC, mirror the fresh access token into localStorage, and let the
// caller replay. Single-flight so a burst of concurrent 401s shares one
// round-trip instead of spending the single-use refresh token multiple times.
let _desktopRefreshInFlight = null;
const refreshAccessTokenDesktop = () => {
  if (_desktopRefreshInFlight) return _desktopRefreshInFlight;
  _desktopRefreshInFlight = (async () => {
    try {
      if (!window.electronAuth?.refreshToken) return null;
      const r = await window.electronAuth.refreshToken();
      const t = r?.access_token || null;
      if (t) {
        try {
          localStorage.setItem("zt_token", t);
          localStorage.setItem("token", t);
        } catch {
          /* storage unavailable — main already injected it */
        }
      }
      return t;
    } catch {
      return null;
    } finally {
      _desktopRefreshInFlight = null;
    }
  })();
  return _desktopRefreshInFlight;
};

// Authenticated fetch used by EVERY attendance call. Attaches the bearer token,
// retries 503s (via fetchWithRetry), and on a 401 performs ONE silent refresh +
// replay so an access-token expiry during Clock In / Out / Break / status poll
// is completely invisible — the action just succeeds. It NEVER navigates or
// reloads: if the session is genuinely over, the Electron main process owns
// that logout. `path` is relative (e.g. "/employees/clock-in").
const apiFetch = async (path, opts = {}, retries = 2) => {
  // X-Client-Type marks this as the desktop shell. The backend's
  // is_desktop_client() reads it (falling back to the User-Agent) and the
  // desktop-only clock-in policy depends on it — without the header, detection
  // would rest on "Electron" appearing in the UA string, which is implicit and
  // one setUserAgent() call away from silently locking employees out. axios.js
  // already sends this; apiFetch uses raw fetch, so it must set it too.
  const withAuth = (t) => ({
    ...opts,
    headers: {
      ...(opts.headers || {}),
      Authorization: `Bearer ${t}`,
      ...(typeof window !== "undefined" && window.isDesktopApp === true
        ? { "X-Client-Type": "desktop" }
        : {}),
    },
  });
  let res = await fetchWithRetry(
    `${API_BASE}${path}`,
    withAuth(getToken()),
    retries,
  );
  if (res && res.status === 401) {
    const fresh = await refreshAccessTokenDesktop();
    if (fresh) {
      res = await fetchWithRetry(
        `${API_BASE}${path}`,
        withAuth(fresh),
        retries,
      );
    }
  }
  return res;
};

// Defensive against legacy "+00:00Z" timestamps (invalid ISO 8601). Without
// this the timer freezes because clockInMsRef.current ends up NaN.
const safeDate = (s) => {
  let v = String(s);
  if (v.endsWith("+00:00Z")) v = v.slice(0, -1);
  return new Date(v);
};

const fmtClock = (totalSecs) => {
  const s = Math.max(0, Math.floor(totalSecs));
  const hh = String(Math.floor(s / 3600)).padStart(2, "0");
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
};

// PERF: the live worked-seconds clock used to live in the 2,900-line
// ElectronDashboard body, so its 1s setInterval re-rendered the ENTIRE page
// every second. Isolating it into this tiny leaf means only the clock text
// re-renders each tick; the parent stays static between data changes. The
// timing refs are owned by the parent (mutated by clock-in/out/break) and
// passed in — refs are stable identities, so this is a pure read.
function LiveWorkedClock({
  clockedIn,
  onBreak,
  mode = "worked", // "worked" = live worked time · "break" = live current-break duration
  clockInMsRef,
  totalBreakMsRef,
  breakStartMsRef,
  prevWorkedSecsRef,
}) {
  const [elapsed, setElapsed] = useState(0);
  const tickRef = useRef(null);
  useEffect(() => {
    const tick = () => {
      const now = Date.now();
      if (mode === "break") {
        // Live duration of the break the user is currently on — counts up from
        // the moment "Take a Break" was hit so they can see how long it's run.
        const breakMs = breakStartMsRef.current
          ? now - breakStartMsRef.current
          : 0;
        setElapsed(Math.floor(Math.max(0, breakMs) / 1000));
        return;
      }
      if (!clockedIn || !clockInMsRef.current) {
        setElapsed(prevWorkedSecsRef.current);
        return;
      }
      const grossMs = now - clockInMsRef.current;
      const breakMs =
        totalBreakMsRef.current +
        (breakStartMsRef.current ? now - breakStartMsRef.current : 0);
      const workedMs = Math.max(0, grossMs - breakMs);
      setElapsed(prevWorkedSecsRef.current + Math.floor(workedMs / 1000));
    };
    if (tickRef.current) clearInterval(tickRef.current);
    tick(); // immediate update on state flip
    tickRef.current = setInterval(tick, 1000);
    return () => clearInterval(tickRef.current);
    // Refs (clockInMsRef etc.) are stable identities passed from the parent;
    // re-running only on clockedIn/onBreak/mode matches the original behaviour.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clockedIn, onBreak, mode]);
  return <>{fmtClock(elapsed)}</>;
}

const greeting = () => {
  const h = new Date().getHours();
  if (h < 12) return "Good Morning";
  if (h < 17) return "Good Afternoon";
  return "Good Evening";
};

const openInBrowser = (path) => {
  const url = WEBSITE_ORIGIN + (path.startsWith("/") ? path : "/" + path);
  // Electron's setWindowOpenHandler routes window.open → shell.openExternal,
  // so this opens in the user's real browser, not a new Electron window.
  window.open(url, "_blank", "noopener,noreferrer");
};

// ── Desktop update notification helpers ───────────────────────────────────
// These power the "Update Available" card surfaced ONLY when the page is
// rendered inside the Electron shell (window.isDesktopApp === true). Browser
// users never reach the fetch path because the effect bails on that gate.

// Parses a semver-shaped string ("1.0.7", "v1.0.11") into a comparable tuple.
// Used for both the UA-extracted current version and the backend-reported
// latest_version / minimum_supported_version.
const parseSemver = (s) => {
  const m = /^\s*v?(\d+)\.(\d+)\.(\d+)\s*$/i.exec(String(s || ""));
  if (!m) return null;
  return {
    major: +m[1],
    minor: +m[2],
    patch: +m[3],
    raw: `${+m[1]}.${+m[2]}.${+m[3]}`,
  };
};

// Pulls "X.Y.Z" out of `temveris-desktop/X.Y.Z` in the UA string. Returns
// null on a browser tab or an ancient build that doesn't stamp the UA.
const extractDesktopVersionFromUA = (ua) => {
  const m = /temveris-desktop\/(\d+\.\d+\.\d+)/i.exec(ua || "");
  return m ? parseSemver(m[1]) : null;
};

// Strict-less-than on the parsed tuple. NaN-safe via parseSemver guard.
const isOlder = (a, b) => {
  if (!a || !b) return false;
  if (a.major !== b.major) return a.major < b.major;
  if (a.minor !== b.minor) return a.minor < b.minor;
  return a.patch < b.patch;
};

// Snooze pointer is { version, until } so a NEW release re-prompts even
// when an older version is still inside its 3-day snooze window.
const UPDATE_SNOOZE_KEY = "zt_desktop_update_snooze";
const UPDATE_SNOOZE_DAYS = 3;

// How long after a user clock/break action loadStatus is allowed to skip
// the matching state slot. Picked empirically: 5 s comfortably covers the
// network round-trip + a brief backend cache window, while being short
// enough that genuine cross-device changes still show up promptly.
const RECONCILE_GRACE_MS = 5000;
// Server clock-in timestamps drift slightly from the client's Date.now()
// due to NTP skew + serialization latency. Reconcile the anchor only when
// the drift is large enough to be meaningful; below this threshold we keep
// the local anchor so the worked-time timer doesn't visibly jump every
// 15 s when /employees/me returns a fresh timestamp.
const CLOCK_ANCHOR_DRIFT_TOLERANCE_MS = 2000;
// When a break passes its allowance the worker gets a blocking modal. If they
// dismiss it with "Later" while still over, it re-appears after this interval
// so an over-limit break can never be silently ignored.
const OVERRUN_NAG_MS = 2 * 60 * 1000;

// Drops a short-lived ripple span at the click location. The `.zte-ripple`
// CSS class scales + fades it out via zteRippleOut, then we GC the node.
const spawnRipple = (e) => {
  try {
    const host = e.currentTarget;
    if (!host) return;
    const rect = host.getBoundingClientRect();
    const span = document.createElement("span");
    span.className = "zte-ripple";
    span.style.left = e.clientX - rect.left - 7 + "px";
    span.style.top = e.clientY - rect.top - 7 + "px";
    host.appendChild(span);
    setTimeout(() => span.remove(), 650);
  } catch {}
};

export default function ElectronDashboard() {
  const navigate = useNavigate();
  // Workspace-uploaded logo first; Temveris default (white in dark mode) as
  // fallback — see the top strip.
  const { workspaceLogo } = useWorkspace();

  // Tracking state — derived from /employees/me on mount, then updated
  // optimistically on each action and reconciled on the next /me call.
  const [name, setName] = useState(localStorage.getItem("name") || "");
  const [clockedIn, setClockedIn] = useState(false);
  const [onBreak, setOnBreak] = useState(false);
  // `elapsed` (live worked seconds) now lives in the <LiveWorkedClock/> leaf
  // so its 1s tick no longer re-renders this whole page. See component above.
  const [loading, setLoading] = useState(true);

  // Per-action in-flight flags that DRIVE THE BUTTON UI (disabled + spinner).
  // pendingClockRef/pendingBreakRef stay the synchronous re-entrancy guard (a
  // ref updates without a re-render); these mirror them as STATE so the button
  // can visibly show "working…" and block a second click for the whole network
  // round-trip. Phase-1 F3.
  const [clockBusy, setClockBusy] = useState(false);
  const [breakBusy, setBreakBusy] = useState(false);

  // Break-type picker — opened before /start-break so the worker chooses
  // between a short (~15m) break and a long (~60m) lunch; the type drives
  // which overtime threshold the backend uses.
  const [showBreakPicker, setShowBreakPicker] = useState(false);
  const [breakThresholds, setBreakThresholds] = useState({
    short_minutes: 15,
    lunch_minutes: 60,
  });
  // Part-time workers are only entitled to short breaks — the lunch option is
  // hidden for them (and rejected server-side as a backstop).
  const [isPartTime, setIsPartTime] = useState(false);
  // Live break-governance view from /employees/me (status chip / banner /
  // exceeded breakdown). Null when not on a break.
  const [breakGov, setBreakGov] = useState(null);
  // Blocking break-overrun modal. Shown the moment the live break crosses its
  // allowance; "Resume Work" ends the break, "Later" snoozes it for
  // OVERRUN_NAG_MS and it re-appears while the break is still over. Admin + HR
  // are alerted server-side by the break-overrun monitor — this is the
  // worker-facing half of that notification.
  const [showOverrunModal, setShowOverrunModal] = useState(false);
  // Controlled Clock-Out (Phase 2): populated with the backend's
  // EARLY_CLOCK_OUT_REASON_REQUIRED response when a reason is needed.
  const [earlyClockOut, setEarlyClockOut] = useState(null);
  const [earlyReason, setEarlyReason] = useState("");
  const [earlyError, setEarlyError] = useState("");
  const [earlySubmitting, setEarlySubmitting] = useState(false);
  const overrunSnoozeUntilRef = useRef(0);

  // ── "Need Help?" support form modal ──
  // Pre-populates name + work email from the user's profile so a typical
  // submission is just typing the issue and hitting Submit.
  const [helpOpen, setHelpOpen] = useState(false);
  const [helpName, setHelpName] = useState("");
  const [helpEmail, setHelpEmail] = useState("");
  const [helpIssue, setHelpIssue] = useState("");
  const [helpBusy, setHelpBusy] = useState(false);
  const [helpError, setHelpError] = useState("");
  const [helpDone, setHelpDone] = useState(false);

  // ── Desktop update notification state ──
  // Only meaningful when running inside Electron (gated by window.isDesktopApp).
  //   - installedDesktopVer : parsed from UA, e.g. {major:1, minor:0, patch:7}
  //   - latestDesktop       : full /desktop/latest response or null
  //   - snoozedUntil        : {version, until} read from localStorage; Later
  //                           writes a fresh object pinned to the LATEST
  //                           version, so a new release re-prompts immediately
  //   - updatePhase         : drives the 4-state native updater UI
  //                           "idle"        — popup shows backend release notes
  //                                           and the [Update Now] [Later] CTA
  //                           "downloading" — progress bar + percentage, CTA
  //                                           disabled (electron-updater is
  //                                           streaming the .nupkg into the
  //                                           app's cache directory)
  //                           "downloaded"  — [Restart and Update] [Later]
  //                           "installing"  — final paint before the window
  //                                           tears down for the NSIS swap
  //                           "error"       — surfaces the message + retry
  //   - updateProgress      : 0-100, driven by electron-updater progress events
  //   - downloadedVersion   : the version electron-updater actually staged
  //                           (may differ from latestDesktop.latest_version
  //                            if the backend bucket and Supabase publish
  //                            channel are momentarily out of sync)
  //   - updateError         : last error message from electron-updater
  const [installedDesktopVer, setInstalledDesktopVer] = useState(null);
  const [latestDesktop, setLatestDesktop] = useState(null);
  const [snoozedUntil, setSnoozedUntil] = useState(null);
  const [updatePhase, setUpdatePhase] = useState("idle");
  const [updateProgress, setUpdateProgress] = useState(0);
  const [downloadedVersion, setDownloadedVersion] = useState(null);
  const [updateError, setUpdateError] = useState(null);

  // Light/dark mode — persisted across launches.
  const [isDark, setIsDark] = useState(() => {
    try {
      return localStorage.getItem(DARK_STORAGE_KEY) === "1";
    } catch {
      return false;
    }
  });
  const toggleDark = () => {
    setIsDark((d) => {
      const next = !d;
      try {
        localStorage.setItem(DARK_STORAGE_KEY, next ? "1" : "0");
      } catch {}
      return next;
    });
  };

  // Refs — wall-clock anchors so the timer keeps ticking accurately even
  // if React re-renders skip a beat.
  const clockInMsRef = useRef(null);
  const breakStartMsRef = useRef(null);
  const totalBreakMsRef = useRef(0);
  const prevWorkedSecsRef = useRef(0);
  // Re-entrancy guards. Split per ACTION GROUP so an in-flight Clock In
  // doesn't bounce the user's next Take a Break click while the network
  // round-trip is still completing — the optimistic state has already
  // flipped, so the next click is semantically valid even mid-fetch. Each
  // ref blocks only its own action (e.g. rapid double-tap on Clock In).
  const pendingClockRef = useRef(false); // gates Clock In / Clock Out
  const pendingBreakRef = useRef(false); // gates Take a Break / Resume Work

  // Timestamps of the most recent successful click (NOT request completion).
  // Used by loadStatus to skip overwriting fresh optimistic state with stale
  // server state during the network round-trip — without this, a focus event
  // or the 15s reconcile fires /employees/me, the server hasn't processed
  // our action yet, and the UI flips back ("Clocked In" → "Not Clocked In"
  // → "Clocked In" within ~600 ms). 5 s is long enough for any reasonable
  // round-trip to complete on a flaky network without making the dashboard
  // feel stale to actions taken in other tabs/devices.
  const lastClockActionAtRef = useRef(0);
  const lastBreakActionAtRef = useRef(0);

  // ── Auth gate ──────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!getToken()) {
      blinkLog("auth-gate-redirect-login", {
        note: "ElectronDashboard mounted with no token — navigating to /login",
      });
      navigate("/login", { replace: true });
    }
  }, [navigate]);

  // ── Renderer crash + reload diagnostics ──────────────────────────────────────
  // The desktop shell must NEVER silently reload during attendance operations.
  // These listeners give us a forensic trail in the Electron console/log so any
  // future unexpected reload (or the renderer crash that would precede a white
  // screen) is attributable instead of invisible:
  //   • "error"             — uncaught renderer exceptions (would otherwise blank
  //                           the screen and trip the ErrorBoundary).
  //   • "unhandledrejection"— rejected promises (e.g. an un-awaited fetch).
  //   • "beforeunload"      — fires on ANY full document teardown (loadURL from
  //                           main, location.reload, location.assign). If this
  //                           logs during normal use, a reload slipped through.
  useEffect(() => {
    console.log(
      "[electron-dashboard] renderer initialized at",
      new Date().toISOString(),
    );
    blinkLog("dashboard-mounted");
    const onError = (e) => {
      console.error(
        "[electron-dashboard] uncaught error:",
        e?.message,
        e?.error?.stack || "",
      );
      const msg = (e?.message || e?.error?.message || "").toString();
      blinkLog(
        isChunkMessage(msg)
          ? "dashboard-chunk-error"
          : "dashboard-window-error",
        {
          message: msg.slice(0, 400),
          stack: e?.error?.stack
            ? String(e.error.stack).slice(0, 800)
            : undefined,
        },
      );
    };
    const onRejection = (e) => {
      console.error(
        "[electron-dashboard] unhandled rejection:",
        e?.reason?.message || e?.reason,
      );
      const msg = (e?.reason?.message || String(e?.reason || "")).toString();
      blinkLog(
        isChunkMessage(msg)
          ? "dashboard-chunk-rejection"
          : "dashboard-unhandled-rejection",
        {
          message: msg.slice(0, 400),
        },
      );
    };
    const onBeforeUnload = () => {
      console.warn(
        "[electron-dashboard] ⚠ document unloading (full reload/navigation) at",
        new Date().toISOString(),
      );
      blinkLog("dashboard-beforeunload");
    };
    window.addEventListener("error", onError);
    window.addEventListener("unhandledrejection", onRejection);
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => {
      window.removeEventListener("error", onError);
      window.removeEventListener("unhandledrejection", onRejection);
      window.removeEventListener("beforeunload", onBeforeUnload);
    };
  }, []);

  // ── Load current state from server ─────────────────────────────────────────
  // Pulls /employees/me and reconciles local optimistic state. The hot bug
  // this avoided previously was: focus event right after Clock In →
  // /employees/me responded BEFORE the clock-in fetch was processed →
  // setClockedIn(false) → UI flipped back to "Not clocked in" for ~500 ms
  // until the next reconcile picked up the truth. Same race fired for break
  // toggles. The fix is to skip the relevant state SLOT when the user took
  // that action recently (RECONCILE_GRACE_MS) or has a request in flight.
  // After the grace window we trust the server fully, so a clock-out from
  // another device still propagates here promptly.
  const loadStatus = useCallback(async () => {
    const tok = getToken();
    if (!tok) return false;
    try {
      // apiFetch silently refreshes + replays on a 401, so a normal token
      // expiry never reaches the !r.ok branch. A 401 that SURVIVES the refresh
      // means the session is genuinely over — and the Electron MAIN process
      // owns that logout (it reloads to /login). We must NOT navigate or reload
      // here: doing so races the main process and is what produced the
      // white-screen flicker when the window regained focus mid-session.
      const r = await apiFetch("/employees/me");
      if (!r || !r.ok) return false;
      const me = await r.json();
      if (me.name) setName(me.name);
      setIsPartTime(
        String(me.employment_type || "full_time")
          .trim()
          .toLowerCase()
          .replace(/[\s-]+/g, "_") === "part_time",
      );
      // Display-only governance view — safe to mirror unconditionally (no
      // optimistic-state conflict). Drives the "(Exceeded)" chip + banner.
      setBreakGov(me.on_break ? me.break_governance || null : null);

      const now = Date.now();
      const clockIsFresh =
        pendingClockRef.current ||
        now - lastClockActionAtRef.current < RECONCILE_GRACE_MS;
      const breakIsFresh =
        pendingBreakRef.current ||
        now - lastBreakActionAtRef.current < RECONCILE_GRACE_MS;

      // Banked worked seconds — the timer reads this as its base, then adds
      // the live session. Updating it during a fresh clock action would
      // clobber the optimistic bump we applied on clock-out, so gate it on
      // the same grace window.
      if (!clockIsFresh) {
        prevWorkedSecsRef.current = me.worked_today_seconds || 0;
      }

      if (me.clocked_in) {
        if (!clockIsFresh) {
          // Trust the server. Adjust the anchor only if it has drifted past
          // the tolerance — otherwise keep the local Date.now() anchor we
          // set on the optimistic click. This stops the timer from visibly
          // jumping every 15 s when /me returns a server-side timestamp
          // that's a few hundred milliseconds different from local.
          const serverMs = me.clock_in_time
            ? safeDate(me.clock_in_time).getTime()
            : Date.now();
          const currentMs = clockInMsRef.current;
          if (
            !currentMs ||
            Math.abs(currentMs - serverMs) > CLOCK_ANCHOR_DRIFT_TOLERANCE_MS
          ) {
            clockInMsRef.current = serverMs;
          }
          setClockedIn(true);
        }

        if (!breakIsFresh) {
          // The backend's break_elapsed_seconds INCLUDES the running break.
          // The local tick adds (now - breakStartMsRef) on top while on
          // break, so mirroring the full backend total would double-count
          // and the worked timer would tick BACKWARDS during a break. Fix:
          // store only completed-break time in totalBreakMsRef; the running
          // break is represented separately by breakStartMsRef.
          const totalBreakSecs = me.break_elapsed_seconds || 0;
          const currentBreakSecs = me.current_break_seconds || 0;
          if (me.on_break) {
            totalBreakMsRef.current =
              Math.max(0, totalBreakSecs - currentBreakSecs) * 1000;
            breakStartMsRef.current = Date.now() - currentBreakSecs * 1000;
            setOnBreak(true);
          } else {
            totalBreakMsRef.current = totalBreakSecs * 1000;
            breakStartMsRef.current = null;
            setOnBreak(false);
          }
        }
      } else if (!clockIsFresh) {
        // Server says not clocked in AND user hasn't acted recently. Trust it
        // and reset everything. The clockIsFresh guard is what stops the
        // "Clocked In → Not Clocked In → Clocked In" flicker right after a
        // Clock In click while the server is still processing.
        setClockedIn(false);
        setOnBreak(false);
        clockInMsRef.current = null;
        breakStartMsRef.current = null;
        totalBreakMsRef.current = 0;
      }
      return true; // reconciled against the server successfully
    } catch {
      // Network blip — keep showing last known state
      return false;
    } finally {
      setLoading(false);
    }
    // No deps: loadStatus no longer navigates on 401 (the silent refresh in
    // apiFetch + the main process own that), so it closes over nothing that
    // changes between renders.
  }, []);

  // ── Immediate sync layer (refreshEmployeeState) ───────────────────────────
  // ONE entry point for "fetch current clock / attendance / break / tracking
  // state NOW", callable from every trigger: login, session restore, token
  // refresh, wake-from-sleep, WebSocket event, window focus, and the
  // bootstrap/steady poll. It funnels into the SAME loadStatus() reconcile above
  // so the RECONCILE_GRACE_MS optimistic-state protection still applies (no
  // flicker), and it COALESCES concurrent triggers into a single /employees/me
  // request via an in-flight guard so the new fast paths can't stampede the
  // endpoint. Emits a reason-tagged SYNC_LATENCY_MS sample into the blink ring
  // buffer for P50/P95 analysis; set window.__ztSyncDiag = true to also log it
  // live in the console.
  const syncInFlightRef = useRef(null);
  const refreshEmployeeState = useCallback(
    (reason = "manual") => {
      if (syncInFlightRef.current) return syncInFlightRef.current; // coalesce → 1 request
      const t0 =
        typeof performance !== "undefined" ? performance.now() : Date.now();
      const p = (async () => {
        const ok = await loadStatus();
        const ms = Math.round(
          (typeof performance !== "undefined"
            ? performance.now()
            : Date.now()) - t0,
        );
        try {
          blinkLog("sync-latency", { reason, ms, ok }); // SYNC_LATENCY_MS sample
          if (typeof window !== "undefined" && window.__ztSyncDiag) {
            console.log(`[SYNC_LATENCY_MS] reason=${reason} ms=${ms} ok=${ok}`);
          }
        } catch {}
        return ok;
      })().finally(() => {
        syncInFlightRef.current = null;
      });
      syncInFlightRef.current = p;
      return p;
    },
    [loadStatus],
  );

  // Desktop update notification — bootstrap on mount.
  // Gate 1: window.isDesktopApp (set by Electron preload) — browsers skip
  //         the entire branch, never even firing the /desktop/latest fetch.
  // Gate 2: UA contains `temveris-desktop/X.Y.Z` — guards against the
  //         theoretical "Electron without our preload" case.
  // Snooze read happens BEFORE the fetch so a snoozed user pays only the
  // localStorage cost, never the network cost.
  useEffect(() => {
    if (typeof window === "undefined" || !window.isDesktopApp) return;
    const current = extractDesktopVersionFromUA(navigator.userAgent);
    if (!current) return;
    setInstalledDesktopVer(current);

    try {
      const raw = localStorage.getItem(UPDATE_SNOOZE_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed.until === "number" && parsed.version) {
          setSnoozedUntil(parsed);
        }
      }
    } catch {}

    let cancelled = false;
    // 8s ceiling: backend hits Supabase, which may itself be slow. We don't
    // want a hung update check to keep the React effect alive forever or
    // delay anything else on the dashboard.
    const ctrl =
      typeof AbortController !== "undefined" ? new AbortController() : null;
    const timer = setTimeout(() => {
      if (ctrl) ctrl.abort();
    }, 8000);
    fetch(`${API_BASE}/desktop/latest?platform=win`, {
      cache: "no-store",
      ...(ctrl ? { signal: ctrl.signal } : {}),
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (cancelled) return;
        if (d && d.latest_version) setLatestDesktop(d);
      })
      .catch(() => {
        // Offline or backend 503 (e.g. bucket not yet seeded). Hide the
        // banner silently — never break the dashboard over an update check.
      })
      .finally(() => {
        clearTimeout(timer);
      });

    return () => {
      cancelled = true;
      clearTimeout(timer);
      if (ctrl)
        try {
          ctrl.abort();
        } catch {}
    };
  }, []);

  // Pull workspace break overtime thresholds once on mount so the picker
  // labels match whatever the admin has set ("15m / 60m limit").
  useEffect(() => {
    const tok = getToken();
    if (!tok) return;
    getBreakThresholds()
      .then((d) => {
        if (d && (d.short_minutes || d.lunch_minutes)) {
          setBreakThresholds({
            short_minutes: Number(d.short_minutes) || 15,
            lunch_minutes: Number(d.lunch_minutes) || 60,
          });
        }
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    let cancelled = false;
    let slowId = null;
    let fastId = null;

    // Steady-state reconcile: every 30 s (increased from 15s to reduce request load
    // while WebSocket + main-process push remain the primary freshness sources).
    // This is now the SAFETY NET behind the WebSocket push + main-process force-refresh.
    const startSteady = () => {
      if (slowId || cancelled) return;
      slowId = setInterval(() => {
        if (!document.hidden) refreshEmployeeState("steady");
      }, 30_000);
    };

    // Bootstrap: after (re)mount, sync immediately; only if that first attempt
    // fails (token landing a beat late, a transient blip) do we retry on an
    // exponential backoff (1s, 2s, 4s) until the first success, then settle into
    // the steady cadence. Cap at 3 retries to avoid cascading load under sustained
    // auth failure. The force-refresh IPC push + WS onopen also drive immediate
    // sync, and refreshEmployeeState de-dupes them into ONE request.
    (async () => {
      if (await refreshEmployeeState("bootstrap")) {
        if (!cancelled) startSteady();
        return;
      }
      let tries = 0;
      let retryDelayMs = 1000;
      const scheduleRetry = () => {
        if (cancelled || tries >= 3) {
          if (!cancelled) startSteady();
          return;
        }
        setTimeout(async () => {
          if (cancelled) return;
          tries += 1;
          const ok = await refreshEmployeeState("bootstrap-retry");
          if (ok) {
            startSteady();
          } else if (tries < 3) {
            retryDelayMs *= 2;
            scheduleRetry();
          } else {
            startSteady();
          }
        }, retryDelayMs);
      };
      scheduleRetry();
    })();

    // Refetch immediately whenever the window regains focus or the user
    // switches back to this tab — cheapest way to make cross-window sync feel
    // instant.
    const onFocus = () => refreshEmployeeState("focus");
    const onVis = () => {
      if (!document.hidden) refreshEmployeeState("visibility");
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVis);
    return () => {
      cancelled = true;
      if (slowId) clearInterval(slowId);
      if (fastId) clearInterval(fastId);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [refreshEmployeeState]);

  // ── Main-process "sync now" push ──────────────────────────────────────────
  // The Electron MAIN process fires tracking:force-refresh on session restore,
  // login, silent token refresh, and wake-from-sleep (it owns those moments).
  // Re-sync IMMEDIATELY instead of waiting for the poll cadence. Pull-only
  // polling above stays as the failover. No-op in a plain browser.
  useEffect(() => {
    if (
      typeof window === "undefined" ||
      !window.electronTracking?.onForceRefresh
    )
      return;
    const off = window.electronTracking.onForceRefresh((reason) =>
      refreshEmployeeState(reason || "force-refresh"),
    );
    return () => {
      try {
        off && off();
      } catch {}
    };
  }, [refreshEmployeeState]);

  // ── Real-time WebSocket sync ──────────────────────────────────────────────
  // The full web Dashboard.jsx has always used the backend /ws push; the
  // Electron shell never did, so cross-device clock / break / attendance changes
  // only landed on the 15 s poll. Subscribe here so they land in <1 s. On
  // (re)connect we also sync once so a freshly-opened socket is never behind.
  // CRITICAL: never navigate/reload on socket error or auth failure — the
  // Electron MAIN process owns logout; we just reconnect with backoff and let
  // the 15 s poll cover the gap (mirrors the loadStatus 401 invariant).
  useEffect(() => {
    if (typeof WS_URL !== "string" || !WS_URL) return;
    let ws = null;
    let reconnectTimer = null;
    let tearing = false;
    let wsBackoff = 1000;
    const myUid = () => {
      try {
        return localStorage.getItem("user_id");
      } catch {
        return null;
      }
    };

    const connect = () => {
      if (tearing) return;
      let sock;
      try {
        sock = new WebSocket(
          `${WS_URL}?token=${encodeURIComponent(getToken() || "")}`,
        );
      } catch {
        if (!tearing) {
          const delay = wsBackoff;
          wsBackoff = Math.min(wsBackoff * 2, 60000);
          reconnectTimer = setTimeout(connect, delay);
        }
        return;
      }
      ws = sock;
      sock.onopen = () => {
        refreshEmployeeState("ws-open");
      };
      sock.onmessage = (evt) => {
        let data;
        try {
          data = JSON.parse(evt.data);
        } catch {
          return;
        }
        const t = data && data.type;
        if (!t || t === "ping") return;
        if (
          t === "clock_in" ||
          t === "clock_out" ||
          t === "break_started" ||
          t === "break_ended" ||
          t === "status_update" ||
          t === "time_adjustment_updated"
        ) {
          // Workspace-wide broadcast — react only to THIS employee's events so
          // a colleague clocking in doesn't trigger a needless fetch here.
          const uid = myUid();
          const evUid =
            data.user_id != null
              ? String(data.user_id)
              : data.employee_id != null
                ? String(data.employee_id)
                : null;
          if (uid && evUid && uid !== evUid) return;
          // Invalidate cache so next fetch gets fresh state
          invalidateMe();
          refreshEmployeeState(`ws-${t}`);
        }
      };
      sock.onclose = async (event) => {
        if (tearing) return;
        if (event.code === 1008) {
          // Electron: main process holds the refresh token; ask it to refresh.
          try {
            await refreshAccessTokenDesktop();
          } catch {
            /* treat as transient */
          }
          wsBackoff = 1000;
        }
        const delay = wsBackoff;
        wsBackoff = Math.min(wsBackoff * 2, 60000);
        reconnectTimer = setTimeout(connect, delay);
      };
      sock.onerror = () => {};
    };
    connect();

    return () => {
      tearing = true;
      wsBackoff = 1000;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      const s = ws;
      if (!s) return;
      if (s.readyState === WebSocket.CONNECTING) {
        const closeWhenReady = () => {
          try {
            s.close();
          } catch {}
        };
        s.addEventListener("open", closeWhenReady, { once: true });
        s.addEventListener("error", closeWhenReady, { once: true });
      } else {
        try {
          s.close();
        } catch {}
      }
    };
  }, [refreshEmployeeState]);

  // ── Local 1s timer ─────────────────────────────────────────────────────────
  // Moved into the <LiveWorkedClock/> leaf (defined above) so the per-second
  // tick re-renders only the clock text, not this entire page.

  // ── Actions ────────────────────────────────────────────────────────────────
  const handleClockIn = async () => {
    const tok = getToken();
    if (!tok) return;
    // F5: a click WHILE the previous clock action is still in flight is the
    // "double-click" case — acknowledge it instead of silently swallowing it.
    if (pendingClockRef.current) {
      toast("⏳ Hang tight — finishing your last action…", {
        id: "zt-clock-busy",
      });
      return;
    }
    if (clockedIn) return;
    pendingClockRef.current = true;
    setClockBusy(true);
    lastClockActionAtRef.current = Date.now();

    // ── Optimistic update — flip the UI to "working" instantly so the timer
    // starts ticking the moment the user clicks, instead of after the server
    // round-trip. The request runs in the background; we reconcile the timer
    // anchor with the server's authoritative clock-in time when it returns,
    // and roll the whole thing back if the request fails.
    clockInMsRef.current = Date.now();
    totalBreakMsRef.current = 0;
    breakStartMsRef.current = null;
    setClockedIn(true);
    setOnBreak(false);
    window.electronTracking?.updateStatus("working");

    try {
      const res = await apiFetch("/employees/clock-in", { method: "POST" });
      if (!res || !res.ok) {
        const e = await res.json().catch(() => ({}));
        throw new Error(e.detail || `Error ${res.status}`);
      }
      const d = await res.json();
      // Reconcile our optimistic anchor with the server's exact start time.
      if (d.clock_in_time)
        clockInMsRef.current = safeDate(d.clock_in_time).getTime();
      // Invalidate cache so next /me read gets fresh state
      invalidateMe();
      toast.success("Clocked in");
    } catch (e) {
      // Roll back — we never actually clocked in.
      setClockedIn(false);
      setOnBreak(false);
      clockInMsRef.current = null;
      breakStartMsRef.current = null;
      totalBreakMsRef.current = 0;
      window.electronTracking?.updateStatus("not_clocked_in");
      toast.error(`Clock-in failed: ${e.message}`);
    } finally {
      pendingClockRef.current = false;
      setClockBusy(false);
    }
  };

  // ── Optimistic update — freeze the timer at its current value instantly.
  // We bank a local estimate of today's worked seconds so the display holds
  // steady; the next /me reconcile (≤15s) replaces it with the server total.
  // Returns a snapshot so the change can be rolled back if the request fails
  // or the backend asks for an Early Clock Out reason.
  const _applyOptimisticClockOut = () => {
    const snap = {
      clockInMs: clockInMsRef.current,
      totalBreakMs: totalBreakMsRef.current,
      breakStartMs: breakStartMsRef.current,
      onBreak,
    };
    const now = Date.now();
    let bankedSecs = 0;
    if (snap.clockInMs) {
      const grossMs = now - snap.clockInMs;
      const breakMs =
        snap.totalBreakMs + (snap.breakStartMs ? now - snap.breakStartMs : 0);
      bankedSecs = Math.max(0, Math.floor((grossMs - breakMs) / 1000));
    }
    prevWorkedSecsRef.current += bankedSecs;
    setClockedIn(false);
    setOnBreak(false);
    clockInMsRef.current = null;
    breakStartMsRef.current = null;
    totalBreakMsRef.current = 0;
    window.electronTracking?.updateStatus("not_clocked_in");
    return { snap, bankedSecs };
  };

  // Undo an optimistic clock-out — restores the pre-request state; we're still
  // clocked in.
  const _rollbackClockOut = (snap, bankedSecs) => {
    prevWorkedSecsRef.current = Math.max(0, prevWorkedSecsRef.current - bankedSecs);
    clockInMsRef.current = snap.clockInMs;
    totalBreakMsRef.current = snap.totalBreakMs;
    breakStartMsRef.current = snap.breakStartMs;
    setOnBreak(snap.onBreak);
    setClockedIn(true);
    window.electronTracking?.updateStatus(snap.onBreak ? "on_break" : "working");
  };

  // POST /employees/clock-out, optionally with a typed Early Clock Out reason.
  // Returns the parsed JSON; throws on a non-OK HTTP status.
  const _postClockOut = async (reasonText) => {
    const opts = { method: "POST" };
    if (reasonText != null) {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify({ reason: reasonText });
    }
    const res = await apiFetch("/employees/clock-out", opts);
    if (!res || !res.ok) {
      const e = await res.json().catch(() => ({}));
      throw new Error(e.detail || `Error ${res?.status}`);
    }
    return await res.json().catch(() => ({}));
  };

  const handleClockOut = async () => {
    const tok = getToken();
    if (!tok) return;
    if (pendingClockRef.current) {
      toast("⏳ Hang tight — finishing your last action…", {
        id: "zt-clock-busy",
      });
      return;
    }
    if (!clockedIn) return;
    pendingClockRef.current = true;
    setClockBusy(true);
    lastClockActionAtRef.current = Date.now();

    const { snap, bankedSecs } = _applyOptimisticClockOut();

    try {
      const d = await _postClockOut(null);
      // TEMPORARILY DISABLED: Early Clock Out is inactive, so this branch never
      // runs (the backend gate is off and no longer returns the code). Kept for
      // future re-enable — see EARLY_CLOCK_OUT_ENABLED in EarlyClockOutModal.
      if (EARLY_CLOCK_OUT_ENABLED && d && d.code === "EARLY_CLOCK_OUT_REASON_REQUIRED") {
        // Below required hours on a non-flexible day — undo the optimistic
        // clock-out (stay clocked in) and collect a typed reason via the modal.
        _rollbackClockOut(snap, bankedSecs);
        setEarlyClockOut(d);
        setEarlyReason("");
        setEarlyError("");
        return;
      }
      // Invalidate cache so next /me read gets fresh state
      invalidateMe();
      toast.success("Clocked out");
    } catch (e) {
      _rollbackClockOut(snap, bankedSecs);
      toast.error(`Clock-out failed: ${e.message}`);
    } finally {
      pendingClockRef.current = false;
      setClockBusy(false);
    }
  };

  // Early Clock Out modal — submit the typed reason and complete the clock-out.
  const submitEarlyClockOut = async () => {
    const reason = (earlyReason || "").trim();
    if (reason.length < EARLY_REASON_MIN) {
      setEarlyError(`Please enter at least ${EARLY_REASON_MIN} characters.`);
      return;
    }
    if (reason.length > EARLY_REASON_MAX) {
      setEarlyError(`Reason must be ${EARLY_REASON_MAX} characters or fewer.`);
      return;
    }
    if (pendingClockRef.current) return;
    pendingClockRef.current = true;
    setClockBusy(true);
    setEarlySubmitting(true);
    setEarlyError("");

    const { snap, bankedSecs } = _applyOptimisticClockOut();
    try {
      const d = await _postClockOut(reason);
      if (d && d.code === "EARLY_CLOCK_OUT_REASON_REQUIRED") {
        // Server rejected the reason (still invalid) — stay clocked in.
        _rollbackClockOut(snap, bankedSecs);
        setEarlyError(d.error || "Please provide a valid reason.");
        return;
      }
      setEarlyClockOut(null);
      setEarlyReason("");
      invalidateMe();
      toast.success("Clocked out");
    } catch (e) {
      _rollbackClockOut(snap, bankedSecs);
      setEarlyError(e.message || "Clock-out failed");
    } finally {
      setEarlySubmitting(false);
      pendingClockRef.current = false;
      setClockBusy(false);
    }
  };

  const cancelEarlyClockOut = () => {
    // Already rolled back to clocked-in; just close the modal.
    setEarlyClockOut(null);
    setEarlyReason("");
    setEarlyError("");
  };

  const handleBreakToggle = async (breakType = "break") => {
    const tok = getToken();
    if (!tok) return;
    if (pendingBreakRef.current) {
      toast("⏳ Hang tight — finishing your last action…", {
        id: "zt-break-busy",
      });
      return;
    }
    if (!clockedIn) return;
    pendingBreakRef.current = true;
    setBreakBusy(true);
    lastBreakActionAtRef.current = Date.now();

    const goingOnBreak = !onBreak;
    // Snapshot break anchors for rollback.
    const snap = {
      breakStartMs: breakStartMsRef.current,
      totalBreakMs: totalBreakMsRef.current,
    };

    // ── Optimistic update — toggle the break state instantly so the worked
    // timer pauses/resumes the moment the user clicks. The request runs in the
    // background and we roll back if it fails.
    if (goingOnBreak) {
      breakStartMsRef.current = Date.now();
      setOnBreak(true);
      window.electronTracking?.updateStatus("on_break");
    } else {
      if (breakStartMsRef.current !== null) {
        totalBreakMsRef.current += Date.now() - breakStartMsRef.current;
        breakStartMsRef.current = null;
      }
      setOnBreak(false);
      window.electronTracking?.updateStatus("working");
    }

    try {
      let res;
      if (goingOnBreak) {
        res = await apiFetch("/employees/start-break", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            break_type: breakType === "lunch" ? "lunch" : "break",
          }),
        });
      } else {
        // Mirror Dashboard.jsx: short break (<1h) resumes silently with a
        // generic reason; long break would normally pop a modal — but the
        // Electron shell stays minimal, so we use the same default here.
        res = await apiFetch("/break/resume", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            reason: "Short break",
            activity: "Short break",
          }),
        });
      }
      if (!res || !res.ok) {
        const e = await res.json().catch(() => ({}));
        throw new Error(e.detail || e.message || `Error ${res.status}`);
      }
      // Governed denial returns HTTP 200 with { allowed:false, code, message }
      // (e.g. DAILY_SHORT_BREAK_ALREADY_USED, LUNCH_BREAK_NOT_ALLOWED). Treat it
      // as a failure so the optimistic state rolls back and the worker sees why.
      if (goingOnBreak) {
        const d = await res.json().catch(() => ({}));
        if (d && (d.allowed === false || d.success === false)) {
          throw new Error(d.message || d.detail || "Break not allowed");
        }
      }
      // Invalidate cache so next /me read gets fresh state
      invalidateMe();
    } catch (e) {
      // Roll back to the pre-click break state.
      breakStartMsRef.current = snap.breakStartMs;
      totalBreakMsRef.current = snap.totalBreakMs;
      setOnBreak(!goingOnBreak);
      window.electronTracking?.updateStatus(
        goingOnBreak ? "working" : "on_break",
      );
      toast.error(e.message || "Break action failed");
    } finally {
      pendingBreakRef.current = false;
      setBreakBusy(false);
    }
  };

  // ── Break-overrun modal control ───────────────────────────────────────────
  // Driven off the governance view from /employees/me (reconciled every ~15s).
  // While the break is over its allowance, surface the modal unless the worker
  // snoozed it; the interval re-checks so a snoozed modal re-appears after
  // OVERRUN_NAG_MS, and a resumed/ended break clears it immediately.
  const breakExceeded = !!(
    onBreak &&
    breakGov &&
    (breakGov.notification_due ?? breakGov.exceeded)
  );
  useEffect(() => {
    if (!breakExceeded) {
      setShowOverrunModal(false);
      overrunSnoozeUntilRef.current = 0;
      return;
    }
    const evaluate = () => {
      if (Date.now() >= overrunSnoozeUntilRef.current)
        setShowOverrunModal(true);
    };
    evaluate();
    const iv = setInterval(evaluate, 10_000);
    return () => clearInterval(iv);
  }, [breakExceeded]);

  const snoozeOverrun = () => {
    overrunSnoozeUntilRef.current = Date.now() + OVERRUN_NAG_MS;
    setShowOverrunModal(false);
  };
  const resumeFromOverrun = () => {
    overrunSnoozeUntilRef.current = 0;
    setShowOverrunModal(false);
    handleBreakToggle(); // no arg → resume (ends the active break)
  };

  const handleLogout = () => {
    // Dedupe: if a logout is already running (e.g. a 401 interceptor logout
    // fired first, or a double-click), don't start a second teardown + nav.
    if (!beginLogout()) return;
    const preserve = [
      "zt_remembered_email",
      "zt_theme_dark",
      "zt_lang_chosen",
      DARK_STORAGE_KEY,
    ];
    const saved = {};
    preserve.forEach((k) => {
      const v = localStorage.getItem(k);
      if (v !== null) saved[k] = v;
    });
    localStorage.clear();
    Object.keys(saved).forEach((k) => localStorage.setItem(k, saved[k]));
    blinkLog("user-logout", { note: "user clicked Logout" });
    // We own navigation: do the smooth in-document React-Router nav below and
    // tell main to skip its hard reload (that reload is the white-screen glitch).
    window.electronAuth?.logout?.({ rendererNavigates: true });
    navigate("/login", { replace: true });
  };

  // ── Open the help modal with sensible defaults ──
  const openHelp = () => {
    let storedEmail = "";
    try {
      storedEmail =
        localStorage.getItem("email") ||
        localStorage.getItem("user_email") ||
        localStorage.getItem("zt_remembered_email") ||
        "";
    } catch {}
    setHelpName(name || "");
    setHelpEmail(storedEmail);
    setHelpIssue("");
    setHelpError("");
    setHelpDone(false);
    setHelpOpen(true);
  };

  const closeHelp = () => {
    if (helpBusy) return;
    setHelpOpen(false);
  };

  // Close on Escape while the modal is open. Bail out of the listener
  // entirely when closed so we don't waste a keydown handler.
  useEffect(() => {
    if (!helpOpen) return;
    const onKey = (e) => {
      if (e.key === "Escape") closeHelp();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [helpOpen, helpBusy]);

  const submitHelp = async () => {
    if (helpBusy) return;
    const n = helpName.trim();
    const em = helpEmail.trim();
    const iss = helpIssue.trim();

    if (!n) {
      setHelpError("Please enter your name.");
      return;
    }
    if (!em || !/^\S+@\S+\.\S+$/.test(em)) {
      setHelpError("Please enter a valid work email.");
      return;
    }
    if (iss.length < 10) {
      setHelpError(
        "Please describe the issue in a bit more detail (10+ characters).",
      );
      return;
    }

    setHelpError("");
    setHelpBusy(true);
    try {
      const tok = getToken();
      const res = await fetchWithRetry(
        `${API_BASE}/worker/help/contact-support`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${tok}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            name: n,
            work_email: em,
            issue: iss,
            source: "desktop-tracker",
          }),
        },
      );
      if (!res.ok) {
        const j = await res.json().catch(() => ({}));
        throw new Error(j.detail || `Error ${res.status}`);
      }
      await res.json().catch(() => ({}));
      setHelpDone(true);
      // The send runs in a backend BackgroundTask, so a successful 201
      // just means the ticket was queued. Phrase the toast accordingly.
      toast.success("Sent to support — they'll get back to you soon.");
      // Auto-dismiss the modal after a beat so the user sees the success state.
      setTimeout(() => setHelpOpen(false), 1400);
    } catch (e) {
      setHelpError(e.message || "Couldn't send. Try again in a moment.");
    } finally {
      setHelpBusy(false);
    }
  };

  // ── Desktop update notification — derived state + actions ──
  // latestVer is null until the server response arrives, which the
  // showUpdateNotice expression handles safely (isOlder is null-tolerant).
  const latestVer = latestDesktop
    ? parseSemver(latestDesktop.latest_version)
    : null;
  const minSupportedVer = latestDesktop
    ? parseSemver(latestDesktop.minimum_supported_version)
    : null;

  // True when the running build is older than the latest published one.
  const isOutdated = isOlder(installedDesktopVer, latestVer);

  // True when the user clicked Later within the snooze window AND that
  // snooze is still pinned to the CURRENT latest version. Bumping the
  // server version invalidates an active snooze automatically.
  const snoozeIsActive =
    !!snoozedUntil &&
    !!latestDesktop &&
    snoozedUntil.version === latestDesktop.latest_version &&
    Date.now() < snoozedUntil.until;

  // Force-update flag — exposed to the UI but not consumed today (spec:
  // "Allow Later button. Do NOT force updates" for the initial rollout).
  // Wired so a future minimum_supported_version bump turns the Later button
  // off without another React deploy.
  const isForceRequired = isOlder(installedDesktopVer, minSupportedVer);

  // The popup stays visible while the user is mid-flow (downloading /
  // downloaded / installing / error) even if they previously snoozed —
  // they've already committed to the update at that point.
  const showUpdateNotice =
    updatePhase !== "idle" ||
    !!(installedDesktopVer && latestDesktop && isOutdated && !snoozeIsActive);

  // Detect whether the new (v1.0.12+) native updater bridge is present. Older
  // shells that don't expose `window.electronUpdater` fall back to the legacy
  // window.open flow so we can roll the React bundle out before every
  // employee has installed the new Electron binary.
  const hasNativeUpdater =
    typeof window !== "undefined" && !!window.electronUpdater?.download;

  // ── Subscribe to native updater events ───────────────────────────────────
  // The main process forwards every electron-updater event over IPC as a
  // {state, ...extra} payload. We translate each into the four UI states.
  // Subscription happens once on mount; the returned unsubscribe is fired on
  // unmount so we don't leak ipcRenderer listeners.
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!window.electronUpdater?.onStatus) return;

    const unsub = window.electronUpdater.onStatus((p) => {
      if (!p || typeof p !== "object") return;

      if (p.state === "available") {
        // electron-updater confirmed there's a real downloadable release.
        // If `/desktop/latest` hasn't responded yet, seed the popup so the
        // user isn't stuck looking at an empty card.
        if (p.info?.version) {
          setLatestDesktop(
            (d) =>
              d || {
                latest_version: p.info.version,
                release_notes: Array.isArray(p.info.releaseNotes)
                  ? p.info.releaseNotes
                      .map((n) => (typeof n === "string" ? n : n?.note || ""))
                      .filter(Boolean)
                  : typeof p.info.releaseNotes === "string"
                    ? [p.info.releaseNotes]
                    : [],
                download_url: null,
              },
          );
        }
      } else if (p.state === "progress") {
        setUpdatePhase("downloading");
        setUpdateProgress(typeof p.percent === "number" ? p.percent : 0);
        setUpdateError(null);
      } else if (p.state === "downloaded") {
        setUpdatePhase("downloaded");
        setUpdateProgress(100);
        setUpdateError(null);
        if (p.info?.version) setDownloadedVersion(p.info.version);
      } else if (p.state === "installing") {
        setUpdatePhase("installing");
      } else if (p.state === "error") {
        setUpdatePhase("error");
        setUpdateError(p.message || "Update failed. Please try again.");
      }
    });

    return () => {
      try {
        unsub && unsub();
      } catch {}
    };
  }, []);

  const onUpdateNow = () => {
    // Preferred path: native electron-updater download (in-app, no browser,
    // no Chrome warning, no manual installer).
    if (hasNativeUpdater) {
      setUpdatePhase("downloading");
      setUpdateProgress(0);
      setUpdateError(null);
      window.electronUpdater.download().catch((e) => {
        setUpdatePhase("error");
        setUpdateError(e?.message || "Could not start the download.");
      });
      return;
    }

    // Legacy fallback for v1.0.7-style shells that don't expose
    // window.electronUpdater yet. Once every employee is on the new build,
    // this branch becomes unreachable and can be deleted.
    if (latestDesktop?.download_url) {
      window.open(latestDesktop.download_url, "_blank", "noopener,noreferrer");
      setUpdatePhase("downloading"); // best-effort visual cue
    }
  };

  const onRestartAndUpdate = () => {
    if (!window.electronUpdater?.quitAndInstall) return;
    // Optimistic local flip — the main process also broadcasts "installing"
    // a tick later, but flipping locally first avoids a perceptible delay
    // between click and feedback.
    setUpdatePhase("installing");
    try {
      window.electronUpdater.quitAndInstall();
    } catch {}
  };

  const onLater = () => {
    if (!latestDesktop) return;
    const payload = {
      version: latestDesktop.latest_version,
      until: Date.now() + UPDATE_SNOOZE_DAYS * 24 * 60 * 60 * 1000,
    };
    try {
      localStorage.setItem(UPDATE_SNOOZE_KEY, JSON.stringify(payload));
    } catch {}
    setSnoozedUntil(payload);
    // If the user clicks Later AFTER the download has staged, keep the local
    // updatePhase as "downloaded" so the card stays available — `autoInstall
    // OnAppQuit` will still apply it on the next manual quit, and they can
    // come back and click Restart and Update any time before then.
  };

  // ── UI ─────────────────────────────────────────────────────────────────────
  const statusText = onBreak
    ? "on break"
    : clockedIn
      ? "working"
      : "not clocked in";
  const statusColor = onBreak ? "#F59E0B" : clockedIn ? "#22C55E" : "#9CA3AF";
  const firstName = (name || "").split(" ")[0] || "there";
  const initial = (firstName[0] || "U").toUpperCase();
  const team = (() => {
    try {
      return localStorage.getItem("department") || "Team";
    } catch {
      return "Team";
    }
  })();

  return (
    <div className={`zte-shell ${isDark ? "dark" : ""}`}>
      {/* React 19 children reconciliation of a long template literal inside
          <style> was flashing the raw CSS as visible body text inside the
          Electron shell (the entire dashboard stylesheet rendered as
          plain text starting with "/* Stylish widget popup …" before the
          element's default display:none kicked in). dangerouslySetInnerHTML
          writes the CSS into innerHTML in one step and avoids the flash. */}
      <style
        dangerouslySetInnerHTML={{
          __html: `
        /* Stylish widget popup — gradient tracker card, glass-morphic
           accents, animated status pulses. Light + dark variants share
           every layout rule; only the colour tokens swap. */

        /* ── Light theme tokens ─────────────────────────────────────── */
        .zte-shell {
          --bg:           #F5F7FB;
          --surface:      #FFFFFF;
          --soft:         #F1F5F9;
          --divider:      rgba(15, 23, 42, 0.08);
          --border:       rgba(15, 23, 42, 0.10);
          --text:         #0F172A;
          --text-sub:     #475569;
          --text-muted:   #6B7280;
          --brand:        #11496A;
          --brand-2:      #1E5C7F;
          --brand-soft:   rgba(17,73,106,0.08);
          --accent:       #44B97B;
          --accent-2:     #38A06B;
          --status:       #16A34A;
          --break:        #F59E0B;
          --break-2:      #D97706;
          --break-text:   #FFFFFF;
          --danger:       #EF4444;
          --danger-2:     #DC2626;
          --danger-text:  #FFFFFF;
          --help-grad:    linear-gradient(135deg, #11496A 0%, #1E5C7F 100%);
          --help-text:    #FFFFFF;
          --shadow-sm:    0 2px 8px rgba(15, 23, 42, 0.06);
          --shadow-md:    0 6px 20px rgba(15, 23, 42, 0.08);
          --shadow-lg:    0 14px 32px -8px rgba(17, 73, 106, 0.28);
          --widget-bg:    linear-gradient(135deg, #FFFFFF 0%, #F8FAFC 100%);
          --avatar-bg:    #E7F6F0; /* soft mint for the initials chip */
          --blob-1:       rgba(68, 185, 123, 0.14);
          --blob-2:       rgba(17, 73, 106, 0.10);
          --status-halo:  rgba(22, 163, 74, 0.18);
          --break-halo:   rgba(245, 158, 11, 0.22);
          --offline-halo: rgba(148, 163, 184, 0.18);
          --grid-dot:     rgba(17, 73, 106, 0.07);
          --ring-grad:    conic-gradient(from 0deg,
                            #44B97B, #11496A, #5DC9B0, #44B97B);
          --divider-grad: linear-gradient(90deg,
                            transparent 0%,
                            rgba(17,73,106,0.18) 50%,
                            transparent 100%);
          --chip-bg:      rgba(22, 163, 74, 0.10);
          --chip-bg-2:    rgba(22, 163, 74, 0.22);
          --break-chip-bg: rgba(245, 158, 11, 0.12);
          --break-chip-bg-2: rgba(245, 158, 11, 0.26);
        }

        /* ── Dark theme tokens — neutral near-blacks (no blue cast) so the
              Electron mini-window matches the web dashboard's pure-black
              dark mode instead of slate-blue. ──────────────────────────── */
        .zte-shell.dark {
          --bg:           #0A0A0A;
          --surface:      #141414;
          --soft:         #1F1F1F;
          --divider:      rgba(255,255,255,0.08);
          --border:       rgba(255,255,255,0.12);
          --text:         #F5F5F5;
          --text-sub:     #D4D4D4;
          --text-muted:   #A3A3A3;
          --brand:        #5DC9B0;
          --brand-2:      #3FA890;
          --brand-soft:   rgba(93,201,176,0.12);
          --accent:       #34D399;
          --accent-2:     #10B981;
          --status:       #34D399;
          --break:        #F59E0B;
          --break-2:      #D97706;
          --break-text:   #1F2937;
          --danger:       #F87171;
          --danger-2:     #EF4444;
          --danger-text:  #FFFFFF;
          --help-grad:    linear-gradient(135deg, #5DC9B0 0%, #3FA890 100%);
          --help-text:    #0A1224;
          --shadow-sm:    0 2px 8px rgba(0, 0, 0, 0.28);
          --shadow-md:    0 6px 20px rgba(0, 0, 0, 0.35);
          --shadow-lg:    0 14px 32px -8px rgba(93, 201, 176, 0.28);
          --widget-bg:    linear-gradient(135deg, #141414 0%, #0A0A0A 100%);
          --avatar-bg:    rgba(93, 201, 176, 0.18); /* mint glow in dark mode */
          --blob-1:       rgba(93, 201, 176, 0.10);
          --blob-2:       rgba(255, 255, 255, 0.04);
          --status-halo:  rgba(52, 211, 153, 0.22);
          --break-halo:   rgba(245, 158, 11, 0.25);
          --offline-halo: rgba(148, 163, 184, 0.18);
          --grid-dot:     rgba(148, 163, 184, 0.10);
          --ring-grad:    conic-gradient(from 0deg,
                            #34D399, #5DC9B0, #3FA890, #34D399);
          --divider-grad: linear-gradient(90deg,
                            transparent 0%,
                            rgba(148,163,184,0.22) 50%,
                            transparent 100%);
          --chip-bg:      rgba(52, 211, 153, 0.14);
          --chip-bg-2:    rgba(52, 211, 153, 0.26);
          --break-chip-bg: rgba(245, 158, 11, 0.16);
          --break-chip-bg-2: rgba(245, 158, 11, 0.30);
        }

        html, body, #root { height: 100%; margin: 0; }

        .zte-shell {
          min-height: 100vh;
          display: flex;
          flex-direction: column;
          background: var(--bg);
          color: var(--text);
          font-family: 'DM Sans', system-ui, sans-serif;
          position: relative;
          overflow: hidden;
          transition: background 240ms ease, color 240ms ease;
        }

        /* Ambient gradient blobs — soft, low-opacity, slowly drifting */
        @keyframes zteBlobDriftA {
          0%,100% { transform: translate(0, 0)        scale(1); }
          50%     { transform: translate(-26px, 18px) scale(1.08); }
        }
        @keyframes zteBlobDriftB {
          0%,100% { transform: translate(0, 0)        scale(1); }
          50%     { transform: translate(22px, -16px) scale(1.06); }
        }
        .zte-ambient {
          position: absolute;
          inset: 0;
          pointer-events: none;
          z-index: 0;
          overflow: hidden;
        }
        .zte-ambient::before,
        .zte-ambient::after {
          content: "";
          position: absolute;
          width: 320px;
          height: 320px;
          border-radius: 50%;
          filter: blur(80px);
          will-change: transform;
        }
        .zte-ambient::before {
          top: -130px;
          right: -100px;
          background: var(--blob-1);
          animation: zteBlobDriftA 14s ease-in-out infinite;
        }
        .zte-ambient::after {
          bottom: -160px;
          left: -110px;
          background: var(--blob-2);
          animation: zteBlobDriftB 16s ease-in-out infinite;
        }
        /* Tiny dot-grid texture overlay — barely visible, adds depth */
        .zte-grid {
          position: absolute;
          inset: 0;
          pointer-events: none;
          z-index: 0;
          background-image: radial-gradient(var(--grid-dot) 1px, transparent 1px);
          background-size: 22px 22px;
          opacity: 0.55;
          mask-image: radial-gradient(ellipse at center, #000 35%, transparent 75%);
          -webkit-mask-image: radial-gradient(ellipse at center, #000 35%, transparent 75%);
        }
        .zte-shell > *:not(.zte-ambient):not(.zte-grid) { position: relative; z-index: 1; }
        .zte-shell.dark .zte-logo { filter: brightness(1.15) saturate(1.10); }
        .zte-logo {
          transition: transform .35s cubic-bezier(.34,1.56,.64,1), filter .35s ease;
        }
        .zte-logo:hover {
          transform: scale(1.05);
          filter: brightness(1.08) saturate(1.1) drop-shadow(0 4px 10px rgba(17,73,106,0.25));
        }
        @media (prefers-reduced-motion: reduce) {
          .zte-ambient::before, .zte-ambient::after { animation: none; }
        }

        /* ── Top strip — logo left, theme + logout right ─────────────── */
        .zte-topbar {
          display: flex;
          align-items: center;
          justify-content: space-between;
          padding: 14px 22px 12px;
        }
        .zte-top-actions {
          display: flex; align-items: center; gap: 10px;
        }
        .zte-icon-btn {
          width: 34px; height: 34px;
          border-radius: 10px;
          border: 1px solid var(--border);
          background: var(--surface);
          color: var(--text-sub);
          display: inline-flex; align-items: center; justify-content: center;
          cursor: pointer;
          transition:
            transform .25s cubic-bezier(.34,1.56,.64,1),
            background .2s ease,
            color .2s ease,
            border-color .2s ease,
            box-shadow .2s ease;
        }
        .zte-icon-btn svg {
          transition: transform .35s cubic-bezier(.34,1.56,.64,1);
        }
        .zte-icon-btn:hover {
          background: var(--brand-soft);
          color: var(--brand);
          border-color: var(--brand);
          transform: translateY(-2px) scale(1.08);
          box-shadow: var(--shadow-md);
        }
        .zte-icon-btn:hover svg { transform: rotate(-12deg) scale(1.1); }
        .zte-icon-btn:active { transform: translateY(0) scale(.95); }

        /* ── Main tracker row — flat layout, no card wrapper ─────────── */
        .zte-tracker {
          display: flex;
          align-items: center;
          gap: 14px;
          padding: 6px 22px 8px;
        }

        /* Avatar — soft mint circle, rotating conic ring while clocked in */
        @keyframes zteRingSpin {
          to { transform: rotate(360deg); }
        }
        .zte-avatar {
          position: relative;
          flex-shrink: 0;
          width: 46px; height: 46px;
          border-radius: 50%;
          background: var(--avatar-bg);
          color: var(--brand);
          display: flex; align-items: center; justify-content: center;
          font-size: 14px;
          font-weight: 700;
          text-transform: uppercase;
          letter-spacing: 0.5px;
          transition: transform .35s cubic-bezier(.34,1.56,.64,1);
        }
        /* Rotating conic-gradient ring — visible only when clocked in */
        .zte-avatar::before {
          content: "";
          position: absolute;
          inset: -3px;
          border-radius: 50%;
          background: var(--ring-grad);
          padding: 2px;
          z-index: 0;
          opacity: 0;
          transition: opacity .3s ease;
          /* Donut mask: outer ring visible, inner cut out */
          -webkit-mask:
            linear-gradient(#000 0 0) content-box,
            linear-gradient(#000 0 0);
          -webkit-mask-composite: xor;
                  mask:
            linear-gradient(#000 0 0) content-box,
            linear-gradient(#000 0 0);
                  mask-composite: exclude;
          animation: zteRingSpin 6s linear infinite;
        }
        .zte-avatar.is-live::before { opacity: 1; }
        .zte-avatar.is-break::before {
          opacity: 1;
          background: conic-gradient(from 0deg, #F59E0B, #D97706, #FBBF24, #F59E0B);
        }
        .zte-avatar:hover { transform: scale(1.06); }
        .zte-avatar > * { position: relative; z-index: 1; }
        .zte-status-dot {
          position: absolute;
          bottom: -1px; right: -1px;
          width: 12px; height: 12px;
          border-radius: 50%;
          border: 2.5px solid var(--surface);
          z-index: 2;
        }
        @keyframes ztePulse {
          0%,100% { box-shadow: 0 0 0 0 var(--status-halo); }
          50%     { box-shadow: 0 0 0 6px transparent; }
        }
        .zte-status-dot.live { animation: ztePulse 2s ease-in-out infinite; }
        @media (prefers-reduced-motion: reduce) {
          .zte-avatar::before { animation: none; }
        }

        .zte-id-text { flex: 1; min-width: 0; }
        .zte-id-name {
          font-size: 13.5px;
          font-weight: 600;
          color: var(--text);
          line-height: 1;
          letter-spacing: 0;
          display: inline-flex;
          align-items: center;
          gap: 4px;
        }
        @keyframes zteTimerGlow {
          0%,100% { text-shadow: 0 0 0 transparent; }
          50%     { text-shadow: 0 0 16px var(--status-halo); }
        }
        .zte-id-timer.is-live {
          animation: zteTimerGlow 3s ease-in-out infinite;
        }
        .zte-id-timer {
          margin-top: 4px;
          font-size: 24px;
          font-weight: 700;
          color: var(--text);
          font-variant-numeric: tabular-nums;
          letter-spacing: 1px;
          line-height: 1.1;
        }

        /* Primary action buttons — full pills, no icons */
        @keyframes zteSheen {
          0%   { transform: translateX(-140%) skewX(-22deg); opacity: 0; }
          40%  { opacity: 1; }
          100% { transform: translateX(240%)  skewX(-22deg); opacity: 0; }
        }
        @keyframes zteBreath {
          0%,100% { box-shadow: 0 8px 22px -6px rgba(17,73,106,0.45),
                                  0 0 0 0 rgba(68,185,123,0.0); }
          50%     { box-shadow: 0 12px 28px -6px rgba(17,73,106,0.55),
                                  0 0 0 10px rgba(68,185,123,0.0); }
        }
        @keyframes ztePopIn {
          0%   { transform: scale(.85); opacity: 0; }
          70%  { transform: scale(1.04); opacity: 1; }
          100% { transform: scale(1); }
        }
        @keyframes zteRippleOut {
          to { transform: scale(2.6); opacity: 0; }
        }

        .zte-btn {
          position: relative;
          isolation: isolate;
          overflow: hidden;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          padding: 11px 22px;
          border-radius: 999px;
          border: none;
          font-family: inherit;
          font-weight: 600;
          font-size: 13px;
          cursor: pointer;
          user-select: none;
          white-space: nowrap;
          /* Tightened from .25s → .14s. 140ms is the perceptual sweet spot
             for click-affordance feedback — long enough to feel smooth,
             short enough that the user reads the button as "instant". */
          transition:
            transform .14s cubic-bezier(.34,1.56,.64,1),
            box-shadow .14s ease,
            filter .14s ease;
          box-shadow: var(--shadow-sm);
          line-height: 1;
          /* NB: no ztePopIn entry animation here. Earlier versions played a
             350ms pop on every mount, which fired every time the action
             buttons toggled between [Clock In] and [Take Break, Clock Out].
             That made every state change feel like the new buttons were
             fading in 350ms after the click — the #1 source of the
             "buttons feel slow" feedback. The optimistic state update
             already swaps the buttons instantly; we do not need an entry
             animation layered on top. */
        }
        /* Diagonal sheen sweeps across on hover */
        .zte-btn::before {
          content: "";
          position: absolute;
          inset: 0;
          background: linear-gradient(120deg,
            transparent 0%,
            rgba(255,255,255,0.32) 50%,
            transparent 100%);
          transform: translateX(-140%) skewX(-22deg);
          opacity: 0;
          pointer-events: none;
          z-index: 0;
        }
        .zte-btn > * { position: relative; z-index: 1; }
        .zte-btn:hover:not(:disabled) {
          transform: translateY(-2px) scale(1.03);
          box-shadow: var(--shadow-md);
          filter: brightness(1.07) saturate(1.05);
        }
        .zte-btn:hover:not(:disabled)::before {
          animation: zteSheen .75s ease forwards;
        }
        .zte-btn:active:not(:disabled) {
          /* Slight extra squash + faster press-down so the click visibly
             "registers" before any state change. .05s = effectively
             instant feedback at 60fps. */
          transform: translateY(0) scale(.95);
          transition-duration: .05s;
        }
        .zte-btn:disabled { cursor: progress; opacity: 0.7; animation: none; }
        /* Phase-1 F3: in-flight spinner inside an attendance button */
        .zte-busy-label { display: inline-flex; align-items: center; gap: 7px; }
        .zte-inline-spin { animation: zteRingSpin .8s linear infinite; }
        @media (prefers-reduced-motion: reduce) { .zte-inline-spin { animation: none; } }

        /* Click ripple — appended at the cursor position by JS */
        .zte-ripple {
          position: absolute;
          border-radius: 50%;
          width: 14px; height: 14px;
          background: rgba(255,255,255,0.55);
          pointer-events: none;
          transform: scale(0);
          opacity: 1;
          animation: zteRippleOut .6s ease-out forwards;
          z-index: 0;
        }

        .zte-btn-break {
          background: linear-gradient(135deg, var(--break), var(--break-2));
          color: #fff;
        }
        .zte-btn-break:hover:not(:disabled) {
          box-shadow: 0 14px 26px -8px rgba(245,158,11,0.55);
        }
        .zte-btn-resume {
          background: linear-gradient(135deg, var(--status), var(--accent-2));
          color: #fff;
        }
        .zte-btn-resume:hover:not(:disabled) {
          box-shadow: 0 14px 26px -8px rgba(22,163,74,0.55);
        }
        .zte-btn-clockout {
          background: linear-gradient(135deg, var(--danger), var(--danger-2));
          color: #fff;
        }
        .zte-btn-clockout:hover:not(:disabled) {
          box-shadow: 0 14px 26px -8px rgba(239,68,68,0.6);
        }
        .zte-btn-clockin {
          background: linear-gradient(135deg, var(--brand), var(--accent));
          background-size: 180% 180%;
          background-position: 0% 50%;
          color: #fff;
          padding: 13px 38px;
          font-size: 14px;
          border-radius: 999px;
          box-shadow: var(--shadow-lg);
          /* Dropped the ztePopIn 400ms entry that re-fired every time the
             button re-mounted on clock-out. Kept the subtle breath so the
             primary CTA still feels alive at rest. */
          animation: zteBreath 2.8s ease-in-out 0.6s infinite;
          /* Background-position transition tightened from .6s → .3s so the
             hover sweep doesn't lag the click. Others match .zte-btn. */
          transition:
            transform .14s cubic-bezier(.34,1.56,.64,1),
            box-shadow .14s ease,
            background-position .3s ease,
            filter .14s ease;
        }
        .zte-btn-clockin:hover:not(:disabled) {
          background-position: 100% 50%;
          transform: translateY(-2px) scale(1.05);
          box-shadow: 0 18px 32px -8px rgba(17,73,106,0.6);
          animation-play-state: paused, paused;
        }
        @media (prefers-reduced-motion: reduce) {
          .zte-btn, .zte-btn-clockin { animation: none !important; }
          .zte-btn::before { display: none; }
        }

        /* ── Greeting block — solid name, inline status line ─────────── */
        .zte-greet {
          padding: 26px 22px 8px;
          text-align: center;
        }
        .zte-greet-line {
          font-size: 23px;
          font-weight: 500;
          color: var(--text);
          letter-spacing: -0.2px;
          line-height: 1.25;
        }
        .zte-greet-line strong {
          font-weight: 700;
          color: var(--text);
        }

        /* Status — plain inline text, colored verb as animated chip */
        @keyframes zteChipPulse {
          0%,100% { box-shadow: 0 0 0 0 var(--status-halo); }
          70%     { box-shadow: 0 0 0 8px transparent; }
        }
        @keyframes zteChipPulseBreak {
          0%,100% { box-shadow: 0 0 0 0 var(--break-halo); }
          70%     { box-shadow: 0 0 0 8px transparent; }
        }
        .zte-status-line {
          margin-top: 8px;
          font-size: 15.5px;
          color: var(--text-muted);
          display: inline-flex;
          align-items: center;
          gap: 6px;
          flex-wrap: wrap;
          justify-content: center;
        }
        .zte-status-line strong {
          color: var(--status);
          font-weight: 700;
          display: inline-flex;
          align-items: center;
          gap: 6px;
          padding: 4px 12px;
          border-radius: 999px;
          background: linear-gradient(135deg, var(--chip-bg), var(--chip-bg-2));
          font-size: 13.5px;
          letter-spacing: 0.2px;
          animation: zteChipPulse 2.2s ease-in-out infinite;
        }
        .zte-status-line strong::before {
          content: "";
          width: 7px; height: 7px;
          border-radius: 50%;
          background: var(--status);
          box-shadow: 0 0 8px var(--status-halo);
        }
        .zte-status-line.idle strong {
          color: var(--break);
          background: linear-gradient(135deg, var(--break-chip-bg), var(--break-chip-bg-2));
          animation: zteChipPulseBreak 2.2s ease-in-out infinite;
        }
        .zte-status-line.idle strong::before { background: var(--break); box-shadow: 0 0 8px var(--break-halo); }
        .zte-status-line.offline strong {
          color: var(--text-muted);
          background: rgba(148,163,184,0.14);
          animation: none;
        }
        .zte-status-line.offline strong::before { background: var(--text-muted); box-shadow: none; }

        /* Bug 6: live break timer chip — uses the amber "break" theme so it
           reads as paused/idle, distinct from the green live work timer. */
        .zte-break-timer {
          margin-top: 12px;
          display: inline-flex;
          align-items: center;
          gap: 8px;
          padding: 8px 16px;
          border-radius: 999px;
          color: var(--break);
          background: linear-gradient(135deg, var(--break-chip-bg), var(--break-chip-bg-2));
          animation: zteChipPulseBreak 2.2s ease-in-out infinite;
        }
        .zte-break-timer-label {
          font-size: 13px;
          font-weight: 600;
          letter-spacing: 0.2px;
        }
        .zte-break-timer-clock {
          font-size: 18px;
          font-weight: 800;
          font-variant-numeric: tabular-nums;
          letter-spacing: 1px;
        }
        @media (prefers-reduced-motion: reduce) {
          .zte-status-line strong { animation: none !important; }
          .zte-id-timer.is-live { animation: none !important; }
          .zte-break-timer { animation: none !important; }
        }

        /* ── Section divider — gradient hairline ─────────────────────── */
        .zte-divider {
          height: 1px;
          margin: 4px 22px 0;
          background: var(--divider-grad);
        }

        /* Animated wave/sparkle next to greeting */
        @keyframes zteWave {
          0%,60%,100% { transform: rotate(0); }
          10%,30%     { transform: rotate(18deg); }
          20%         { transform: rotate(-12deg); }
          40%         { transform: rotate(10deg); }
          50%         { transform: rotate(-6deg); }
        }
        .zte-wave {
          display: inline-block;
          transform-origin: 70% 70%;
          animation: zteWave 3.4s ease-in-out infinite;
          margin-left: 6px;
        }
        @media (prefers-reduced-motion: reduce) {
          .zte-wave { animation: none; }
        }

        /* ── Secondary widget cards ─────────────────────────────────── */
        .zte-secondary {
          display: grid;
          grid-template-columns: 1fr 1fr;
          gap: 14px;
          padding: 18px 22px 18px;
        }
        .zte-sec-card {
          position: relative;
          isolation: isolate;
          display: flex;
          align-items: center;
          gap: 12px;
          padding: 16px 18px;
          background: var(--surface);
          color: var(--text);
          border: 1.5px solid var(--border);
          border-radius: 14px;
          font-family: inherit;
          font-size: 14px;
          font-weight: 600;
          cursor: pointer;
          text-align: left;
          overflow: hidden;
          transition:
            transform .3s cubic-bezier(.34,1.56,.64,1),
            border-color .25s ease,
            box-shadow .25s ease,
            background .25s ease;
        }
        /* Subtle gradient wash that fades in on hover */
        .zte-sec-card::before {
          content: "";
          position: absolute;
          inset: 0;
          background: linear-gradient(135deg, var(--brand-soft) 0%, transparent 60%);
          opacity: 0;
          transition: opacity .25s ease;
          pointer-events: none;
          z-index: 0;
        }
        /* Diagonal sheen sweep on hover */
        .zte-sec-card::after {
          content: "";
          position: absolute;
          top: 0; bottom: 0;
          left: -40%;
          width: 30%;
          background: linear-gradient(120deg,
            transparent 0%,
            rgba(17,73,106,0.10) 50%,
            transparent 100%);
          transform: skewX(-22deg);
          opacity: 0;
          pointer-events: none;
          z-index: 0;
        }
        .zte-sec-card > * { position: relative; z-index: 1; }
        .zte-sec-card.primary {
          border-color: var(--brand);
        }
        .zte-sec-card:hover {
          transform: translateY(-3px) scale(1.015);
          border-color: var(--brand);
          box-shadow: var(--shadow-lg);
        }
        .zte-sec-card:hover::before { opacity: 1; }
        .zte-sec-card:hover::after {
          animation: zteSheen .9s ease forwards;
        }
        .zte-sec-card:active {
          transform: translateY(-1px) scale(.99);
          transition-duration: .08s;
        }
        .zte-sec-card-icon {
          color: var(--brand);
          display: inline-flex;
          align-items: center;
          justify-content: center;
          flex-shrink: 0;
          transition: transform .35s cubic-bezier(.34,1.56,.64,1);
        }
        .zte-sec-card:hover .zte-sec-card-icon {
          transform: rotate(-8deg) scale(1.18);
        }
        .zte-sec-card-row {
          display: flex;
          align-items: center;
          justify-content: space-between;
          flex: 1;
          min-width: 0;
          gap: 8px;
        }
        .zte-sec-card-arrow {
          color: var(--text-muted);
          transition: transform .3s cubic-bezier(.34,1.56,.64,1), color .25s ease;
          flex-shrink: 0;
        }
        .zte-sec-card:hover .zte-sec-card-arrow {
          color: var(--brand);
          transform: translate(4px, -4px) scale(1.1);
        }

        /* ── Help bubble — floating pill with gradient + glow ─────────
           Width is locked down explicitly (width:auto + max-content + left:auto)
           so any global 'button { width: 100% }' in the host CSS can't
           stretch this into a full-width bar. */
        @keyframes zteHelpFloat {
          0%,100% { transform: translateY(0); }
          50%     { transform: translateY(-3px); }
        }
        @keyframes zteHelpHalo {
          0%,100% { box-shadow: 0 8px 22px -6px rgba(17,73,106,0.45),
                                  0 0 0 0 rgba(17,73,106,0.25); }
          70%     { box-shadow: 0 8px 22px -6px rgba(17,73,106,0.45),
                                  0 0 0 10px rgba(17,73,106,0); }
        }
        .zte-help {
          position: absolute !important;
          bottom: 44px;
          right: 18px;
          left: auto;
          top: auto;
          width: auto !important;
          max-width: max-content;
          min-width: 0;
          padding: 8px 16px;
          border-radius: 999px;
          background: var(--help-grad);
          background-size: 180% 180%;
          background-position: 0% 50%;
          color: var(--help-text);
          border: none;
          font-family: inherit;
          font-weight: 700;
          font-size: 12px;
          line-height: 1;
          cursor: pointer;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          gap: 6px;
          box-shadow: var(--shadow-lg);
          overflow: hidden;
          isolation: isolate;
          transition:
            transform .25s cubic-bezier(.34,1.56,.64,1),
            box-shadow .25s ease,
            background-position .6s ease,
            filter .25s ease;
          z-index: 5;
          white-space: nowrap;
          animation:
            zteHelpFloat 3.2s ease-in-out infinite,
            zteHelpHalo 2.6s ease-out infinite;
        }
        .zte-help::before {
          content: "";
          position: absolute;
          inset: 0;
          background: linear-gradient(120deg,
            transparent 0%,
            rgba(255,255,255,0.35) 50%,
            transparent 100%);
          transform: translateX(-140%) skewX(-22deg);
          opacity: 0;
          pointer-events: none;
          z-index: 0;
        }
        .zte-help > * { position: relative; z-index: 1; }
        .zte-help svg {
          transition: transform .4s cubic-bezier(.34,1.56,.64,1);
        }
        .zte-help:hover {
          transform: translateY(-3px) scale(1.07);
          background-position: 100% 50%;
          box-shadow: 0 18px 32px -8px rgba(17,73,106,0.6);
          animation-play-state: paused, paused;
          filter: brightness(1.08);
        }
        .zte-help:hover svg { transform: rotate(18deg) scale(1.15); }
        .zte-help:hover::before { animation: zteSheen .85s ease forwards; }
        .zte-help:active {
          transform: translateY(-1px) scale(1.02);
          transition-duration: .08s;
        }
        @media (prefers-reduced-motion: reduce) {
          .zte-help { animation: none; }
          .zte-help::before { display: none; }
        }

        /* ── Brand caption strip — fixed at the very bottom of the popup ─ */
        @keyframes zteDotBlink {
          0%,100% { opacity: 0.35; transform: scale(1); }
          50%     { opacity: 1;    transform: scale(1.3); }
        }
        .zte-brand-strip {
          position: absolute;
          left: 0;
          right: 0;
          bottom: 0;
          padding: 10px 22px 12px;
          text-align: center;
          font-family: inherit;
          z-index: 4;
          pointer-events: none;
          display: flex;
          align-items: center;
          justify-content: center;
          gap: 8px;
        }
        .zte-brand-strip::before,
        .zte-brand-strip::after {
          content: "";
          flex: 0 0 36px;
          height: 1px;
          background: var(--divider-grad);
        }
        .zte-brand-strip-name {
          font-size: 11.5px;
          font-weight: 800;
          letter-spacing: 0.7px;
          background: linear-gradient(90deg, var(--brand) 0%, var(--accent) 100%);
          -webkit-background-clip: text;
                  background-clip: text;
          -webkit-text-fill-color: transparent;
                  color: transparent;
        }
        .zte-brand-strip-dot {
          display: inline-block;
          width: 4px; height: 4px;
          border-radius: 50%;
          background: var(--accent);
          animation: zteDotBlink 2.6s ease-in-out infinite;
        }
        .zte-brand-strip-tag {
          font-size: 10.5px;
          font-weight: 600;
          color: var(--text-muted);
          letter-spacing: 0.3px;
        }
        @media (prefers-reduced-motion: reduce) {
          .zte-brand-strip-dot { animation: none; }
        }

        /* ── Help modal (Need Help? form) ────────────────────────────── */
        @keyframes zteFadeIn { from { opacity: 0; } to { opacity: 1; } }
        @keyframes zteModalIn {
          from { opacity: 0; transform: scale(.94) translateY(8px); }
          to   { opacity: 1; transform: scale(1)   translateY(0); }
        }
        .zte-modal-backdrop {
          position: fixed;
          inset: 0;
          z-index: 9999;
          background: rgba(8, 14, 28, 0.55);
          backdrop-filter: blur(4px);
          -webkit-backdrop-filter: blur(4px);
          display: flex;
          align-items: center;
          justify-content: center;
          padding: 18px;
          animation: zteFadeIn .18s ease-out;
        }
        .zte-modal {
          width: 100%;
          max-width: 460px;
          max-height: calc(100vh - 36px);
          overflow-y: auto;
          background: var(--surface);
          color: var(--text);
          border: 1px solid var(--border);
          border-radius: 18px;
          box-shadow: 0 26px 60px -12px rgba(8, 14, 28, 0.55);
          padding: 22px 22px 18px;
          font-family: inherit;
          position: relative;
          animation: zteModalIn .22s cubic-bezier(.34,1.56,.64,1) both;
        }
        .zte-modal-head {
          display: flex; align-items: flex-start; gap: 12px;
          margin-bottom: 14px;
        }
        .zte-modal-icon {
          flex-shrink: 0;
          width: 38px; height: 38px;
          border-radius: 12px;
          background: linear-gradient(135deg, var(--brand-soft), transparent);
          border: 1px solid var(--border);
          color: var(--brand);
          display: inline-flex; align-items: center; justify-content: center;
        }
        .zte-modal-title {
          font-size: 16px;
          font-weight: 800;
          color: var(--text);
          margin: 0 0 2px;
          letter-spacing: -0.1px;
        }
        .zte-modal-sub {
          font-size: 12px;
          color: var(--text-muted);
          line-height: 1.5;
          margin: 0;
        }
        .zte-modal-close {
          position: absolute;
          top: 14px; right: 14px;
          width: 28px; height: 28px;
          border-radius: 8px;
          border: 1px solid var(--border);
          background: var(--surface);
          color: var(--text-muted);
          cursor: pointer;
          display: inline-flex; align-items: center; justify-content: center;
          transition:
            transform .25s cubic-bezier(.34,1.56,.64,1),
            background .2s ease,
            color .2s ease,
            border-color .2s ease;
        }
        .zte-modal-close:hover {
          background: var(--brand-soft);
          color: var(--brand);
          border-color: var(--brand);
          transform: rotate(90deg) scale(1.05);
        }

        .zte-field { display: flex; flex-direction: column; gap: 6px; margin-top: 12px; }
        .zte-field-label {
          font-size: 11.5px;
          font-weight: 700;
          letter-spacing: 0.4px;
          text-transform: uppercase;
          color: var(--text-muted);
        }
        .zte-field-wrap {
          position: relative;
          display: flex;
          align-items: center;
        }
        .zte-field-icon {
          position: absolute;
          left: 12px;
          color: var(--text-muted);
          display: inline-flex;
          pointer-events: none;
          transition: color .2s ease;
        }
        .zte-input, .zte-textarea {
          width: 100%;
          background: var(--bg);
          color: var(--text);
          border: 1.5px solid var(--border);
          border-radius: 10px;
          padding: 10px 12px 10px 38px;
          font-family: inherit;
          font-size: 13.5px;
          line-height: 1.4;
          outline: none;
          transition: border-color .2s ease, box-shadow .2s ease, background .2s ease;
        }
        .zte-textarea {
          padding: 10px 12px;          /* no leading icon — textarea has its own affordance */
          resize: vertical;
          min-height: 92px;
          max-height: 200px;
        }
        .zte-input::placeholder,
        .zte-textarea::placeholder { color: var(--text-muted); opacity: .85; }
        .zte-input:focus, .zte-textarea:focus {
          border-color: var(--brand);
          background: var(--surface);
          box-shadow: 0 0 0 4px var(--brand-soft);
        }
        .zte-input:focus ~ .zte-field-icon,
        .zte-field-wrap:focus-within .zte-field-icon { color: var(--brand); }

        .zte-modal-err {
          margin-top: 12px;
          padding: 9px 12px;
          border-radius: 10px;
          background: rgba(239,68,68,0.10);
          border: 1px solid rgba(239,68,68,0.30);
          color: var(--danger);
          font-size: 12px;
          font-weight: 600;
          display: flex; align-items: center; gap: 8px;
          animation: zteFadeIn .18s ease-out;
        }

        .zte-modal-actions {
          display: flex;
          gap: 10px;
          justify-content: flex-end;
          margin-top: 18px;
        }
        .zte-modal-btn-cancel {
          padding: 10px 18px;
          border-radius: 999px;
          border: 1px solid var(--border);
          background: var(--surface);
          color: var(--text-sub);
          font-family: inherit;
          font-weight: 600;
          font-size: 13px;
          cursor: pointer;
          transition: all .2s ease;
        }
        .zte-modal-btn-cancel:hover:not(:disabled) {
          background: var(--soft);
          border-color: var(--text-muted);
          color: var(--text);
        }
        .zte-modal-btn-submit {
          position: relative;
          isolation: isolate;
          overflow: hidden;
          padding: 10px 22px;
          border-radius: 999px;
          border: none;
          background: linear-gradient(135deg, var(--brand), var(--accent));
          background-size: 180% 180%;
          background-position: 0% 50%;
          color: #fff;
          font-family: inherit;
          font-weight: 700;
          font-size: 13px;
          cursor: pointer;
          display: inline-flex; align-items: center; gap: 6px;
          box-shadow: var(--shadow-md);
          transition:
            transform .25s cubic-bezier(.34,1.56,.64,1),
            background-position .5s ease,
            box-shadow .25s ease,
            filter .25s ease;
        }
        .zte-modal-btn-submit:hover:not(:disabled) {
          transform: translateY(-1.5px) scale(1.03);
          background-position: 100% 50%;
          box-shadow: 0 14px 26px -8px rgba(17,73,106,0.55);
          filter: brightness(1.05);
        }
        .zte-modal-btn-submit:active:not(:disabled) { transform: translateY(0) scale(.98); }
        .zte-modal-btn-submit:disabled { opacity: .65; cursor: not-allowed; }
        .zte-modal-btn-submit .zte-spin {
          width: 14px; height: 14px; border-radius: 50%;
          border: 2px solid rgba(255,255,255,0.4);
          border-top-color: #fff;
          animation: zteRingSpin .8s linear infinite;
        }

        /* Success state — green check + message */
        .zte-modal-success {
          display: flex; flex-direction: column; align-items: center;
          gap: 10px; padding: 18px 8px 8px;
          text-align: center;
          animation: zteFadeIn .25s ease-out;
        }
        .zte-modal-success-icon {
          width: 54px; height: 54px;
          border-radius: 50%;
          background: linear-gradient(135deg, var(--status), var(--accent-2));
          color: #fff;
          display: inline-flex; align-items: center; justify-content: center;
          box-shadow: 0 10px 22px -6px var(--status-halo);
          animation: ztePopIn .35s cubic-bezier(.34,1.56,.64,1);
        }
        .zte-modal-success-title {
          font-size: 15px; font-weight: 800; color: var(--text); margin: 0;
        }
        .zte-modal-success-sub {
          font-size: 12.5px; color: var(--text-muted); margin: 0;
        }

        @media (prefers-reduced-motion: reduce) {
          .zte-modal-backdrop, .zte-modal, .zte-modal-err, .zte-modal-success { animation: none; }
        }

        /* ── Update notification card ────────────────────────────────── */
        .zte-update-card {
          margin: 10px 14px 0;
          padding: 13px 15px 12px;
          border-radius: 14px;
          background: linear-gradient(135deg, rgba(68,185,123,0.10), rgba(17,73,106,0.08));
          border: 1px solid rgba(68,185,123,0.36);
          color: var(--text);
          box-shadow: 0 8px 20px -12px rgba(17,73,106,0.35);
          animation: zteUpdateIn .35s cubic-bezier(.34,1.56,.64,1);
        }
        .zte-shell.dark .zte-update-card {
          background: linear-gradient(135deg, rgba(68,185,123,0.14), rgba(94,159,255,0.10));
          border-color: rgba(94,159,255,0.40);
        }
        @keyframes zteUpdateIn {
          0%   { opacity: 0; transform: translateY(-6px); }
          100% { opacity: 1; transform: translateY(0); }
        }
        .zte-update-head {
          display: flex; align-items: center; gap: 9px;
          margin-bottom: 7px;
        }
        .zte-update-emoji { font-size: 17px; line-height: 1; }
        .zte-update-iconwrap {
          height: 22px; width: 22px;
          border-radius: 7px;
          display: inline-flex; align-items: center; justify-content: center;
          background: var(--accent);
          color: #fff;
          box-shadow: 0 4px 10px -4px rgba(56,160,107,0.6);
        }
        .zte-update-title {
          font-weight: 700;
          font-size: 13.5px;
          letter-spacing: 0.1px;
        }
        .zte-update-body { font-size: 12.5px; line-height: 1.45; }
        .zte-update-version { color: var(--text-sub); margin-bottom: 3px; }
        .zte-update-version strong { font-weight: 700; color: var(--text); }
        .zte-update-notes {
          margin: 3px 0 5px 18px;
          padding: 0;
          color: var(--text);
        }
        .zte-update-notes li { margin: 1px 0; }
        .zte-update-steps {
          margin: 3px 0 5px 22px;
          padding: 0;
          color: var(--text);
        }
        .zte-update-steps li { margin: 1px 0; }
        .zte-update-foot {
          color: var(--text-muted);
          font-size: 11.5px;
          margin-top: 4px;
        }
        .zte-update-actions {
          margin-top: 10px;
          display: flex; gap: 8px; justify-content: flex-end;
        }
        .zte-update-btn-ghost,
        .zte-update-btn-primary {
          padding: 7px 14px;
          border-radius: 8px;
          font-weight: 600;
          font-size: 12px;
          cursor: pointer;
          position: relative; overflow: hidden;
          transition: transform .2s ease, filter .2s ease, background .2s ease;
        }
        .zte-update-btn-ghost {
          border: 1px solid var(--border);
          background: transparent;
          color: var(--text);
        }
        .zte-update-btn-ghost:hover:not(:disabled) { background: var(--soft); }
        .zte-update-btn-ghost:disabled { opacity: 0.45; cursor: not-allowed; }
        .zte-update-btn-primary {
          border: 0;
          background: linear-gradient(135deg, var(--accent), var(--accent-2));
          color: #fff;
          font-weight: 700;
          box-shadow: 0 5px 12px -4px rgba(56,160,107,0.5);
        }
        .zte-update-btn-primary:hover:not(:disabled) { transform: translateY(-1px); filter: brightness(1.05); }
        .zte-update-btn-primary:disabled { opacity: 0.6; cursor: progress; }

        /* Progress bar — used by the "Downloading update…" state. Reuses the
           accent gradient so it visually anchors to the primary button. */
        .zte-update-percent {
          display: inline-block;
          font-weight: 700;
          color: var(--text);
          font-variant-numeric: tabular-nums;
          font-size: 13px;
        }
        .zte-update-progress {
          margin-top: 6px;
          height: 6px;
          background: rgba(15, 23, 42, 0.10);
          border-radius: 999px;
          overflow: hidden;
        }
        .zte-shell.dark .zte-update-progress {
          background: rgba(255, 255, 255, 0.10);
        }
        .zte-update-progress-bar {
          height: 100%;
          background: linear-gradient(90deg, var(--accent), var(--accent-2));
          border-radius: 999px;
          transition: width 200ms ease;
          box-shadow: 0 0 8px var(--status-halo);
        }
        @media (prefers-reduced-motion: reduce) {
          .zte-update-card { animation: none; }
          .zte-update-progress-bar { transition: none; }
        }
      `,
        }}
      />

      {/* Ambient background blobs + subtle dot-grid texture */}
      <div className="zte-ambient" aria-hidden="true" />
      <div className="zte-grid" aria-hidden="true" />

      {/* ── Desktop update notification card ─────────────────────────────
          Four-state native-updater card:
            1. idle        → release notes + [Update Now] [Later]
            2. downloading → percentage + progress bar (button disabled)
            3. downloaded  → "Update ready" + [Restart and Update] [Later]
            4. installing  → final paint before NSIS swaps the binary
          Plus a transient "error" state with a Retry CTA.

          The download / install actions go through window.electronUpdater
          (exposed by preload.js → electron-updater in main.js), so bytes
          stream inside the app — no browser download, no Chrome warning,
          no manual installer launch.

          Legacy compat: older shells (v1.0.7) that don't expose
          window.electronUpdater fall back to window.open(download_url),
          which the legacy setWindowOpenHandler routes via shell.openExternal. */}
      {showUpdateNotice && latestDesktop && (
        <div
          className="zte-update-card"
          role="region"
          aria-label="Update available"
        >
          {updatePhase === "idle" && (
            <>
              <div className="zte-update-head">
                <span className="zte-update-emoji" aria-hidden="true">
                  🚀
                </span>
                <div className="zte-update-title">
                  Temveris Desktop Update Available
                </div>
              </div>
              <div className="zte-update-body">
                <div className="zte-update-version">
                  Version <strong>{latestDesktop.latest_version}</strong>{" "}
                  includes:
                </div>
                {(latestDesktop.release_notes || []).length > 0 && (
                  <ul className="zte-update-notes">
                    {latestDesktop.release_notes.map((note, i) => (
                      <li key={i}>{note}</li>
                    ))}
                  </ul>
                )}
                <div className="zte-update-foot">
                  Your attendance and work history will not be affected.
                </div>
              </div>
              <div className="zte-update-actions">
                <button
                  className="zte-update-btn-ghost"
                  onClick={(e) => {
                    spawnRipple(e);
                    onLater();
                  }}
                  disabled={
                    isForceRequired /* reserved for a future hard-floor */
                  }
                  title={
                    isForceRequired ? "Update required" : "Remind me in 3 days"
                  }
                >
                  Later
                </button>
                <button
                  className="zte-update-btn-primary"
                  onClick={(e) => {
                    spawnRipple(e);
                    onUpdateNow();
                  }}
                >
                  Update Now
                </button>
              </div>
            </>
          )}

          {updatePhase === "downloading" && (
            <>
              <div className="zte-update-head">
                <span className="zte-update-iconwrap" aria-hidden="true">
                  <Download size={16} strokeWidth={2.4} />
                </span>
                <div className="zte-update-title">Downloading update…</div>
              </div>
              <div className="zte-update-body">
                <div className="zte-update-version">
                  <span className="zte-update-percent">
                    {Math.round(updateProgress)}%
                  </span>
                  <span style={{ marginLeft: 6, color: "var(--text-muted)" }}>
                    of v{latestDesktop.latest_version}
                  </span>
                </div>
                <div
                  className="zte-update-progress"
                  role="progressbar"
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={Math.round(updateProgress)}
                >
                  <div
                    className="zte-update-progress-bar"
                    style={{
                      width: `${Math.max(2, Math.min(100, updateProgress))}%`,
                    }}
                  />
                </div>
                <div className="zte-update-foot">
                  Keep Temveris open — the download is streaming inside the
                  app.
                </div>
              </div>
              <div className="zte-update-actions">
                <button className="zte-update-btn-primary" disabled>
                  Downloading…
                </button>
              </div>
            </>
          )}

          {updatePhase === "downloaded" && (
            <>
              <div className="zte-update-head">
                <span className="zte-update-iconwrap" aria-hidden="true">
                  <Download size={16} strokeWidth={2.4} />
                </span>
                <div className="zte-update-title">Update ready to install</div>
              </div>
              <div className="zte-update-body">
                <div className="zte-update-version">
                  Version{" "}
                  <strong>
                    {downloadedVersion || latestDesktop.latest_version}
                  </strong>{" "}
                  is staged.
                </div>
                <div className="zte-update-foot">
                  Temveris will close, install the update, and reopen
                  automatically.
                </div>
              </div>
              <div className="zte-update-actions">
                <button
                  className="zte-update-btn-ghost"
                  onClick={(e) => {
                    spawnRipple(e);
                    onLater();
                  }}
                  title="Install on next launch"
                >
                  Later
                </button>
                <button
                  className="zte-update-btn-primary"
                  onClick={(e) => {
                    spawnRipple(e);
                    onRestartAndUpdate();
                  }}
                >
                  Restart and Update
                </button>
              </div>
            </>
          )}

          {updatePhase === "installing" && (
            <>
              <div className="zte-update-head">
                <span className="zte-update-iconwrap" aria-hidden="true">
                  <Download size={16} strokeWidth={2.4} />
                </span>
                <div className="zte-update-title">Installing update…</div>
              </div>
              <div className="zte-update-body">
                <div className="zte-update-foot">
                  Temveris will restart automatically.
                </div>
              </div>
            </>
          )}

          {updatePhase === "error" && (
            <>
              <div className="zte-update-head">
                <span className="zte-update-emoji" aria-hidden="true">
                  ⚠️
                </span>
                <div className="zte-update-title">Update failed</div>
              </div>
              <div className="zte-update-body">
                <div className="zte-update-foot">
                  {updateError || "Something went wrong while updating."}
                </div>
              </div>
              <div className="zte-update-actions">
                <button
                  className="zte-update-btn-ghost"
                  onClick={(e) => {
                    spawnRipple(e);
                    setUpdatePhase("idle");
                    setUpdateError(null);
                  }}
                >
                  Dismiss
                </button>
                <button
                  className="zte-update-btn-primary"
                  onClick={(e) => {
                    spawnRipple(e);
                    onUpdateNow();
                  }}
                >
                  Try Again
                </button>
              </div>
            </>
          )}
        </div>
      )}

      {/* ── Top strip — logo left · theme + logout right ─────────────── */}
      <div className="zte-topbar">
        <img
          src={workspaceLogo || (isDark ? logoWhite : logo)}
          alt="Workspace"
          className="zte-logo"
          style={{ height: 26 }}
        />
        <div className="zte-top-actions">
          <button
            className="zte-icon-btn"
            onClick={toggleDark}
            title={isDark ? "Switch to light" : "Switch to dark"}
            aria-label="Toggle theme"
          >
            {isDark ? (
              <Sun size={15} strokeWidth={2.2} />
            ) : (
              <Moon size={15} strokeWidth={2.2} />
            )}
          </button>
          <button
            className="zte-icon-btn"
            onClick={handleLogout}
            title="Sign out"
            aria-label="Sign out"
          >
            <LogOut size={15} strokeWidth={2.2} />
          </button>
        </div>
      </div>

      {/* ── Tracker row — avatar · name + timer · action pills ───────── */}
      <div className="zte-tracker">
        <div
          className={`zte-avatar ${clockedIn && !onBreak ? "is-live" : ""} ${onBreak ? "is-break" : ""}`}
        >
          {initial}
          <span
            className={`zte-status-dot ${clockedIn && !onBreak ? "live" : ""}`}
            style={{ background: statusColor }}
          />
        </div>
        <div className="zte-id-text">
          <div className="zte-id-name">{name || firstName}</div>
          <div
            className={`zte-id-timer ${clockedIn && !onBreak ? "is-live" : ""}`}
          >
            <LiveWorkedClock
              clockedIn={clockedIn}
              onBreak={onBreak}
              clockInMsRef={clockInMsRef}
              totalBreakMsRef={totalBreakMsRef}
              breakStartMsRef={breakStartMsRef}
              prevWorkedSecsRef={prevWorkedSecsRef}
            />
          </div>
        </div>
        {clockedIn ? (
          <div style={{ display: "flex", gap: 10, flexShrink: 0 }}>
            <button
              className={`zte-btn ${onBreak ? "zte-btn-resume" : "zte-btn-break"}`}
              // F3: lock + spinner while a break request is in flight.
              // F4: also lock while the break-type picker is open so the click
              // visibly "took" (the modal IS the feedback) and can't be re-fired.
              disabled={breakBusy || (!onBreak && showBreakPicker)}
              onClick={(e) => {
                spawnRipple(e);
                if (onBreak) handleBreakToggle();
                else setShowBreakPicker(true);
              }}
            >
              {breakBusy ? (
                <span className="zte-busy-label">
                  <Loader2 size={15} className="zte-inline-spin" /> Updating…
                </span>
              ) : onBreak ? (
                "Resume Work"
              ) : (
                "Take a Break"
              )}
            </button>
            <button
              className="zte-btn zte-btn-clockout"
              disabled={clockBusy}
              onClick={(e) => {
                spawnRipple(e);
                handleClockOut();
              }}
            >
              {clockBusy ? (
                <span className="zte-busy-label">
                  <Loader2 size={15} className="zte-inline-spin" /> Clocking
                  out…
                </span>
              ) : (
                "Clock Out"
              )}
            </button>
          </div>
        ) : null}
      </div>

      {/* Gradient hairline divider between tracker and greeting */}
      <div className="zte-divider" aria-hidden="true" />

      {/* ── Greeting + inline status line ─────────────────────────────── */}
      <div className="zte-greet">
        <div className="zte-greet-line">
          {greeting()}, <strong>{name || firstName}</strong>
          <span className="zte-wave" role="img" aria-label="wave">
            👋
          </span>
        </div>
        <div
          className={`zte-status-line ${onBreak ? "idle" : clockedIn ? "" : "offline"}`}
        >
          You are currently <strong>{statusText}</strong>
        </div>
        {/* Bug 6: live break timer so the worker can see how long they've been
            on their break (the work timer above is paused during a break). */}
        {onBreak && (
          <div className="zte-break-timer" role="status" aria-live="polite">
            <Coffee size={15} strokeWidth={2.2} />
            <span className="zte-break-timer-label">On break for</span>
            <span className="zte-break-timer-clock">
              <LiveWorkedClock
                mode="break"
                clockedIn={clockedIn}
                onBreak={onBreak}
                clockInMsRef={clockInMsRef}
                totalBreakMsRef={totalBreakMsRef}
                breakStartMsRef={breakStartMsRef}
                prevWorkedSecsRef={prevWorkedSecsRef}
              />
            </span>
          </div>
        )}
        {!clockedIn && (
          <div style={{ marginTop: 18 }}>
            <button
              className="zte-btn zte-btn-clockin"
              // Still NOT gated by the initial `loading` (the first /employees/me
              // can take 1-2s on cold start and a dead Clock In button during
              // that window was the top "feels slow" complaint). We gate ONLY on
              // clockBusy — the in-flight flag set the instant the user clicks —
              // so the button shows a spinner and can't be double-fired, while
              // remaining clickable during the pre-click /me load. Phase-1 F3.
              disabled={clockBusy}
              onClick={(e) => {
                spawnRipple(e);
                handleClockIn();
              }}
            >
              {clockBusy ? (
                <span className="zte-busy-label">
                  <Loader2 size={15} className="zte-inline-spin" /> Clocking in…
                </span>
              ) : (
                "Clock In"
              )}
            </button>
          </div>
        )}
      </div>

      {/* ── Secondary widget cards — primary highlight on report ─────── */}
      <div className="zte-secondary">
        <button
          className="zte-sec-card primary"
          onClick={(e) => {
            spawnRipple(e);
            openInBrowser("/dashboard?tab=my-work");
          }}
          title="Opens in browser"
        >
          <div className="zte-sec-card-icon">
            <FileText size={18} strokeWidth={2} />
          </div>
          <div className="zte-sec-card-row">
            <span>View your work report</span>
            <ArrowUpRight
              size={16}
              className="zte-sec-card-arrow"
              strokeWidth={2.2}
            />
          </div>
        </button>
        <button
          className="zte-sec-card"
          onClick={(e) => {
            spawnRipple(e);
            openInBrowser("/dashboard?tab=leave");
          }}
          title="Opens in browser"
        >
          <div className="zte-sec-card-icon">
            <CalendarDays size={18} strokeWidth={2} />
          </div>
          <div className="zte-sec-card-row">
            <span>Apply leave</span>
            <ArrowUpRight
              size={16}
              className="zte-sec-card-arrow"
              strokeWidth={2.2}
            />
          </div>
        </button>
      </div>

      {/* ── Floating help bubble — bottom-right ───────────────────────── */}
      <button
        className="zte-help"
        onClick={(e) => {
          spawnRipple(e);
          openHelp();
        }}
      >
        <HelpCircle size={14} strokeWidth={2.2} />
        Need Help?
      </button>

      {/* ── "Need Help?" support form modal ──────────────────────────── */}
      {helpOpen && (
        <div
          className="zte-modal-backdrop"
          onClick={closeHelp}
          role="dialog"
          aria-modal="true"
          aria-labelledby="zte-help-title"
        >
          <div className="zte-modal" onClick={(e) => e.stopPropagation()}>
            <button
              type="button"
              className="zte-modal-close"
              onClick={closeHelp}
              disabled={helpBusy}
              aria-label="Close"
              title="Close"
            >
              <X size={14} strokeWidth={2.2} />
            </button>

            {helpDone ? (
              <div className="zte-modal-success">
                <div className="zte-modal-success-icon">
                  <CheckCircle2 size={26} strokeWidth={2.4} />
                </div>
                <p className="zte-modal-success-title">Sent to support</p>
                <p className="zte-modal-success-sub">
                  We'll reply at <strong>{helpEmail}</strong> shortly.
                </p>
              </div>
            ) : (
              <>
                <div className="zte-modal-head">
                  <div className="zte-modal-icon">
                    <HelpCircle size={18} strokeWidth={2.2} />
                  </div>
                  <div>
                    <p id="zte-help-title" className="zte-modal-title">
                      Tell us what's wrong
                    </p>
                    <p className="zte-modal-sub">
                      Your message goes straight to{" "}
                      <strong>support@temveris.example</strong>. They'll reply to
                      your work email.
                    </p>
                  </div>
                </div>

                <div className="zte-field">
                  <label className="zte-field-label" htmlFor="zte-help-name">
                    Your name
                  </label>
                  <div className="zte-field-wrap">
                    <span className="zte-field-icon">
                      <User size={14} strokeWidth={2.2} />
                    </span>
                    <input
                      id="zte-help-name"
                      className="zte-input"
                      type="text"
                      placeholder="e.g. Harish Reddy"
                      value={helpName}
                      onChange={(e) => setHelpName(e.target.value)}
                      maxLength={200}
                      disabled={helpBusy}
                      autoFocus
                    />
                  </div>
                </div>

                <div className="zte-field">
                  <label className="zte-field-label" htmlFor="zte-help-email">
                    Work email
                  </label>
                  <div className="zte-field-wrap">
                    <span className="zte-field-icon">
                      <Mail size={14} strokeWidth={2.2} />
                    </span>
                    <input
                      id="zte-help-email"
                      className="zte-input"
                      type="email"
                      placeholder="you@yourcompany.com"
                      value={helpEmail}
                      onChange={(e) => setHelpEmail(e.target.value)}
                      maxLength={320}
                      disabled={helpBusy}
                    />
                  </div>
                </div>

                <div className="zte-field">
                  <label className="zte-field-label" htmlFor="zte-help-issue">
                    What's the issue?{" "}
                    <span
                      style={{
                        opacity: 0.7,
                        textTransform: "none",
                        letterSpacing: 0,
                      }}
                    >
                      ({Math.min(helpIssue.trim().length, 8000)}/8000)
                    </span>
                  </label>
                  <textarea
                    id="zte-help-issue"
                    className="zte-textarea"
                    placeholder="Describe what happened, what you expected, and any error you saw…"
                    value={helpIssue}
                    onChange={(e) => setHelpIssue(e.target.value)}
                    maxLength={8000}
                    rows={4}
                    disabled={helpBusy}
                  />
                </div>

                {helpError && (
                  <div className="zte-modal-err">
                    <AlertCircle size={14} strokeWidth={2.4} />
                    {helpError}
                  </div>
                )}

                <div className="zte-modal-actions">
                  <button
                    type="button"
                    className="zte-modal-btn-cancel"
                    onClick={closeHelp}
                    disabled={helpBusy}
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    className="zte-modal-btn-submit"
                    onClick={(e) => {
                      spawnRipple(e);
                      submitHelp();
                    }}
                    disabled={helpBusy}
                  >
                    {helpBusy ? (
                      <>
                        <span className="zte-spin" />
                        Sending…
                      </>
                    ) : (
                      <>
                        <MessageSquare size={14} strokeWidth={2.4} />
                        Submit
                      </>
                    )}
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {/* ── Break overrun banner — break exceeded its allowed duration ─ */}
      {onBreak && breakGov && breakGov.exceeded && (
        <div
          role="alert"
          style={{
            position: "fixed",
            top: 0,
            left: 0,
            right: 0,
            zIndex: 9998,
            background: isDark ? "rgba(239,68,68,0.16)" : "#FEE2E2",
            borderBottom: `1px solid ${isDark ? "rgba(239,68,68,0.5)" : "#FCA5A5"}`,
            color: "#EF4444",
            padding: "10px 18px",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: 10,
            fontWeight: 700,
            fontSize: 13,
            fontFamily: "inherit",
          }}
        >
          <AlertCircle size={15} strokeWidth={2.4} />
          <span>
            {breakGov.status_chip || "On Break (Exceeded)"} —{" "}
            {breakGov.banner ||
              "Your break allowance has ended. Please resume work."}{" "}
            (Elapsed {Math.floor((breakGov.elapsed_seconds || 0) / 60)}m /
            Allowed {breakGov.allowed_minutes}m / Exceeded{" "}
            {Math.floor((breakGov.exceeded_seconds || 0) / 60)}m)
          </span>
        </div>
      )}

      {/* ── Break-overrun blocking modal — the worker-facing alert. Center
             screen so it can't be missed; no backdrop dismiss (must choose
             Resume Work or Later). Admin + HR are already notified by the
             server-side break-overrun monitor. ── */}
      {/* TEMPORARILY DISABLED: Early Clock Out is inactive — the modal is never
          opened. Kept for future re-enable (EARLY_CLOCK_OUT_ENABLED). */}
      {EARLY_CLOCK_OUT_ENABLED && earlyClockOut && (
        <EarlyClockOutModal
          reason={earlyReason}
          onReasonChange={(v) => {
            setEarlyReason(v);
            if (earlyError) setEarlyError("");
          }}
          error={earlyError}
          submitting={earlySubmitting}
          onCancel={cancelEarlyClockOut}
          onSubmit={submitEarlyClockOut}
        />
      )}

      {showOverrunModal && breakGov && (
        <div
          className="zte-modal-backdrop"
          role="alertdialog"
          aria-modal="true"
          aria-labelledby="zte-overrun-title"
        >
          <div
            className="zte-modal"
            onClick={(e) => e.stopPropagation()}
            style={{ maxWidth: 400 }}
          >
            <div className="zte-modal-head">
              <div
                className="zte-modal-icon"
                style={{
                  background: isDark ? "rgba(239,68,68,0.16)" : "#FEE2E2",
                  borderColor: isDark ? "rgba(239,68,68,0.5)" : "#FCA5A5",
                  color: "#EF4444",
                }}
              >
                <AlertCircle size={18} strokeWidth={2.2} />
              </div>
              <div>
                <p id="zte-overrun-title" className="zte-modal-title">
                  {breakGov.break_type === "lunch"
                    ? "Lunch Break Exceeded"
                    : "Break Time Exceeded"}
                </p>
                <p className="zte-modal-sub">
                  You've been on{" "}
                  {breakGov.break_type === "lunch" ? "lunch" : "your break"} for{" "}
                  {Math.floor((breakGov.elapsed_seconds || 0) / 60)}m (limit{" "}
                  {breakGov.allowed_minutes}m). Please resume work now — your
                  admin and HR have been notified.
                </p>
              </div>
            </div>
            <div className="zte-modal-actions" style={{ marginTop: 16 }}>
              <button
                type="button"
                className="zte-modal-btn-cancel"
                onClick={snoozeOverrun}
                disabled={breakBusy}
              >
                Later
              </button>
              <button
                type="button"
                className="zte-modal-btn-submit"
                onClick={resumeFromOverrun}
                disabled={breakBusy}
              >
                {breakBusy ? (
                  <span className="zte-busy-label">
                    <Loader2 size={15} className="zte-inline-spin" /> Resuming…
                  </span>
                ) : (
                  "Resume Work"
                )}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Break-type picker — short vs long break before /start-break ─ */}
      {showBreakPicker && (
        <div
          className="zte-modal-backdrop"
          onClick={() => setShowBreakPicker(false)}
          role="dialog"
          aria-modal="true"
          aria-labelledby="zte-break-picker-title"
        >
          <div
            className="zte-modal"
            onClick={(e) => e.stopPropagation()}
            style={{ maxWidth: 380 }}
          >
            <button
              type="button"
              className="zte-modal-close"
              onClick={() => setShowBreakPicker(false)}
              aria-label="Close"
              title="Close"
            >
              <X size={14} strokeWidth={2.2} />
            </button>

            <div className="zte-modal-head">
              <div className="zte-modal-icon">
                <Coffee size={18} strokeWidth={2.2} />
              </div>
              <div>
                <p id="zte-break-picker-title" className="zte-modal-title">
                  Start a break
                </p>
                <p className="zte-modal-sub">
                  Pick the break type so we know when to remind you to resume.
                </p>
              </div>
            </div>

            <div
              style={{
                display: "grid",
                gridTemplateColumns: isPartTime ? "1fr" : "1fr 1fr",
                gap: 10,
                marginTop: 6,
              }}
            >
              <button
                type="button"
                disabled={breakBusy}
                onClick={(e) => {
                  spawnRipple(e);
                  setShowBreakPicker(false);
                  handleBreakToggle("break");
                }}
                style={{
                  position: "relative",
                  overflow: "hidden",
                  padding: "14px 10px",
                  borderRadius: 12,
                  border: `1.5px solid ${isDark ? "#92400E" : "#FCD34D"}`,
                  background: isDark ? "rgba(251,191,36,0.10)" : "#FFFBEB",
                  color: isDark ? "#FCD34D" : "#B45309",
                  fontWeight: 700,
                  fontSize: 13,
                  cursor: "pointer",
                  fontFamily: "inherit",
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  gap: 6,
                }}
              >
                <Coffee size={20} strokeWidth={2.2} />
                Short Break
                <span style={{ fontSize: 11, fontWeight: 600, opacity: 0.75 }}>
                  {breakThresholds.short_minutes}m limit
                </span>
              </button>
              {!isPartTime && (
                <button
                  type="button"
                  disabled={breakBusy}
                  onClick={(e) => {
                    spawnRipple(e);
                    setShowBreakPicker(false);
                    handleBreakToggle("lunch");
                  }}
                  style={{
                    position: "relative",
                    overflow: "hidden",
                    padding: "14px 10px",
                    borderRadius: 12,
                    border: `1.5px solid ${isDark ? "#0F766E" : "#5EEAD4"}`,
                    background: isDark ? "rgba(20,184,166,0.10)" : "#F0FDFA",
                    color: isDark ? "#5EEAD4" : "#0F766E",
                    fontWeight: 700,
                    fontSize: 13,
                    cursor: "pointer",
                    fontFamily: "inherit",
                    display: "flex",
                    flexDirection: "column",
                    alignItems: "center",
                    gap: 6,
                  }}
                >
                  <UtensilsCrossed size={20} strokeWidth={2.2} />
                  Lunch Break
                  <span
                    style={{ fontSize: 11, fontWeight: 600, opacity: 0.75 }}
                  >
                    {breakThresholds.lunch_minutes}m limit
                  </span>
                </button>
              )}
            </div>

            <div className="zte-modal-actions" style={{ marginTop: 16 }}>
              <button
                type="button"
                className="zte-modal-btn-cancel"
                onClick={() => setShowBreakPicker(false)}
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Brand caption strip — anchored to the very bottom ─────────── */}
      <div className="zte-brand-strip" aria-hidden="true">
        <span className="zte-brand-strip-name">Temveris</span>
        <span className="zte-brand-strip-dot" />
        <span className="zte-brand-strip-tag">
          Workforce Truth • Financial Control
        </span>
      </div>
    </div>
  );
}

/**
 * blinkLog.js — TEMPORARY diagnostic instrumentation for the blink/reload
 * investigation. REMOVE once the root cause (A–E) is confirmed.
 *
 * Everything is tagged "[BlinkInvestigation]" and mirrored into a localStorage
 * ring buffer (`zt_blink_log`). The ring buffer is the important part: the
 * events we are chasing (location.reload, loadURL, navigation, crashes) tear
 * the page down, so pure console output is often lost. localStorage SURVIVES a
 * reload/navigation and even an app restart, so the entries written in the last
 * moments before a teardown are still there afterwards for export.
 *
 * Channels:
 *   1. console.warn("[BlinkInvestigation]", …)  → visible live in DevTools
 *   2. localStorage "zt_blink_log" (capped)      → survives reloads, exportable
 *   3. window.__blinkLog(entry)                  → bridge for the Electron MAIN
 *        process to append its own events (loadURL / logout / nav-cage) into
 *        the SAME buffer right before it tears the renderer down.
 *
 * Export (employee runs in DevTools console):  __blinkExport()
 */

const TAG = "[BlinkInvestigation]";
const STORE_KEY = "zt_blink_log";
const MAX_ENTRIES = 300;

const CHUNK_RE =
  /loading chunk|loading css chunk|dynamically imported module|chunkloaderror|failed to fetch|networkerror/i;

export function isChunkMessage(msg) {
  return CHUNK_RE.test(String(msg || ""));
}

function readRoute() {
  try {
    return (
      window.location.pathname + window.location.search + window.location.hash
    );
  } catch {
    return "";
  }
}

function readUserId() {
  try {
    return (
      localStorage.getItem("user_id") ||
      localStorage.getItem("zt_user_id") ||
      localStorage.getItem("email") ||
      "unknown"
    );
  } catch {
    return "unknown";
  }
}

function readElectronVersion() {
  try {
    const m = /temveris-desktop\/(\d+\.\d+\.\d+)/i.exec(navigator.userAgent || "");
    if (m) return m[1];
    return typeof window !== "undefined" && window.isDesktopApp
      ? "desktop-unknown"
      : "browser";
  } catch {
    return "unknown";
  }
}

function persist(entry) {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    arr.push(entry);
    while (arr.length > MAX_ENTRIES) arr.shift();
    localStorage.setItem(STORE_KEY, JSON.stringify(arr));
  } catch {
    /* localStorage full/unavailable — the console line below still lands */
  }
}

/** Log a single investigation event from the renderer. */
export function blinkLog(event, detail = {}) {
  const entry = {
    ts: new Date().toISOString(),
    event,
    userId: readUserId(),
    electronVersion: readElectronVersion(),
    route: readRoute(),
    source: "renderer",
    ...detail,
  };
  persist(entry);
  try {
    console.warn(TAG, event, entry);
  } catch {
    /* console unavailable */
  }
  return entry;
}

/**
 * Install global capture + the main-process bridge + the export helper.
 * Idempotent. Call once, as early as possible (main.jsx).
 */
export function installBlinkGlobals() {
  if (typeof window === "undefined" || window.__blinkInstalled) return;
  window.__blinkInstalled = true;

  // ── Main-process bridge ──────────────────────────────────────────────────
  // The Electron MAIN process calls window.__blinkLog({event, ...}) via
  // executeJavaScript so its loadURL / logout / nav-cage events land in the
  // same buffer the renderer exports. We stamp the renderer-side context
  // (route, userId, version) here so main doesn't have to compute it.
  window.__blinkLog = (mainEntry) => {
    try {
      const entry = {
        ts: new Date().toISOString(),
        userId: readUserId(),
        electronVersion: readElectronVersion(),
        route: readRoute(),
        ...(mainEntry || {}),
        source: "main",
      };
      persist(entry);
      console.warn(TAG, entry.event || "main-event", entry);
    } catch {
      /* ignore */
    }
  };

  // ── Export helper (run in DevTools): __blinkExport() ──────────────────────
  window.__blinkExport = () => {
    let raw = "[]";
    try {
      raw = localStorage.getItem(STORE_KEY) || "[]";
    } catch {
      /* ignore */
    }
    try {
      // copy() is a DevTools-console convenience; guarded for safety.
      if (typeof window.copy === "function") window.copy(raw);
    } catch {
      /* ignore */
    }
    console.warn(TAG, "EXPORT BEGIN (copied to clipboard if supported)");
    console.warn(raw);
    console.warn(TAG, "EXPORT END");
    return raw;
  };

  window.__blinkClear = () => {
    try {
      localStorage.removeItem(STORE_KEY);
    } catch {
      /* ignore */
    }
    console.warn(TAG, "buffer cleared");
  };

  // ── Uncaught errors (covers ChunkLoadError + generic React render crashes) ─
  window.addEventListener("error", (e) => {
    const msg = (e?.message || e?.error?.message || "").toString();
    blinkLog(isChunkMessage(msg) ? "chunk-load-error" : "window-error", {
      message: msg.slice(0, 400),
      filename: e?.filename,
      stack: e?.error?.stack ? String(e.error.stack).slice(0, 800) : undefined,
    });
  });

  // ── Unhandled promise rejections (failed dynamic import()s land here too) ──
  window.addEventListener("unhandledrejection", (e) => {
    const reason = e?.reason;
    const msg = (reason?.message || String(reason || "")).toString();
    blinkLog(
      isChunkMessage(msg) ? "chunk-load-rejection" : "unhandled-rejection",
      {
        message: msg.slice(0, 400),
        stack: reason?.stack ? String(reason.stack).slice(0, 800) : undefined,
      },
    );
  });

  // ── The actual teardown — fires for EVERY reload/navigation regardless of
  //    who triggered it (renderer reload OR main-process loadURL). ───────────
  window.addEventListener("beforeunload", () => {
    blinkLog("document-unload", { trigger: "beforeunload" });
  });
  window.addEventListener("pagehide", () => {
    blinkLog("document-unload", { trigger: "pagehide" });
  });

  // ── Patch location.reload so any reload (even one we didn't instrument)
  //    records WHO called it via a stack trace. Best-effort: some engines make
  //    this non-configurable, in which case the explicit call-site logs +
  //    beforeunload still cover us. ───────────────────────────────────────────
  try {
    const origReload = window.location.reload.bind(window.location);
    Object.defineProperty(window.location, "reload", {
      configurable: true,
      writable: true,
      value: function patchedReload(...args) {
        blinkLog("location-reload-called", {
          stack: new Error("location.reload").stack
            ? String(new Error("location.reload").stack).slice(0, 800)
            : undefined,
        });
        return origReload(...args);
      },
    });
  } catch {
    /* reload not patchable here — rely on call-site logs + beforeunload */
  }

  blinkLog("instrumentation-installed", {
    userAgent: (navigator.userAgent || "").slice(0, 200),
  });
}

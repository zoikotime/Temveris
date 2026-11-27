import { createContext, useContext, useEffect, useMemo, useRef, useState, useCallback } from "react";
import { API_BASE } from "../config";
// TEMPORARY: blink/reload investigation. Remove with the rest of the
// instrumentation once the root cause is confirmed.
import { blinkLog } from "../lib/blinkLog";

const ConnectionContext = createContext(null);

const HEALTH_URL = `${API_BASE}/health`;
const HEALTH_POLL_MS = 15000;
const HEALTH_TIMEOUT_MS = 10000;       // single-request timeout (was 6s — too tight)
const FAIL_THRESHOLD = 3;              // consecutive failures before overlay shows
const OFFLINE_STORAGE_KEY = "zt_offline_since";

// Why a threshold instead of "fail once → block UI":
// A single failed /health probe used to flip the whole app into the
// "Connection Interrupted" modal. In practice that fires on every transient
// blip — backend cold-start, TCP retransmit, WiFi handoff, deploy bounce —
// even when the user's network is fine. Requiring N consecutive failures
// (~45s of real outage at 15s polling) eliminates the false alarms while
// still catching genuine outages within a minute.

const readOfflineSince = () => {
  const v = localStorage.getItem(OFFLINE_STORAGE_KEY);
  const n = v ? parseInt(v, 10) : NaN;
  return Number.isFinite(n) ? n : null;
};

const writeOfflineSince = (ms) => {
  if (ms == null) localStorage.removeItem(OFFLINE_STORAGE_KEY);
  else localStorage.setItem(OFFLINE_STORAGE_KEY, String(ms));
};

export function ConnectionProvider({ children }) {
  const [browserOnline, setBrowserOnline]   = useState(navigator.onLine);
  const [backendOk, setBackendOk]           = useState(true);
  const [offlineSince, setOfflineSince]     = useState(readOfflineSince());
  const pollTimerRef  = useRef(null);
  const inFlightRef   = useRef(false);
  const failCountRef  = useRef(0);  // consecutive failed health checks
  // [NetDiag] TEMPORARY diagnostics — last /health probe outcome
  // {kind,status,ms}; used only to attribute an overlay event to its trigger.
  // Remove together with the rest of the [NetDiag] instrumentation.
  const lastProbeRef  = useRef(null);

  const connected = browserOnline && backendOk;
  // [NetDiag] TEMPORARY — previous `connected`, to detect the exact
  // true->false transition (= the moment the overlay becomes visible).
  const prevConnectedRef = useRef(connected);

  // Mark the start of an interruption only once, so refresh-triggered
  // reconnects can compute the total offline duration.
  const markOffline = useCallback(() => {
    setOfflineSince((prev) => {
      if (prev != null) return prev;
      const now = Date.now();
      writeOfflineSince(now);
      return now;
    });
  }, []);

  const clearOffline = useCallback(() => {
    setOfflineSince(null);
    writeOfflineSince(null);
  }, []);

  const pingHealth = useCallback(async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    // [NetDiag] TEMPORARY — response-time measurement start.
    const _t0 = performance.now();
    try {
      const ctrl = new AbortController();
      const to = setTimeout(() => ctrl.abort(), HEALTH_TIMEOUT_MS);
      const res = await fetch(HEALTH_URL, { method: "GET", signal: ctrl.signal, cache: "no-store" });
      clearTimeout(to);
      if (res.ok) {
        failCountRef.current = 0;
        setBackendOk(true);
      } else {
        failCountRef.current += 1;
        if (failCountRef.current >= FAIL_THRESHOLD) setBackendOk(false);
      }
      // [NetDiag] TEMPORARY — record probe outcome (no behaviour change).
      const _ms = Math.round(performance.now() - _t0);
      lastProbeRef.current = { kind: res.ok ? "ok" : `http-${res.status}`, status: res.status, ms: _ms };
      blinkLog("health-probe", {
        status: res.status,
        ok: res.ok,
        ms: _ms,
        failCount: failCountRef.current,
        navigatorOnLine: navigator.onLine,
      });
    } catch (err) {
      failCountRef.current += 1;
      if (failCountRef.current >= FAIL_THRESHOLD) setBackendOk(false);
      // [NetDiag] TEMPORARY — distinguish timeout (AbortError) from a real
      // network/fetch error (DNS, CORS, connection refused).
      const _ms = Math.round(performance.now() - _t0);
      const _kind = err && err.name === "AbortError" ? "timeout" : "fetch-error";
      lastProbeRef.current = { kind: _kind, status: null, ms: _ms };
      blinkLog("health-probe-error", {
        kind: _kind,
        message: String((err && err.message) || err || "").slice(0, 200),
        ms: _ms,
        failCount: failCountRef.current,
        navigatorOnLine: navigator.onLine,
      });
    } finally {
      inFlightRef.current = false;
    }
  }, []);

  // Manual refresh path (e.g. the overlay "Refresh" button) needs to bypass
  // the consecutive-failure threshold so the user gets immediate feedback —
  // a single successful probe should clear the overlay, a single failed one
  // should keep it. Reset the counter and short-circuit the threshold check.
  const forcePingHealth = useCallback(async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    // [NetDiag] TEMPORARY — response-time measurement start.
    const _t0 = performance.now();
    try {
      const ctrl = new AbortController();
      const to = setTimeout(() => ctrl.abort(), HEALTH_TIMEOUT_MS);
      const res = await fetch(HEALTH_URL, { method: "GET", signal: ctrl.signal, cache: "no-store" });
      clearTimeout(to);
      if (res.ok) {
        failCountRef.current = 0;
        setBackendOk(true);
      } else {
        setBackendOk(false);
      }
      // [NetDiag] TEMPORARY — record forced (Refresh-button) probe outcome.
      const _ms = Math.round(performance.now() - _t0);
      lastProbeRef.current = { kind: res.ok ? "ok" : `http-${res.status}`, status: res.status, ms: _ms };
      blinkLog("health-probe-force", {
        status: res.status,
        ok: res.ok,
        ms: _ms,
        failCount: failCountRef.current,
        navigatorOnLine: navigator.onLine,
      });
    } catch (err) {
      setBackendOk(false);
      // [NetDiag] TEMPORARY — timeout (AbortError) vs network/fetch error.
      const _ms = Math.round(performance.now() - _t0);
      const _kind = err && err.name === "AbortError" ? "timeout" : "fetch-error";
      lastProbeRef.current = { kind: _kind, status: null, ms: _ms };
      blinkLog("health-probe-force-error", {
        kind: _kind,
        message: String((err && err.message) || err || "").slice(0, 200),
        ms: _ms,
        failCount: failCountRef.current,
        navigatorOnLine: navigator.onLine,
      });
    } finally {
      inFlightRef.current = false;
    }
  }, []);

  useEffect(() => {
    const onOnline  = () => { blinkLog("browser-online",  { navigatorOnLine: navigator.onLine }); setBrowserOnline(true);  pingHealth(); };
    const onOffline = () => { blinkLog("browser-offline", { navigatorOnLine: navigator.onLine }); setBrowserOnline(false); };
    window.addEventListener("online",  onOnline);
    window.addEventListener("offline", onOffline);
    return () => {
      window.removeEventListener("online",  onOnline);
      window.removeEventListener("offline", onOffline);
    };
  }, [pingHealth]);

  // Periodic backend health probe — catches server crashes even when the
  // browser itself still reports "online".
  useEffect(() => {
    pingHealth();
    // Skip the periodic probe while the tab is hidden: a backgrounded tab
    // doesn't need a fresh connection state and the spaced-out 15s fetches
    // are pure battery/backend overhead. We only gate the *interval* tick —
    // the `online`/`offline` window listeners and the manual force-refresh
    // path are untouched, so offline/recovery detection semantics are
    // preserved. When the tab is shown again the next tick (≤15s later)
    // resumes probing, and the threshold logic is unchanged throughout.
    pollTimerRef.current = setInterval(() => {
      if (document.hidden) return;
      pingHealth();
    }, HEALTH_POLL_MS);
    return () => {
      if (pollTimerRef.current) clearInterval(pollTimerRef.current);
    };
  }, [pingHealth]);

  // Reconcile the persisted offline marker with current state.
  useEffect(() => {
    if (!connected) markOffline();
  }, [connected, markOffline]);

  // [BlinkInvestigation] log every connection-state change — lets us correlate
  // a backend-probe failure (candidate B) with the overlay + any reload.
  useEffect(() => {
    const wasConnected = prevConnectedRef.current;
    prevConnectedRef.current = connected;

    blinkLog("connection-state-change", {
      connected,
      browserOnline,
      backendOk,
      failCount: failCountRef.current,
      navigatorOnLine: navigator.onLine,
    });

    // [NetDiag] TEMPORARY — OVERLAY EVENT attribution. The overlay is visible
    // whenever `connected` is false. Log the exact true->false transition and
    // attribute it to navigator.onLine=false vs the last /health probe outcome
    // (http-429 / http-500 / http-503 / timeout / fetch-error / ...).
    if (wasConnected && !connected) {
      const probe = lastProbeRef.current;
      blinkLog("overlay-shown", {
        cause: !browserOnline ? "navigator-offline" : (probe ? probe.kind : "unknown"),
        healthStatus: probe ? probe.status : null,
        healthMs: probe ? probe.ms : null,
        failCount: failCountRef.current,
        browserOnline,
        backendOk,
        navigatorOnLine: navigator.onLine,
      });
    } else if (!wasConnected && connected) {
      blinkLog("overlay-hidden", {
        failCount: failCountRef.current,
        navigatorOnLine: navigator.onLine,
      });
    }
  }, [connected, browserOnline, backendOk]);

  // Memoize the provider value so consumers of useConnection() only re-render
  // when the connection state actually changes. Without this, every
  // ConnectionProvider re-render (e.g. when ThemeProvider above flips dark
  // mode) hands every subscriber a fresh object reference and forces them
  // to re-render alongside.
  const value = useMemo(() => ({
    connected,
    browserOnline,
    backendOk,
    offlineSince,
    pingHealth: forcePingHealth,  // overlay "Refresh" → immediate feedback
    markOffline,
    clearOffline,
  }), [connected, browserOnline, backendOk, offlineSince, forcePingHealth, markOffline, clearOffline]);

  return (
    <ConnectionContext.Provider value={value}>
      {children}
    </ConnectionContext.Provider>
  );
}

export function useConnection() {
  const ctx = useContext(ConnectionContext);
  if (!ctx) throw new Error("useConnection must be used inside ConnectionProvider");
  return ctx;
}

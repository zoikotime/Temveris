import React, { useState, useEffect, useRef, useCallback } from "react";
import { getOrgTimeZone } from "../lib/orgTime";
import { API_BASE, fetchWithRetry } from "../config";
import { getBreakThresholds, getMe } from "../api/session";
import { useConnection } from "../context/ConnectionContext";
import { useLanguage } from "../context/LanguageContext";
import EarlyClockOutModal, {
  EARLY_CLOCK_OUT_ENABLED,
} from "./EarlyClockOutModal";

// ── helpers ──────────────────────────────────────────────────────────────────
const getToken = () =>
  localStorage.getItem("zt_token") || localStorage.getItem("token") || null;
const OVERRUN_FIRST_NOTIFICATION_DELAY_MINUTES = 5;

const fmtClock = (s) => {
  if (!s || s < 0) s = 0;
  return `${String(Math.floor(s / 3600)).padStart(2, "0")}:${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
};

const fmtHM = (s) => {
  if (!s || s <= 0) return "0h 0m";
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
};

// ── inline mini-icons ─────────────────────────────────────────────────────────
const Svg = ({ d, s = 16, c = "#64748B", fw = "2" }) => (
  <svg
    width={s}
    height={s}
    viewBox="0 0 24 24"
    fill="none"
    stroke={c}
    strokeWidth={fw}
    strokeLinecap="round"
    strokeLinejoin="round"
    style={{ display: "inline-block", flexShrink: 0 }}
  >
    {d.split(" M").map((seg, i) => (
      <path key={i} d={i === 0 ? seg : "M" + seg} />
    ))}
  </svg>
);

const ICONS = {
  play: "M5 3l14 9-14 9V3z",
  pause: "M6 4h4v16H6z M14 4h4v16h-4z",
  stop: "M18 6L6 18 M6 6l12 12",
  coffee:
    "M18 8h1a4 4 0 010 8h-1 M2 8h16v9a4 4 0 01-4 4H6a4 4 0 01-4-4V8z M6 1v3 M10 1v3 M14 1v3",
  resume: "M5 3l14 9-14 9V3z",
  clock: "M12 2a10 10 0 100 20A10 10 0 0012 2z M12 6v6l4 2",
  logout: "M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4 M16 17l5-5-5-5 M21 12H9",
  check: "M20 6L9 17l-5-5",
  alert:
    "M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z M12 9v4 M12 17h.01",
  x: "M18 6L6 18 M6 6l12 12",
};

// ── ClockInWidget ─────────────────────────────────────────────────────────────
export default function ClockInWidget({ isDark = false }) {
  const { connected } = useConnection();
  const { T } = useLanguage();
  const C = {
    // Dark mode uses neutral near-blacks (no blue cast) to match the
    // global ThemeContext palette and the rest of the dashboard surfaces.
    bg: isDark ? "#0A0A0A" : "#F8FAFC",
    surface: isDark ? "#141414" : "#FFFFFF",
    border: isDark ? "#262626" : "#E2E8F0",
    text: isDark ? "#F5F5F5" : "#1E293B",
    textSub: isDark ? "#A3A3A3" : "#64748B",
    brand: "#11496A",
    brandBg: isDark ? "rgba(17,73,106,0.18)" : "#E7F0F6",
    green: "#44B97B",
    greenBg: isDark ? "rgba(68,185,123,0.14)" : "#E8F7EE",
    greenBdr: isDark ? "rgba(68,185,123,0.4)" : "#A8E0BD",
    amber: "#F59E0B",
    amberBg: isDark ? "rgba(245,158,11,0.12)" : "#FEF3C7",
    amberBdr: isDark ? "rgba(245,158,11,0.4)" : "#FCD34D",
    red: "#EF4444",
    redBg: isDark ? "rgba(239,68,68,0.12)" : "#FEE2E2",
    redBdr: isDark ? "rgba(239,68,68,0.4)" : "#FCA5A5",
  };

  // ── state ──
  const [status, setStatus] = useState("loading"); // loading | not_clocked_in | working | on_break | clocked_out
  const [elapsed, setElapsed] = useState(0);
  const [breakElapsed, setBreakElapsed] = useState(0);
  const [workedSecs, setWorkedSecs] = useState(0);
  const [clockInTime, setClockInTime] = useState(null);
  const [clockOutTime, setClockOutTime] = useState(null);
  const [clockingIn, setClockingIn] = useState(false);
  const [error, setError] = useState(null);
  const [userName, setUserName] = useState("Admin");

  // break reason modal
  const [showBreakModal, setShowBreakModal] = useState(false);
  const [breakReason, setBreakReason] = useState("");
  const [breakErr, setBreakErr] = useState("");

  // Controlled Clock-Out: populated with the backend's
  // EARLY_CLOCK_OUT_REASON_REQUIRED response when a typed reason is needed.
  // Non-null means "still clocked in, awaiting a reason".
  const [earlyClockOut, setEarlyClockOut] = useState(null);
  const [earlyReason, setEarlyReason] = useState("");
  const [earlyError, setEarlyError] = useState("");
  const [earlySubmitting, setEarlySubmitting] = useState(false);

  // ── Break-type + overtime alert state ─────────────────────────────────────
  // breakType is "break" (short) or "lunch" (long). The threshold is read
  // from /break/thresholds so admins can tune the limits without a deploy.
  const [breakType, setBreakType] = useState("break");
  const [breakTypePicker, setBreakTypePicker] = useState(false); // mini-modal before starting
  const [thresholds, setThresholds] = useState({
    short_minutes: 15,
    lunch_minutes: 60,
  });
  // Per-workday entitlements (governance). lunchAllowed is false for part-time
  // and (by default policy) contractors — the UI side of the spec's Test 5.
  const [lunchAllowed, setLunchAllowed] = useState(true);
  // Locally-tracked elapsed seconds (used for instant popup firing even when
  // backend polling is delayed). Updated by the same ticker that runs the
  // session clock. Overtime checking is now driven by the backend /employees/me
  // governance view on the regular poll cadence (30s) instead of redundant /break/active.
  const overtimeFiredRef = useRef(false);

  // daily accumulation
  const [dailyWorkedSecs, setDailyWorkedSecs] = useState(0);
  const prevWorkedSecsRef = useRef(0);

  // refs
  const timerRef = useRef(null);
  const clockInMsRef = useRef(null);
  const breakStartMsRef = useRef(null);
  const totalBreakMsRef = useRef(0);
  // Tracks the start of a network-interrupt pause so we can subtract that
  // window from worked time when the connection returns.
  const netPauseStartMsRef = useRef(null);

  const showError = (msg) => {
    setError(msg);
    setTimeout(() => setError(null), 5000);
  };

  // ── timer ──
  const startTick = useCallback(() => {
    if (timerRef.current) clearInterval(timerRef.current);
    timerRef.current = setInterval(() => {
      const ms = Date.now();
      const total = ms - (clockInMsRef.current || ms);
      const brk =
        totalBreakMsRef.current +
        (breakStartMsRef.current !== null ? ms - breakStartMsRef.current : 0);
      const sessionSecs = Math.floor(Math.max(0, total - brk) / 1000);
      setElapsed(sessionSecs);
      setBreakElapsed(Math.floor(brk / 1000));
      setDailyWorkedSecs(prevWorkedSecsRef.current + sessionSecs);
    }, 1000);
  }, []);

  const stopTick = useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  useEffect(() => () => stopTick(), [stopTick]);

  // ── network-interrupt pause/resume ──
  // While disconnected the timer interval is frozen and the moment of
  // disconnect is recorded. On reconnect we credit that exact duration to
  // the break counter so worked time resumes from the value it had when the
  // interruption began.
  useEffect(() => {
    if (status !== "working" && status !== "on_break") return;
    if (!connected) {
      if (netPauseStartMsRef.current == null) {
        netPauseStartMsRef.current = Date.now();
      }
      stopTick();
      return;
    }
    if (netPauseStartMsRef.current != null) {
      const offlineMs = Date.now() - netPauseStartMsRef.current;
      netPauseStartMsRef.current = null;
      if (offlineMs > 0) totalBreakMsRef.current += offlineMs;
      startTick();
    }
  }, [connected, status, startTick, stopTick]);

  // ── load workspace break thresholds (overtime alert limits) ──
  // Workers + admins both read this. The popup uses these minutes so admins
  // can change the policy via /break/thresholds without a redeploy.
  useEffect(() => {
    const tok = getToken();
    if (!tok) return;
    getBreakThresholds()
      .then((d) => {
        if (d && (d.short_minutes || d.lunch_minutes)) {
          setThresholds({
            short_minutes: Number(d.short_minutes) || 15,
            lunch_minutes: Number(d.lunch_minutes) || 60,
          });
        }
      })
      .catch(() => {});
  }, []);

  // ── load per-workday break entitlements ──
  // Drives whether the Lunch option is offered at all (part-time / contractor
  // workers are not entitled to a lunch break under the governance policy).
  useEffect(() => {
    const tok = getToken();
    if (!tok) return;
    fetch(`${API_BASE}/break/entitlements`, {
      headers: { Authorization: `Bearer ${tok}` },
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d && typeof d.lunch_allowed === "boolean")
          setLunchAllowed(d.lunch_allowed);
      })
      .catch(() => {});
  }, []);

  // ── overtime watcher ──
  // Break overrun state is now driven by governance view from /employees/me
  // on the regular 30s poll instead of redundant /break/active calls.
  // The backend broadcast + upstream reconcile handles the freshness.
  useEffect(() => {
    if (status !== "on_break") {
      overtimeFiredRef.current = false;
      setOvertimeAlert(null);
      return;
    }
    // Local timer for instant UI feedback when the threshold is crossed
    const limitMin =
      breakType === "lunch"
        ? thresholds.lunch_minutes
        : thresholds.short_minutes;
    const limitMs =
      ((limitMin || 15) + OVERRUN_FIRST_NOTIFICATION_DELAY_MINUTES) * 60 * 1000;
    const iv = setInterval(() => {
      if (!breakStartMsRef.current) return;
      const elapsedMs = Date.now() - breakStartMsRef.current;
      if (elapsedMs >= limitMs && !overtimeFiredRef.current) {
        overtimeFiredRef.current = true;
        setOvertimeAlert({ break_type: breakType, threshold_min: limitMin });
        // Browser notification — silently ignored if permission denied.
        try {
          if (typeof Notification !== "undefined") {
            if (Notification.permission === "granted") {
              new Notification(
                breakType === "lunch"
                  ? "Lunch break over an hour"
                  : "Break over 15 minutes",
                {
                  body:
                    breakType === "lunch"
                      ? `You've been on lunch for over ${limitMin} minutes. Time to clock back in.`
                      : `You've been on break for over ${limitMin} minutes. Time to resume work.`,
                  tag: `zt-break-overtime-${breakType}`,
                },
              );
            } else if (Notification.permission !== "denied") {
              Notification.requestPermission().catch(() => {});
            }
          }
        } catch {}
        // Note: server-side broadcast now handles admin dashboard alert
        // (no need for redundant /break/active POST call)
      }
    }, 5_000);
    return () => clearInterval(iv);
  }, [status, breakType, thresholds]);

  // ── session restore on mount ──
  useEffect(() => {
    const tok = getToken();
    if (!tok) {
      setStatus("not_clocked_in");
      return;
    }
    getMe()
      .catch(() => null)
      .then((d) => {
        if (!d) {
          setStatus("not_clocked_in");
          return;
        }
        if (d.name) setUserName(d.name.split(" ")[0] || d.name);
        prevWorkedSecsRef.current = d.worked_today_seconds || 0;
        if (d.clocked_in) {
          clockInMsRef.current = new Date(d.clock_in_time).getTime();
          totalBreakMsRef.current = (d.break_elapsed_seconds || 0) * 1000;
          setClockInTime(d.clock_in_time);
          if (d.on_break) {
            setStatus("on_break");
            breakStartMsRef.current = Date.now();
            // Break state is now reconciled from /employees/me governance view
            // on the regular poll (every 30s), no need for redundant /break/active call
          } else setStatus("working");
          startTick();
        } else {
          setDailyWorkedSecs(prevWorkedSecsRef.current);
          setStatus("not_clocked_in");
        }
      })
      .catch(() => setStatus("not_clocked_in"));
  }, [startTick]);

  // ── midnight reset — re-fetch when date changes ──
  useEffect(() => {
    let lastDate = new Date().toDateString();
    const iv = setInterval(() => {
      const now = new Date().toDateString();
      if (now !== lastDate) {
        lastDate = now;
        prevWorkedSecsRef.current = 0;
        setDailyWorkedSecs(0);
        setWorkedSecs(0);
        const tok = getToken();
        if (!tok) return;
        fetch(`${API_BASE}/employees/me`, {
          headers: { Authorization: `Bearer ${tok}` },
        })
          .then((r) => (r.ok ? r.json() : null))
          .then((d) => {
            if (!d || !d.clocked_in) {
              stopTick();
              setStatus("not_clocked_in");
            }
          })
          .catch(() => {});
      }
    }, 30_000);
    return () => clearInterval(iv);
  }, [stopTick]);

  // ── clock in ──
  const handleClockIn = async () => {
    const tok = getToken();
    if (!tok) {
      showError("No auth token. Please log in again.");
      return;
    }
    setClockingIn(true);
    try {
      const res = await fetchWithRetry(`${API_BASE}/employees/clock-in`, {
        method: "POST",
        headers: { Authorization: `Bearer ${tok}` },
      });
      if (!res.ok) {
        const e = await res.json().catch(() => ({}));
        throw new Error(e.detail || `Error ${res.status}`);
      }
      const d = await res.json();

      if (d.success === false) {
        // Already clocked in — restore
        const me = await fetch(`${API_BASE}/employees/me`, {
          headers: { Authorization: `Bearer ${tok}` },
        }).then((r) => r.json());
        if (me.clocked_in) {
          prevWorkedSecsRef.current = me.worked_today_seconds || 0;
          clockInMsRef.current = new Date(me.clock_in_time).getTime();
          totalBreakMsRef.current = (me.break_elapsed_seconds || 0) * 1000;
          setClockInTime(me.clock_in_time);
          setStatus(me.on_break ? "on_break" : "working");
          startTick();
        }
        return;
      }
      // Update UI immediately — don't block on /me
      const startMs = d.clock_in_time
        ? new Date(d.clock_in_time).getTime()
        : Date.now();
      clockInMsRef.current = startMs;
      totalBreakMsRef.current = 0;
      breakStartMsRef.current = null;
      setClockInTime(d.clock_in_time || new Date(startMs).toISOString());
      setElapsed(0);
      setBreakElapsed(0);
      setStatus("working");
      startTick();
      // Defer /me fetch to avoid request storm; next upstream poll will get fresh data
      // and the upstream cache (5s TTL) will serve stale read efficiently if called again
    } catch (e) {
      showError(`Clock-in failed: ${e.message}`);
    } finally {
      setClockingIn(false);
    }
  };

  // ── clock out ──
  // POST /employees/clock-out, optionally with a typed Early Clock Out reason.
  // Returns the parsed JSON; throws only on a non-OK HTTP status.
  const postClockOut = async (reasonText) => {
    const tok = getToken();
    const opts = { method: "POST", headers: { Authorization: `Bearer ${tok}` } };
    if (reasonText != null) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify({ reason: reasonText });
    }
    const res = await fetchWithRetry(`${API_BASE}/employees/clock-out`, opts);
    if (!res.ok) {
      const e = await res.json().catch(() => ({}));
      throw new Error(e.detail || `Error ${res.status}`);
    }
    return await res.json().catch(() => ({}));
  };

  // Applies a CONFIRMED clock-out to local state. Only called once the server
  // has actually closed the session.
  const finalizeClockOut = (d) => {
    const worked = d.worked_seconds != null ? d.worked_seconds : elapsed;
    prevWorkedSecsRef.current += worked;
    setDailyWorkedSecs(prevWorkedSecsRef.current);
    setWorkedSecs(prevWorkedSecsRef.current);
    setClockOutTime(d.clock_out_time || new Date().toISOString());
    setStatus("clocked_out");
    clockInMsRef.current = null;
    breakStartMsRef.current = null;
    totalBreakMsRef.current = 0;
  };

  const handleClockOut = async () => {
    const tok = getToken();
    if (!tok) return;
    stopTick();
    try {
      const d = await postClockOut(null);
      // The Controlled Clock-Out gate answers HTTP 200 with
      // {success:false, code:"EARLY_CLOCK_OUT_REASON_REQUIRED"} and leaves the
      // session OPEN. `res.ok` is true for it, so checking only the status code
      // used to flip the UI to "clocked out" while the worker was still clocked
      // in — the server and the screen disagreed until the next refresh.
      // TEMPORARILY DISABLED: Early Clock Out is inactive, so this branch never
      // runs (the backend gate is off and no longer returns the code). Kept for
      // future re-enable — see EARLY_CLOCK_OUT_ENABLED in EarlyClockOutModal.
      if (EARLY_CLOCK_OUT_ENABLED && d && d.code === "EARLY_CLOCK_OUT_REASON_REQUIRED") {
        setEarlyClockOut(d);
        setEarlyReason("");
        setEarlyError("");
        startTick(); // still working — keep the timer running
        return;
      }
      finalizeClockOut(d);
    } catch (e) {
      showError(`Clock-out failed: ${e.message}`);
      startTick();
    }
  };

  // Re-submit with the typed reason. Same endpoint, same gate — the server is
  // still the authority on whether the session actually closed.
  const submitEarlyClockOut = async () => {
    const reason = (earlyReason || "").trim();
    setEarlySubmitting(true);
    setEarlyError("");
    try {
      const d = await postClockOut(reason);
      if (d && d.code === "EARLY_CLOCK_OUT_REASON_REQUIRED") {
        setEarlyError(d.message || "A longer reason is required.");
        return;
      }
      setEarlyClockOut(null);
      stopTick();
      finalizeClockOut(d);
    } catch (e) {
      setEarlyError(e.message || "Clock-out failed.");
    } finally {
      setEarlySubmitting(false);
    }
  };

  // ── start break ──
  // `type` is "break" (default short) or "lunch" (long). Server-side it
  // selects which overtime threshold applies.
  const handleStartBreak = useCallback(async (type = "break") => {
    const tok = getToken();
    if (!tok) return;
    const btype = type === "lunch" ? "lunch" : "break";
    try {
      const res = await fetchWithRetry(`${API_BASE}/employees/start-break`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${tok}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ break_type: btype }),
      });
      const d = await res.json().catch(() => ({}));
      // Governed denial: the server returns { allowed:false, code, message }
      // (e.g. DAILY_SHORT_BREAK_ALREADY_USED, LUNCH_BREAK_NOT_ALLOWED). Surface
      // the spec message verbatim instead of a generic toast.
      if (!res.ok || d.allowed === false || d.success === false) {
        showError(d.message || d.detail || "Failed to start break");
        return;
      }
      breakStartMsRef.current = Date.now();
      setStatus("on_break");
      setBreakType(btype);
      overtimeFiredRef.current = false;
      setOvertimeAlert(null);
    } catch {
      showError("Failed to start break");
    }
  }, []);

  // ── resume from break ──
  const handleResume = async () => {
    if (!breakReason.trim()) {
      setBreakErr("Please enter a reason for your break");
      return;
    }
    setBreakErr("");
    const tok = getToken();
    if (!tok) return;
    try {
      const res = await fetchWithRetry(`${API_BASE}/break/resume`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${tok}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ reason: breakReason.trim() }),
      });
      const d = await res.json();
      if (res.ok && d.success !== false) {
        if (breakStartMsRef.current !== null) {
          totalBreakMsRef.current += Date.now() - breakStartMsRef.current;
          breakStartMsRef.current = null;
        }
        setStatus("working");
        setShowBreakModal(false);
        setBreakReason("");
      } else {
        // fallback
        const r2 = await fetchWithRetry(`${API_BASE}/employees/resume-work`, {
          method: "POST",
          headers: { Authorization: `Bearer ${tok}` },
        });
        const d2 = await r2.json();
        if (d2.success !== false) {
          if (breakStartMsRef.current !== null) {
            totalBreakMsRef.current += Date.now() - breakStartMsRef.current;
            breakStartMsRef.current = null;
          }
          setStatus("working");
          setShowBreakModal(false);
          setBreakReason("");
        } else {
          setBreakErr("Failed to resume. Please try again.");
        }
      }
    } catch {
      setBreakErr("Network error. Please try again.");
    }
  };

  // ── helpers for display ──
  const initials = (n) =>
    n
      .split(" ")
      .map((w) => w[0])
      .join("")
      .toUpperCase()
      .slice(0, 2) || "AD";
  const now = new Date();
  const h = now.getHours();
  const greeting =
    h < 12
      ? T.greeting_morning || "Good Morning"
      : h < 17
        ? T.greeting_afternoon || "Good Afternoon"
        : T.greeting_evening || "Good Evening";
  const timeStr =
    now
      .toLocaleTimeString("en-IN", {
        hour: "2-digit",
        minute: "2-digit",
        hour12: true,
        timeZone: getOrgTimeZone(),
      })
      .toUpperCase() + " IST";
  const fmtTime = (iso) =>
    iso
      ? new Date(iso).toLocaleTimeString("en-IN", {
          hour: "2-digit",
          minute: "2-digit",
          hour12: true,
          timeZone: getOrgTimeZone(),
        })
      : "—";

  const card = (extra = {}) => ({
    background: C.surface,
    borderRadius: 12,
    border: `1px solid ${C.border}`,
    padding: "20px 24px",
    ...extra,
  });

  // ── Break-governance derived state (status chip / banner / breakdown) ──
  const breakLabelTxt =
    breakType === "lunch"
      ? T.lunchBreak || "Lunch Break"
      : T.shortBreak || "Short Break";
  const breakAllowedMin =
    breakType === "lunch"
      ? thresholds.lunch_minutes || 60
      : thresholds.short_minutes || 15;
  const breakExceeded =
    status === "on_break" && breakElapsed >= breakAllowedMin * 60;
  const exceededSecs = Math.max(0, breakElapsed - breakAllowedMin * 60);
  // Spec status chip: "On Short Break" / "On Lunch Break", + " (Exceeded)".
  const statusChip =
    status === "on_break"
      ? `On ${breakLabelTxt}${breakExceeded ? " (Exceeded)" : ""}`
      : T.working || "Working";
  const statusColor =
    status === "on_break" ? (breakExceeded ? C.red : C.amber) : C.green;
  const statusBg =
    status === "on_break" ? (breakExceeded ? C.redBg : C.amberBg) : C.greenBg;
  const fmtMins = (s) => `${Math.floor((s || 0) / 60)}m`;

  // ── render ─────────────────────────────────────────────────────────────────
  if (status === "loading") {
    return (
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          minHeight: 220,
        }}
      >
        <div
          style={{
            width: 28,
            height: 28,
            borderRadius: "50%",
            border: `3px solid ${C.brand}`,
            borderTopColor: "transparent",
            animation: "spin .8s linear infinite",
          }}
        />
      </div>
    );
  }

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 16,
        maxWidth: 700,
        fontFamily: "'Inter','Segoe UI',sans-serif",
      }}
    >
      {/* Controlled Clock-Out — reason required. The worker stays clocked in
          and the timer keeps running until the server confirms the close. */}
      {/* TEMPORARILY DISABLED: Early Clock Out is inactive — the modal is never
          opened. Kept for future re-enable (EARLY_CLOCK_OUT_ENABLED). */}
      {EARLY_CLOCK_OUT_ENABLED && earlyClockOut && (
        <EarlyClockOutModal
          reason={earlyReason}
          onReasonChange={setEarlyReason}
          error={earlyError}
          submitting={earlySubmitting}
          onCancel={() => {
            setEarlyClockOut(null);
            setEarlyError("");
          }}
          onSubmit={submitEarlyClockOut}
        />
      )}

      {/* Break reason modal */}
      {showBreakModal && (
        <div
          onClick={() => {
            setShowBreakModal(false);
            setBreakReason("");
            setBreakErr("");
          }}
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 9999,
            background: "rgba(0,0,0,0.45)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              background: C.surface,
              borderRadius: 14,
              padding: 28,
              width: 420,
              boxShadow: "0 24px 60px rgba(0,0,0,0.2)",
              border: `1px solid ${C.border}`,
            }}
          >
            <p
              style={{
                fontSize: 15,
                fontWeight: 700,
                color: C.text,
                marginBottom: 4,
              }}
            >
              {T.resumeWork || "Resume Work"}
            </p>
            <p style={{ fontSize: 12, color: C.textSub, marginBottom: 16 }}>
              {T.resumeWorkReason ||
                "Please enter a reason for your break before resuming."}
            </p>
            <textarea
              value={breakReason}
              onChange={(e) => setBreakReason(e.target.value)}
              rows={3}
              placeholder={
                T.breakReasonPlaceholder ||
                "e.g. Lunch break, Doctor visit, Personal errand..."
              }
              style={{
                width: "100%",
                boxSizing: "border-box",
                border: `1px solid ${breakErr ? C.red : C.border}`,
                borderRadius: 8,
                padding: "10px 12px",
                fontSize: 13,
                fontFamily: "inherit",
                color: C.text,
                background: C.bg,
                resize: "none",
                outline: "none",
              }}
            />
            {breakErr && (
              <p style={{ fontSize: 11, color: C.red, marginTop: 4 }}>
                {breakErr}
              </p>
            )}
            <div
              style={{
                display: "flex",
                gap: 10,
                justifyContent: "flex-end",
                marginTop: 18,
              }}
            >
              <button
                onClick={() => {
                  setShowBreakModal(false);
                  setBreakReason("");
                  setBreakErr("");
                }}
                style={{
                  padding: "8px 18px",
                  borderRadius: 7,
                  border: `1px solid ${C.border}`,
                  background: C.surface,
                  color: C.textSub,
                  fontWeight: 600,
                  fontSize: 13,
                  cursor: "pointer",
                  fontFamily: "inherit",
                }}
              >
                {T.cancel || "Cancel"}
              </button>
              <button
                onClick={handleResume}
                style={{
                  padding: "8px 22px",
                  borderRadius: 7,
                  border: "none",
                  background: C.brand,
                  color: "#fff",
                  fontWeight: 700,
                  fontSize: 13,
                  cursor: "pointer",
                  fontFamily: "inherit",
                }}
              >
                {T.resumeWork || "Resume Work"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Break-type picker modal (short break vs lunch) */}
      {breakTypePicker && (
        <div
          onClick={() => setBreakTypePicker(false)}
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 9999,
            background: "rgba(0,0,0,0.45)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              background: C.surface,
              borderRadius: 14,
              padding: 28,
              width: 420,
              boxShadow: "0 24px 60px rgba(0,0,0,0.2)",
              border: `1px solid ${C.border}`,
            }}
          >
            <p
              style={{
                fontSize: 15,
                fontWeight: 700,
                color: C.text,
                marginBottom: 6,
              }}
            >
              {T.startBreakTitle || "Start a Break"}
            </p>
            <p style={{ fontSize: 12, color: C.textSub, marginBottom: 18 }}>
              {(
                T.startBreakHint ||
                "Pick the break type so we can warn you if it runs over ({short}m for a break, {lunch}m for lunch)."
              )
                .replace("{short}", thresholds.short_minutes)
                .replace("{lunch}", thresholds.lunch_minutes)}
            </p>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: lunchAllowed ? "1fr 1fr" : "1fr",
                gap: 10,
                marginBottom: 16,
              }}
            >
              <button
                onClick={() => {
                  setBreakTypePicker(false);
                  handleStartBreak("break");
                }}
                style={{
                  padding: "14px 12px",
                  borderRadius: 10,
                  border: `1.5px solid ${C.amberBdr}`,
                  background: C.amberBg,
                  color: C.amber,
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
                <Svg d={ICONS.coffee} s={20} c={C.amber} />
                {T.shortBreak || "Short Break"}
                <span
                  style={{ fontSize: 10, fontWeight: 600, color: C.textSub }}
                >
                  {thresholds.short_minutes}m limit
                </span>
              </button>
              {lunchAllowed && (
                <button
                  onClick={() => {
                    setBreakTypePicker(false);
                    handleStartBreak("lunch");
                  }}
                  style={{
                    padding: "14px 12px",
                    borderRadius: 10,
                    border: `1.5px solid ${C.brand}`,
                    background: C.brandBg,
                    color: C.brand,
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
                  <span style={{ fontSize: 20 }}>🍱</span>
                  {T.lunchBreak || "Lunch"}
                  <span
                    style={{ fontSize: 10, fontWeight: 600, color: C.textSub }}
                  >
                    {thresholds.lunch_minutes}m limit
                  </span>
                </button>
              )}
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end" }}>
              <button
                onClick={() => setBreakTypePicker(false)}
                style={{
                  padding: "8px 18px",
                  borderRadius: 7,
                  border: `1px solid ${C.border}`,
                  background: C.surface,
                  color: C.textSub,
                  fontWeight: 600,
                  fontSize: 13,
                  cursor: "pointer",
                  fontFamily: "inherit",
                }}
              >
                {T.cancel || "Cancel"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Overtime alert popup — fires once when break exceeds threshold */}
      {overtimeAlert && (
        <div
          onClick={() => setOvertimeAlert(null)}
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 10000,
            background: "rgba(0,0,0,0.55)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              background: C.surface,
              borderRadius: 14,
              padding: 28,
              width: 440,
              boxShadow: "0 24px 60px rgba(0,0,0,0.3)",
              border: `2px solid ${C.red}`,
            }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 12,
                marginBottom: 12,
              }}
            >
              <div
                style={{
                  width: 44,
                  height: 44,
                  borderRadius: "50%",
                  background: C.redBg,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                }}
              >
                <Svg d={ICONS.alert} s={22} c={C.red} fw="2.5" />
              </div>
              <div>
                <p
                  style={{
                    fontSize: 16,
                    fontWeight: 800,
                    color: C.red,
                    margin: 0,
                  }}
                >
                  {overtimeAlert.break_type === "lunch"
                    ? (
                        T.lunchOvertimeTitle || "Lunch over {n} minutes"
                      ).replace("{n}", overtimeAlert.threshold_min)
                    : (
                        T.breakOvertimeTitle || "Break over {n} minutes"
                      ).replace("{n}", overtimeAlert.threshold_min)}
                </p>
                <p
                  style={{ fontSize: 12, color: C.textSub, margin: "2px 0 0" }}
                >
                  {T.breakOvertimeSub || "Time to resume work."}
                </p>
              </div>
            </div>
            <p
              style={{
                fontSize: 13,
                color: C.text,
                lineHeight: 1.5,
                marginBottom: 18,
              }}
            >
              {overtimeAlert.break_type === "lunch"
                ? (
                    T.lunchOvertimeBody ||
                    "You've been on lunch for over {n} minutes. Resume work now or your admin will be notified."
                  ).replace("{n}", overtimeAlert.threshold_min)
                : (
                    T.breakOvertimeBody ||
                    "You've been on break for over {n} minutes. Resume work now or your admin will be notified."
                  ).replace("{n}", overtimeAlert.threshold_min)}
            </p>
            <div
              style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}
            >
              <button
                onClick={() => setOvertimeAlert(null)}
                style={{
                  padding: "8px 18px",
                  borderRadius: 7,
                  border: `1px solid ${C.border}`,
                  background: C.surface,
                  color: C.textSub,
                  fontWeight: 600,
                  fontSize: 13,
                  cursor: "pointer",
                  fontFamily: "inherit",
                }}
              >
                {T.dismiss || "Dismiss"}
              </button>
              <button
                onClick={() => {
                  setOvertimeAlert(null);
                  setBreakReason("");
                  setBreakErr("");
                  setShowBreakModal(true);
                }}
                style={{
                  padding: "8px 22px",
                  borderRadius: 7,
                  border: "none",
                  background: C.brand,
                  color: "#fff",
                  fontWeight: 700,
                  fontSize: 13,
                  cursor: "pointer",
                  fontFamily: "inherit",
                }}
              >
                {T.resumeWork || "Resume Work"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Persistent inline overtime banner — visible while still on break */}
      {status === "on_break" && overtimeFiredRef.current && !overtimeAlert && (
        <div
          style={{
            background: C.redBg,
            border: `1px solid ${C.redBdr}`,
            borderRadius: 8,
            padding: "10px 14px",
            display: "flex",
            alignItems: "center",
            gap: 8,
          }}
        >
          <Svg d={ICONS.alert} s={14} c={C.red} />
          <span
            style={{ fontSize: 12, color: C.red, fontWeight: 600, flex: 1 }}
          >
            {breakType === "lunch"
              ? T.lunchOvertimeBanner ||
                "Your Lunch Break allowance has ended. Please resume work."
              : T.breakOvertimeBanner ||
                "Your Short Break allowance has ended. Please resume work."}
          </span>
          <button
            onClick={() => {
              setBreakReason("");
              setBreakErr("");
              setShowBreakModal(true);
            }}
            style={{
              padding: "5px 14px",
              borderRadius: 6,
              border: "none",
              background: C.red,
              color: "#fff",
              fontSize: 11,
              fontWeight: 700,
              cursor: "pointer",
              fontFamily: "inherit",
            }}
          >
            {T.resumeNow || "Resume Now"}
          </button>
        </div>
      )}

      {/* Error banner */}
      {error && (
        <div
          style={{
            background: C.redBg,
            border: `1px solid ${C.redBdr}`,
            borderRadius: 8,
            padding: "10px 14px",
            display: "flex",
            alignItems: "center",
            gap: 8,
          }}
        >
          <Svg d={ICONS.alert} s={14} c={C.red} />
          <span style={{ fontSize: 12, color: C.red, fontWeight: 600 }}>
            {error}
          </span>
        </div>
      )}

      {/* ── NOT CLOCKED IN ── */}
      {status === "not_clocked_in" && (
        <div style={{ ...card(), textAlign: "center", padding: "48px 40px" }}>
          <div
            style={{
              width: 64,
              height: 64,
              borderRadius: "50%",
              background: C.brandBg,
              border: `1px solid ${C.border}`,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              margin: "0 auto 18px",
              fontSize: 24,
              fontWeight: 800,
              color: C.brand,
            }}
          >
            {initials(userName)}
          </div>
          <p
            style={{
              fontSize: 20,
              fontWeight: 400,
              color: C.text,
              marginBottom: 4,
            }}
          >
            {greeting}, <strong style={{ fontWeight: 700 }}>{userName}</strong>
          </p>
          <p style={{ fontSize: 13, color: C.textSub, marginBottom: 6 }}>
            {timeStr}
          </p>
          <p style={{ fontSize: 13, color: C.textSub, marginBottom: 32 }}>
            {T.notClockedInYet || "You are not clocked in yet"}
          </p>
          <button
            onClick={handleClockIn}
            disabled={clockingIn}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 10,
              padding: "14px 40px",
              borderRadius: 10,
              border: "none",
              background: clockingIn ? "#9CA3AF" : C.green,
              color: "#fff",
              fontWeight: 700,
              fontSize: 15,
              cursor: clockingIn ? "wait" : "pointer",
              fontFamily: "inherit",
              boxShadow: clockingIn
                ? "none"
                : "0 4px 20px rgba(68,185,123,0.4)",
              transition: "all .2s",
            }}
          >
            <Svg d={ICONS.play} s={15} c="#fff" fw="2.5" />
            {clockingIn
              ? T.connecting || "Connecting..."
              : T.clockInBtn || "Clock In"}
          </button>
        </div>
      )}

      {/* ── WORKING / ON BREAK ── */}
      {(status === "working" || status === "on_break") && (
        <>
          {/* Timer card */}
          <div
            style={{
              ...card(),
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 16,
              flexWrap: "wrap",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
              <div style={{ position: "relative" }}>
                <div
                  style={{
                    width: 52,
                    height: 52,
                    borderRadius: "50%",
                    background: C.brandBg,
                    border: `2px solid ${C.border}`,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    fontSize: 18,
                    fontWeight: 800,
                    color: C.brand,
                  }}
                >
                  {initials(userName)}
                </div>
                <div
                  style={{
                    position: "absolute",
                    bottom: 1,
                    right: 1,
                    width: 13,
                    height: 13,
                    borderRadius: "50%",
                    background: status === "on_break" ? C.amber : C.green,
                    border: "2.5px solid #fff",
                  }}
                />
              </div>
              <div>
                <p
                  style={{
                    fontWeight: 600,
                    fontSize: 14,
                    color: C.text,
                    marginBottom: 2,
                  }}
                >
                  {userName}
                </p>
                <p
                  style={{
                    fontSize: 28,
                    fontWeight: 800,
                    color: C.text,
                    fontVariantNumeric: "tabular-nums",
                    letterSpacing: 2,
                    lineHeight: 1,
                  }}
                >
                  {fmtClock(dailyWorkedSecs)}
                </p>
                {status === "on_break" && (
                  <p
                    style={{
                      fontSize: 11,
                      color: C.amber,
                      marginTop: 4,
                      fontWeight: 600,
                    }}
                  >
                    {T.breakLabel || "Break"}: {fmtClock(breakElapsed)}
                  </p>
                )}
              </div>
            </div>

            <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
              {status === "working" ? (
                <button
                  onClick={() => setBreakTypePicker(true)}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 7,
                    padding: "10px 20px",
                    borderRadius: 8,
                    background: C.amberBg,
                    color: C.amber,
                    fontWeight: 700,
                    fontSize: 13,
                    cursor: "pointer",
                    fontFamily: "inherit",
                    border: `1px solid ${C.amberBdr}`,
                  }}
                >
                  <Svg d={ICONS.coffee} s={14} c={C.amber} />{" "}
                  {T.takeABreak || "Take a Break"}
                </button>
              ) : (
                <button
                  onClick={() => {
                    setBreakReason("");
                    setBreakErr("");
                    setShowBreakModal(true);
                  }}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 7,
                    padding: "10px 20px",
                    borderRadius: 8,
                    border: `1px solid ${C.brand}`,
                    background: C.brandBg,
                    color: C.brand,
                    fontWeight: 700,
                    fontSize: 13,
                    cursor: "pointer",
                    fontFamily: "inherit",
                  }}
                >
                  <Svg d={ICONS.play} s={14} c={C.brand} />{" "}
                  {T.resumeWork || "Resume Work"}
                </button>
              )}
              <button
                onClick={handleClockOut}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 7,
                  padding: "10px 20px",
                  borderRadius: 8,
                  border: `1px solid ${C.redBdr}`,
                  background: C.redBg,
                  color: C.red,
                  fontWeight: 700,
                  fontSize: 13,
                  cursor: "pointer",
                  fontFamily: "inherit",
                }}
              >
                <Svg d={ICONS.logout} s={14} c={C.red} />{" "}
                {T.clockOut || "Clock Out"}
              </button>
            </div>
          </div>

          {/* Status + session info */}
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "1fr 1fr 1fr",
              gap: 12,
            }}
          >
            {[
              {
                label: T.status || "Status",
                val: statusChip,
                color: statusColor,
                bg: statusBg,
              },
              {
                label: T.workTime || "Work Time",
                val: fmtClock(dailyWorkedSecs),
                color: C.text,
              },
              {
                label: T.breakTime || "Break Time",
                val: fmtClock(breakElapsed),
                color: breakExceeded ? C.red : C.amber,
              },
            ].map((s) => (
              <div key={s.label} style={{ ...card({ padding: "14px 18px" }) }}>
                <p
                  style={{
                    fontSize: 11,
                    color: C.textSub,
                    fontWeight: 600,
                    textTransform: "uppercase",
                    letterSpacing: 0.5,
                    marginBottom: 6,
                  }}
                >
                  {s.label}
                </p>
                <p style={{ fontSize: 18, fontWeight: 800, color: s.color }}>
                  {s.val}
                </p>
              </div>
            ))}
          </div>

          {/* Break governance breakdown — Elapsed / Allowed / Exceeded (per spec) */}
          {status === "on_break" && (
            <div
              style={{
                ...card({
                  padding: "14px 18px",
                  borderColor: breakExceeded ? C.redBdr : C.amberBdr,
                  background: breakExceeded ? C.redBg : C.amberBg,
                }),
              }}
            >
              <p
                style={{
                  fontSize: 12,
                  fontWeight: 800,
                  color: breakExceeded ? C.red : C.amber,
                  marginBottom: 10,
                }}
              >
                {breakLabelTxt}
                {breakExceeded ? " — Allowance Exceeded" : ""}
              </p>
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "1fr 1fr 1fr",
                  gap: 12,
                }}
              >
                {[
                  {
                    label: T.elapsed || "Elapsed",
                    val: fmtMins(breakElapsed),
                    color: C.text,
                  },
                  {
                    label: T.allowed || "Allowed",
                    val: `${breakAllowedMin}m`,
                    color: C.textSub,
                  },
                  {
                    label: T.exceeded || "Exceeded",
                    val: fmtMins(exceededSecs),
                    color: breakExceeded ? C.red : C.textSub,
                  },
                ].map((s) => (
                  <div key={s.label}>
                    <p
                      style={{
                        fontSize: 10,
                        color: C.textSub,
                        fontWeight: 600,
                        textTransform: "uppercase",
                        letterSpacing: 0.5,
                        marginBottom: 4,
                      }}
                    >
                      {s.label}
                    </p>
                    <p
                      style={{
                        fontSize: 18,
                        fontWeight: 800,
                        color: s.color,
                        fontVariantNumeric: "tabular-nums",
                      }}
                    >
                      {s.val}
                    </p>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Clock-in time */}
          {clockInTime && (
            <div
              style={{
                ...card({ padding: "12px 18px" }),
                display: "flex",
                alignItems: "center",
                gap: 8,
              }}
            >
              <Svg d={ICONS.clock} s={14} c={C.textSub} />
              <span style={{ fontSize: 12, color: C.textSub }}>
                {T.clockedInAt || "Clocked in at"}
              </span>
              <span style={{ fontSize: 13, fontWeight: 700, color: C.text }}>
                {fmtTime(clockInTime)}
              </span>
            </div>
          )}
        </>
      )}

      {/* ── CLOCKED OUT ── */}
      {status === "clocked_out" && (
        <>
          <div style={{ ...card(), textAlign: "center", padding: "48px 40px" }}>
            <div
              style={{
                width: 56,
                height: 56,
                borderRadius: "50%",
                background: C.greenBg,
                border: `1px solid ${C.greenBdr}`,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                margin: "0 auto 18px",
              }}
            >
              <Svg d={ICONS.check} s={24} c={C.green} fw="2.5" />
            </div>
            <p
              style={{
                fontSize: 20,
                fontWeight: 700,
                color: C.text,
                marginBottom: 6,
              }}
            >
              {T.workSessionEnded || "Work session ended"}
            </p>
            <p style={{ fontSize: 14, color: C.textSub, marginBottom: 6 }}>
              {T.youWorkedFor || "You worked for"}{" "}
              <strong style={{ color: C.green }}>{fmtHM(workedSecs)}</strong>{" "}
              {T.todayPeriod || "today"}
            </p>
            {clockOutTime && (
              <p style={{ fontSize: 12, color: C.textSub, marginBottom: 32 }}>
                {T.clockedOutAt || "Clocked out at"} {fmtTime(clockOutTime)}
              </p>
            )}
            <button
              onClick={handleClockIn}
              disabled={clockingIn}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 10,
                padding: "12px 32px",
                borderRadius: 10,
                border: "none",
                background: C.brand,
                color: "#fff",
                fontWeight: 700,
                fontSize: 14,
                cursor: clockingIn ? "wait" : "pointer",
                fontFamily: "inherit",
                boxShadow: "0 4px 16px rgba(17,73,106,0.35)",
              }}
            >
              <Svg d={ICONS.play} s={14} c="#fff" fw="2.5" />
              {clockingIn
                ? T.connecting || "Connecting..."
                : T.clockInAgain || "Clock In Again"}
            </button>
          </div>

          <div
            style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}
          >
            {[
              {
                label: T.totalWork || "Total Work Time",
                val: fmtHM(workedSecs),
                icon: ICONS.clock,
                color: C.green,
                bg: C.greenBg,
              },
              {
                label: T.clockInBtn || "Clock In",
                val: fmtTime(clockInTime),
                icon: ICONS.play,
                color: C.brand,
                bg: C.brandBg,
              },
            ].map((s) => (
              <div
                key={s.label}
                style={{
                  ...card({ padding: "16px 20px" }),
                  display: "flex",
                  alignItems: "center",
                  gap: 12,
                }}
              >
                <div
                  style={{
                    width: 36,
                    height: 36,
                    borderRadius: 9,
                    background: s.bg,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    flexShrink: 0,
                  }}
                >
                  <Svg d={s.icon} s={16} c={s.color} />
                </div>
                <div>
                  <p
                    style={{ fontSize: 11, color: C.textSub, fontWeight: 500 }}
                  >
                    {s.label}
                  </p>
                  <p style={{ fontSize: 16, fontWeight: 800, color: C.text }}>
                    {s.val}
                  </p>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

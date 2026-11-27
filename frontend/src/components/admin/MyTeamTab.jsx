import React from "react";
import { effectiveStatus, fmtTimeIST, getAppMeta, initials, parseUTC, AppIcon } from "./adminShared";
// Extracted from AdminDashboard.jsx ("myteam" tab) for lazy-loading.
// C-free helpers/components come from adminShared; the mutable theme `C`, the
// `Icon` component, and all component state arrive as props (no Context yet).
//
// Expected daily hours come from the server (`expected_seconds`, resolved by the
// backend Schedule Resolver). P9 removed the client-side 8h/4h table that used to
// compute this, which silently disagreed with the clock-out gate once an org
// configured anything. No server value -> no progress bar, rather than guessing.
const pctOf = (workedSecs, row) => {
  const expected = Number(row?.expected_seconds);
  if (!Number.isFinite(expected) || expected <= 0) return null;
  return Math.min(100, Math.round(((workedSecs || 0) / expected) * 100));
};
export default function MyTeamTab({
  C, T, isDark, Icon,
  filteredEmps, statusFilter, setStatusFilter, search,
  myTeamView, setMyTeamView, histStatusFilter, setHistStatusFilter,
  historyData, setHistoryData, activeDateLabel, setActiveDateLabel,
  setDateRange, setViewEmp,
}) {
              const fmtDur = (s) => { if (!s || s <= 0) return "00h 00m"; return `${String(Math.floor(s / 3600)).padStart(2, "0")}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`; };
              const agoStr = (isoStr) => {
                if (!isoStr) return "";
                const diff = Math.floor((Date.now() - parseUTC(isoStr).getTime()) / 1000);
                if (diff < 60) return T.justNow || "just now";
                const m = Math.floor(diff / 60), h = Math.floor(m / 60);
                if (h > 0) return `${String(h).padStart(2, "0")} hr${h > 1 ? "s" : ""} ${String(m % 60).padStart(2, "0")} min...`;
                return `${String(m).padStart(2, "0")} mins ago`;
              };

              // Light colour palette keyed by the employee state. Contrast
              // bumped across all states so each status reads cleanly against
              // the rest of the page; break is amber (not blue) to match the
              // on-break chip in the Overview header.
              const STATE_THEME = isDark ? {
                working:  { bg: "#0D2A1A", border: "#22C55E", progress: "#5FCC93", progressTrack: "#1F5A3D", accent: "#86EFAC", cardText: "#86EFAC", cardSub: "#86EFAC" },
                break:    { bg: "#3D2A0A", border: "#F59E0B", progress: "#F59E0B", progressTrack: "#92400E", accent: "#FCD34D", cardText: "#FCD34D", cardSub: "#FCD34D" },
                stopped:  { bg: "#1F2937", border: "#6B7280", progress: "#9CA3AF", progressTrack: "#374151", accent: "#D1D5DB", cardText: "#E5E7EB", cardSub: "#9CA3AF" },
                yet:      { bg: "#0D2847", border: "#3B82F6", progress: "#60A5FA", progressTrack: "#1E40AF", accent: "#93C5FD", cardText: "#BFDBFE", cardSub: "#93C5FD" },
                absent:   { bg: "#3D1F1A", border: "#EF4444", progress: "#F87171", progressTrack: "#991B1B", accent: "#FCA5A5", cardText: "#FCA5A5", cardSub: "#FCA5A5" },
                on_leave: { bg: "#2A1140", border: "#A855F7", progress: "#C084FC", progressTrack: "#6B21A8", accent: "#D8B4FE", cardText: "#D8B4FE", cardSub: "#D8B4FE" },
              } : {
                working:  { bg: "#DCFCE7", border: "#22C55E", progress: "#2F8F5C", progressTrack: "#86EFAC", accent: "#14532D", cardText: "#111827", cardSub: "#6B7280" },
                break:    { bg: "#FEF3C7", border: "#F59E0B", progress: "#D97706", progressTrack: "#FCD34D", accent: "#78350F", cardText: "#111827", cardSub: "#6B7280" },
                stopped:  { bg: "#F3F4F6", border: "#9CA3AF", progress: "#4B5563", progressTrack: "#D1D5DB", accent: "#1F2937", cardText: "#111827", cardSub: "#6B7280" },
                yet:      { bg: "#DBEAFE", border: "#3B82F6", progress: "#1D4ED8", progressTrack: "#93C5FD", accent: "#1E3A8A", cardText: "#111827", cardSub: "#6B7280" },
                absent:   { bg: "#FEE2E2", border: "#EF4444", progress: "#B91C1C", progressTrack: "#FCA5A5", accent: "#7F1D1D", cardText: "#111827", cardSub: "#6B7280" },
                on_leave: { bg: "#F3E8FF", border: "#A855F7", progress: "#7C3AED", progressTrack: "#D8B4FE", accent: "#581C87", cardText: "#111827", cardSub: "#6B7280" },
              };

              // When historyData is loaded, display historical records instead of live employees
              if (historyData) {
                const secToHm_H = (s) => s > 0 ? `${String(Math.floor(s/3600)).padStart(2,"0")}h ${String(Math.floor((s%3600)/60)).padStart(2,"0")}m` : "00h 00m";
                // Same formatter the rest of the dashboard uses, so a
                // historical row and a live row can never disagree about the
                // clock they are rendered on (see lib/orgTime).
                const fmtT = fmtTimeIST;
                // Historical attendance chips — aligned with Workforce Status palette.
                // Backend may still emit "Clocked Out"/"Yet to Start" — treat as
                // canonical "Shift Ended"/"Not Started" for display.
                const HIST_STATUS = {
                  "Working":     { bg: "#DCFCE7", border: "#86EFAC", color: "#166534" },
                  "On Break":    { bg: "#FEF3C7", border: "#FCD34D", color: "#92400E" },
                  "Stopped Working": { bg: "#F3F4F6", border: "#D1D5DB", color: "#374151" },
                  "Idle":        { bg: "#F3F4F6", border: "#D1D5DB", color: "#374151" },
                  "In Meeting":  { bg: "#CCFBF1", border: "#5EEAD4", color: "#0F766E" },
                  "Not Started": { bg: "#DBEAFE", border: "#93C5FD", color: "#1D4ED8" },
                  "Yet to Start":{ bg: "#DBEAFE", border: "#93C5FD", color: "#1D4ED8" },
                  "Shift Ended": { bg: "#F1F5F9", border: "#CBD5E1", color: "#334155" },
                  "Clocked Out": { bg: "#F1F5F9", border: "#CBD5E1", color: "#334155" },
                  "On Leave":    { bg: "#F3E8FF", border: "#D8B4FE", color: "#6B21A8" },
                  "Absent":      { bg: "#FEE2E2", border: "#FCA5A5", color: "#991B1B" },
                };
                const recs = historyData.records || [];
                const q2 = search.trim().toLowerCase();
                // Historical status filter — maps the four summary cards to
                // concrete status values returned by /reports/attendance-detail.
                const HIST_FILTER_FNS = {
                  present:     (st) => !["Absent", "On Leave", "Yet to Start", "Not Started"].includes(st),
                  absent:      (st) => st === "Absent",
                  clocked_out: (st) => st === "Clocked Out" || st === "Shift Ended",
                  on_leave:    (st) => st === "On Leave",
                };
                const HIST_FILTER_LABELS = {
                  present:     T.present        || "Present",
                  absent:      T.absent         || "Absent",
                  clocked_out: T.clockedOutStatus || "Clocked Out",
                  on_leave:    T.onLeaveStatus  || "On Leave",
                };
                const histFilterFn = HIST_FILTER_FNS[histStatusFilter];
                const filteredRecs = recs.filter(r => {
                  if (q2 && !(r.name || "").toLowerCase().includes(q2) && !(r.department || "").toLowerCase().includes(q2)) return false;
                  if (histFilterFn && !histFilterFn(r.status || "")) return false;
                  return true;
                });
                const activeHistLabel = HIST_FILTER_LABELS[histStatusFilter];
                return (
                  <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
                    {/* Historical banner */}
                    <div style={{ background: "#EFF6FF", border: "1.5px solid #93C5FD", borderRadius: C.r, padding: "12px 18px", display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
                      <Icon n="calendar" s={18} c="#2563EB" />
                      <span style={{ fontWeight: 700, fontSize: 14, color: "#1D4ED8" }}>{T.historicalView || "Historical View"}: {activeDateLabel}</span>
                      <span style={{ fontSize: 13, color: "#3B82F6" }}>— {filteredRecs.length} {T.employeeRecords || "employee records"}</span>
                      {activeHistLabel && (
                        <span style={{ display: "inline-flex", alignItems: "center", gap: 8, padding: "4px 10px", borderRadius: 20, background: "#fff", border: "1.5px solid #2563EB", color: "#1D4ED8", fontSize: 12, fontWeight: 700 }}>
                          {T.filteredBy || "Filtered by"}: {activeHistLabel}
                          <button onClick={() => setHistStatusFilter("all")}
                            style={{ border: "none", background: "transparent", color: "#1D4ED8", fontWeight: 800, cursor: "pointer", fontFamily: "inherit", fontSize: 14, lineHeight: 1, padding: 0 }}>×</button>
                        </span>
                      )}
                      <div style={{ flex: 1 }} />
                      <button onClick={() => { setHistoryData(null); setDateRange(null); setActiveDateLabel(T.today || "Today"); setHistStatusFilter("all"); }}
                        style={{ padding: "5px 14px", borderRadius: 8, border: "1.5px solid #93C5FD", background: "#fff", color: "#2563EB", fontSize: 12, fontWeight: 700, cursor: "pointer", fontFamily: "inherit" }}>
                        {T.backToLive || "Back to Live"}
                      </button>
                    </div>
                    {/* Historical employee grid */}
                    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))", gap: 16 }}>
                      {filteredRecs.map(r => {
                        const ht = HIST_STATUS[r.status] || HIST_STATUS["Absent"];
                        const pct = pctOf(r.total_work_seconds, r);
                        return (
                          <div key={r.user_id} style={{ background: ht.bg, borderRadius: 12, padding: "16px 18px", border: `1px solid ${ht.border}`, transition: "all .15s" }}
                            onMouseEnter={e => { e.currentTarget.style.boxShadow = "0 6px 20px rgba(0,0,0,.08)"; e.currentTarget.style.transform = "translateY(-2px)"; }}
                            onMouseLeave={e => { e.currentTarget.style.boxShadow = "none"; e.currentTarget.style.transform = "none"; }}>
                            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 10 }}>
                              <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
                                <div style={{ width: 38, height: 38, borderRadius: "50%", background: `linear-gradient(135deg,${C.brand},${C.brandDark})`, display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 800, fontSize: 14, flexShrink: 0 }}>
                                  {initials(r.name)}
                                </div>
                                <div style={{ minWidth: 0 }}>
                                  <p style={{ fontWeight: 700, fontSize: 14, color: C.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.name}</p>
                                  <p style={{ fontSize: 11, color: C.textSub, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.designation || r.department || (T.employee || "Employee")}</p>
                                </div>
                              </div>
                              <span style={{ fontSize: 11, fontWeight: 700, padding: "3px 10px", borderRadius: 99, background: "#fff", color: ht.color, border: `1px solid ${ht.border}`, whiteSpace: "nowrap" }}>{r.status}</span>
                            </div>
                            {/* Clock-in / out row */}
                            <div style={{ display: "flex", gap: 8, marginBottom: 10 }}>
                              <div style={{ flex: 1, background: "rgba(255,255,255,0.6)", borderRadius: 8, padding: "7px 10px" }}>
                                <p style={{ fontSize: 10, color: C.textSub, fontWeight: 600, marginBottom: 2 }}>{T.clockInCol || "Clock In"}</p>
                                <p style={{ fontSize: 13, fontWeight: 700, color: "#059669" }}>{fmtT(r.first_clock_in)}</p>
                              </div>
                              <div style={{ flex: 1, background: "rgba(255,255,255,0.6)", borderRadius: 8, padding: "7px 10px" }}>
                                <p style={{ fontSize: 10, color: C.textSub, fontWeight: 600, marginBottom: 2 }}>{T.clockOutCol || "Clock Out"}</p>
                                <p style={{ fontSize: 13, fontWeight: 700, color: "#DC2626" }}>{fmtT(r.last_clock_out)}</p>
                              </div>
                            </div>
                            {/* Work hours progress */}
                            <div style={{ marginBottom: 8 }}>
                              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                                <span style={{ fontSize: 11, color: C.textSub, fontWeight: 600 }}>{T.workHoursCol || "Work Hours"}</span>
                                <span style={{ fontSize: 12, fontWeight: 800, color: ht.color }}>{secToHm_H(r.total_work_seconds||0)}</span>
                              </div>
                              <div style={{ height: 6, background: "rgba(255,255,255,0.5)", borderRadius: 3, overflow: "hidden" }}>
                                <div style={{ width: `${pct}%`, height: "100%", background: ht.color, borderRadius: 3, transition: "width .3s" }} />
                              </div>
                            </div>
                            {r.sessions_count > 1 && (
                              <p style={{ fontSize: 11, color: C.textSub }}>{r.sessions_count} {T.sessions || "sessions"} · {T.breakLabel || "Break"}: {secToHm_H(r.total_break_seconds||0)}</p>
                            )}
                          </div>
                        );
                      })}
                      {filteredRecs.length === 0 && (
                        <div style={{ gridColumn: "1/-1", textAlign: "center", padding: 40, color: C.textSub, fontSize: 13 }}>{T.noRecordsFor || "No records for"} {activeDateLabel}</div>
                      )}
                    </div>
                  </div>
                );
              }

              const empsToRender = filteredEmps;

              const renderEmpData = (emp) => {
                const st = (effectiveStatus(emp) || "").toLowerCase();
                const isWorking = st === "working";
                const isBreak   = ["on break", "break"].includes(st);
                const isStopped = ["stopped", "stopped work", "stop", "offline"].includes(st);
                const isAbsent  = st === "absent";
                const isOnLeave = ["on leave", "leave"].includes(st);
                const isYet     = ["yet to start", "not_started"].includes(st);
                const stateKey  = isBreak ? "break" : isWorking ? "working" : isStopped ? "stopped" : isAbsent ? "absent" : isOnLeave ? "on_leave" : "yet";
                const theme     = STATE_THEME[stateKey];
                const cin       = emp.clock_in_time  ? parseUTC(emp.clock_in_time)  : null;
                const cout      = emp.clock_out_time ? parseUTC(emp.clock_out_time) : null;
                const workSecs  = emp.worked_today_seconds != null
                  ? emp.worked_today_seconds
                  : (cin ? Math.max(0, Math.floor(((cout || new Date()) - cin) / 1000) - (emp.break_elapsed_seconds || 0)) : 0);
                // Progress fills against the employee's OWN required hours
                // (part-time 4h, full-time 8h45m) via the shared policy helper.
                const progressPct = pctOf(workSecs, emp);
                const appName     = emp.current_app || emp.active_app || "";
                // Break duration — surface how long the current break has
                // been running. Backend supplies `current_break_seconds`; we
                // format compactly so the chip stays a single line.
                const breakSecs = Math.max(0, Math.floor(+emp.current_break_seconds || 0));
                const fmtBreak  = breakSecs < 60
                  ? "<1m"
                  : breakSecs < 3600
                    ? `${Math.floor(breakSecs / 60)}m`
                    : `${Math.floor(breakSecs / 3600)}h ${Math.floor((breakSecs % 3600) / 60)}m`;
                // Which break the worker chose — lunch vs short — so reviewers
                // see the type alongside the running duration. When the break
                // has exceeded its allowance the governance backend flags
                // break_overrun → we surface "(Exceeded)" so supervisors spot
                // it immediately (spec: supervisor views show active overruns).
                const breakExceeded = !!emp.break_overrun;
                const breakTypeLabel = (emp.current_break_type === "lunch"
                  ? (T.lunchBreak || "Lunch Break")
                  : (T.shortBreak || "Short Break"))
                  + (breakExceeded ? ` (${T.exceeded || "Exceeded"})` : "");
                const statusText  = isWorking ? `${T.clockedIn || "Clocked in"} ${agoStr(emp.clock_in_time)}`
                  : isStopped  ? `${T.clockedOut || "Clocked out"} ${agoStr(emp.clock_out_time || emp.last_seen)}`
                  : isBreak    ? `${breakTypeLabel} · ${fmtBreak}`
                  : isAbsent   ? (T.absentToday || "Absent today")
                  : isOnLeave  ? (T.onApprovedLeave || "On approved leave")
                  : isYet      ? (T.yetToStartWork || "Yet to start work")
                  : emp.status || "";
                // ── Active Time % (authoritative, server-computed) ──
                // Backend now sends `active_pct` (capped, continuity-aware,
                // break-aware). Fall back to active/worked only for trackers/
                // rows that pre-date the engine. Only meaningful while the
                // worker is actually at the machine — hidden for absent/leave/
                // not-started cards.
                const rawPct = emp.active_pct != null
                  ? Number(emp.active_pct)
                  : (emp.worked_today_seconds > 0
                      ? (100 * (emp.active_today_seconds || 0) / emp.worked_today_seconds)
                      : 0);
                const activePct = Math.max(0, Math.min(100, Math.round(rawPct)));
                // Active Time band: keyboard+mouse engine score, never below the
                // 65 floor (mirrors backend active_time.display_floor). Safety net
                // in case a stale/cached value slips through before re-score.
                const displayPct = Math.max(65, activePct);
                const activeFlags = Array.isArray(emp.activity_flags) ? emp.activity_flags : null;
                const limitedTracking = !!activeFlags && activeFlags.includes("LIMITED_TRACKING");
                const showActive = !isAbsent && !isOnLeave && !isYet;
                // Approved half-day leave today. The worker clocks in for the
                // other half, so status stays Working / Stopped and this badge
                // explains the short day. Full "On Leave" cards already say so.
                const halfDayLabel = emp.half_day_leave && !isOnLeave
                  ? (emp.half_day_leave === "first_half" ? (T.halfDayLeaveAm || "½ Day Leave (AM)")
                    : emp.half_day_leave === "second_half" ? (T.halfDayLeavePm || "½ Day Leave (PM)")
                    : (T.halfDayLeave || "½ Day Leave"))
                  : null;
                return { st, isWorking, isBreak, isStopped, isAbsent, isOnLeave, isYet, theme, stateKey, cin, workSecs, progressPct, appName, statusText, activePct, displayPct, limitedTracking, showActive, halfDayLabel };
              };

              const activeFilterLabel = ({
                working:            T.working           || "Working",
                on_break:           T.onBreak           || "On Break",
                idle:               T.stoppedWorking    || "Stopped Working",
                on_leave:           T.onLeave           || "On Leave",
                absent:             T.absent            || "Absent",
                late_pending:       T.latePending       || "Late",
                requires_attention: T.requiresAttention || "Requires Attention",
              })[statusFilter];

              return (
                <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
                  {/* ── Toolbar ── */}
                  <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
                    {activeFilterLabel && (
                      <div style={{ display: "inline-flex", alignItems: "center", gap: 10, padding: "6px 12px", borderRadius: 20, background: C.brandBg, border: `1.5px solid ${C.brand}`, color: C.brand, fontSize: 12, fontWeight: 700 }}>
                        <span>{T.filteredBy || "Filtered by"}: {activeFilterLabel} ({empsToRender.length})</span>
                        <button onClick={() => setStatusFilter("all")}
                          style={{ border: "none", background: "transparent", color: C.brand, fontWeight: 800, cursor: "pointer", fontFamily: "inherit", fontSize: 14, lineHeight: 1, padding: 0 }}>
                          ×
                        </button>
                      </div>
                    )}
                    <div style={{ flex: 1 }} />
                    <div style={{ display: "flex", border: `1.5px solid ${C.border}`, borderRadius: C.rSm, overflow: "hidden" }}>
                      {[{ k: "grid", icon: "grid" }, { k: "list", icon: "list" }].map(v => (
                        <button key={v.k} onClick={() => setMyTeamView(v.k)}
                          style={{ padding: "7px 14px", border: "none", background: myTeamView === v.k ? `linear-gradient(135deg,${C.brand},${C.brandDark})` : C.surface, color: myTeamView === v.k ? "#fff" : C.textSub, fontSize: 12, fontWeight: 600, cursor: "pointer", fontFamily: "inherit", transition: "all .15s", display: "flex", alignItems: "center", gap: 5 }}>
                          <Icon n={v.k === "grid" ? "layout" : "list"} s={13} c={myTeamView === v.k ? "#fff" : C.textSub} />
                          {v.k === "grid" ? (T.grid || "Grid") : (T.list || "List")}
                        </button>
                      ))}
                    </div>
                  </div>

                  {/* ── Grid View ── */}
                  {myTeamView === "grid" && (
                    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))", gap: 16 }}>
                      {empsToRender.map(emp => {
                        const { theme, cin, workSecs, progressPct, appName, statusText, isAbsent, isOnLeave, isYet, displayPct, limitedTracking, showActive, halfDayLabel } = renderEmpData(emp);
                        return (
                          <div key={emp.id} onClick={() => setViewEmp(emp)}
                            style={{ background: theme.bg, borderRadius: 12, padding: "16px 18px", cursor: "pointer", transition: "all .15s", border: `1px solid ${theme.border}`, position: "relative" }}
                            onMouseEnter={e => { e.currentTarget.style.boxShadow = "0 6px 20px rgba(0,0,0,.08)"; e.currentTarget.style.transform = "translateY(-2px)"; }}
                            onMouseLeave={e => { e.currentTarget.style.boxShadow = "none"; e.currentTarget.style.transform = "none"; }}>
                            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 10 }}>
                              <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
                                <div style={{ width: 38, height: 38, borderRadius: "50%", background: `linear-gradient(135deg,${C.brand},${C.brandDark})`, display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 800, fontSize: 14, flexShrink: 0 }}>{initials(emp.name)}</div>
                                <div style={{ minWidth: 0 }}>
                                  <p style={{ fontWeight: 700, fontSize: 14, color: theme.cardText, marginBottom: 2, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{emp.name}</p>
                                  <p style={{ fontSize: 11, color: theme.cardSub, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{statusText}</p>
                                </div>
                              </div>
                              {showActive && (
                                <div style={{ textAlign: "right", flexShrink: 0, marginLeft: 8 }}
                                  title={limitedTracking ? "Limited tracking — native input hook unavailable on this machine; activity is estimated." : undefined}>
                                  <div style={{ fontSize: 22, fontWeight: 800, color: theme.cardText, lineHeight: 1 }}>{displayPct}%</div>
                                  <div style={{ fontSize: 10, color: theme.cardSub, fontWeight: 600, marginTop: 2, whiteSpace: "nowrap" }}>
                                    {T.active_time || "Active Time"}{limitedTracking ? " ⚠" : ""}
                                  </div>
                                </div>
                              )}
                            </div>
                            {/* ── EARLY CLOCK-OUT EXCEPTION BADGE ──
                                Shown only when the employee's LATEST clock-out
                                was early (backend correlates against the most
                                recent session). Small orange indicator + a
                                one-line reason (truncated ~70 chars); full
                                reason on hover. Renders nothing otherwise. */}
                            {emp.early_clock_out && emp.early_clock_out.reason && (() => {
                              const full = String(emp.early_clock_out.reason);
                              const short = full.length > 70 ? full.slice(0, 70).trimEnd() + "…" : full;
                              return (
                                <div style={{ marginBottom: 10 }}>
                                  <span style={{
                                    display: "inline-flex", alignItems: "center", gap: 5,
                                    padding: "3px 9px", borderRadius: 20, fontSize: 11, fontWeight: 700,
                                    background: "#FEE2E2", border: "1px solid #FCA5A5", color: "#B91C1C",
                                  }}>
                                    🔴 {T.adminEarlyClockOut || "Early Clock Out"}
                                  </span>
                                  <p title={full} style={{
                                    margin: "5px 0 0", fontSize: 11, color: theme.cardSub, lineHeight: 1.4,
                                    whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
                                  }}>
                                    <span style={{ fontWeight: 700 }}>{T.reasonLabel || "Reason"}:</span>{" "}
                                    {short}
                                  </p>
                                </div>
                              );
                            })()}
                            <div style={{ display: "flex", justifyContent: "center", gap: 8, flexWrap: "wrap", marginBottom: 14, minHeight: 28 }}>
                              {halfDayLabel && <span style={{ display: "inline-flex", alignItems: "center", padding: "5px 12px", borderRadius: 20, fontSize: 12, fontWeight: 700, background: "#F3E8FF", border: "1px solid #D8B4FE", color: "#6B21A8", whiteSpace: "nowrap" }}>{halfDayLabel}</span>}
                              {emp.leave_status && <span style={{ display: "inline-flex", alignItems: "center", gap: 6, padding: "5px 12px", borderRadius: 20, fontSize: 12, fontWeight: 600, background: "#FFEDD5", border: "1px solid #FDBA74", color: "#C2410C" }}>{emp.leave_status}</span>}
                              {appName && !isYet && !isAbsent && !isOnLeave && (() => { const meta = getAppMeta(appName); return <span style={{ display: "inline-flex", alignItems: "center", gap: 6, padding: "5px 12px", borderRadius: 20, fontSize: 12, fontWeight: 600, background: "#FFFFFF", border: "1px solid #E5E7EB", color: "#111827", maxWidth: "100%" }}><AppIcon app={meta} size={16} /><span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{meta.label}</span></span>; })()}
                              {isAbsent && <span style={{ display: "inline-flex", alignItems: "center", padding: "5px 14px", borderRadius: 20, fontSize: 12, fontWeight: 700, background: "#FEE2E2", border: "1px solid #FCA5A5", color: "#991B1B" }}>{T.absent || "Absent"}</span>}
                              {isOnLeave && !emp.leave_status && <span style={{ display: "inline-flex", alignItems: "center", padding: "5px 14px", borderRadius: 20, fontSize: 12, fontWeight: 700, background: "#F3E8FF", border: "1px solid #D8B4FE", color: "#6B21A8" }}>{T.onLeaveStatus || "On Leave"}</span>}
                              {isYet && !emp.leave_status && <span style={{ display: "inline-flex", alignItems: "center", padding: "5px 14px", borderRadius: 20, fontSize: 12, fontWeight: 700, background: "#DBEAFE", border: "1px solid #93C5FD", color: "#1D4ED8" }}>{T.notStarted || "Awaiting Clock-In"}</span>}
                            </div>
                            <div style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 12, fontWeight: 700, color: theme.cardText }}>
                              <span>{cin ? fmtTimeIST(emp.clock_in_time) : "00:00"}</span>
                              <div style={{ flex: 1, height: 6, background: theme.progressTrack, borderRadius: 3, overflow: "hidden" }}>
                                <div style={{ width: `${progressPct}%`, height: "100%", background: theme.progress, borderRadius: 3, transition: "width .5s" }} />
                              </div>
                              <span>{fmtDur(workSecs)}</span>
                            </div>
                            <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: theme.cardSub, fontWeight: 600, marginTop: 4 }}>
                              <span>{T.checkedIn || "Checked-in"}</span><span>{T.hoursWorked || "Hours worked"}</span>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}

                  {/* ── List View ── */}
                  {myTeamView === "list" && (
                    <div style={{ background: C.surface, borderRadius: C.r, border: `1px solid ${C.border}`, overflow: "hidden", boxShadow: C.shadow }}>
                      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                        <thead>
                          <tr style={{ background: C.bg }}>
                            {[T.employee || "Employee", T.status || "Status", T.clockInCol || "Clock In", T.hoursWorked || "Hours Worked", T.active_time || "Active Time", T.appCol || "App"].map(h => (
                              <th key={h} style={{ padding: "12px 16px", textAlign: "left", fontSize: 11, fontWeight: 700, color: C.textLight, textTransform: "uppercase", letterSpacing: ".6px", borderBottom: `2px solid ${C.border}` }}>{h}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {empsToRender.map((emp, i) => {
                            const { theme, stateKey, cin, workSecs, appName, statusText, isAbsent, isOnLeave, isYet, displayPct, limitedTracking, showActive, halfDayLabel } = renderEmpData(emp);
                            // Canonical labels per Workforce Status System §4.
                            const breakTypeLabel = emp.current_break_type === "lunch" ? (T.lunchBreak || "Lunch Break") : (T.shortBreak || "Short Break");
                            const statusLabel = isAbsent ? (T.absent || "Absent") : isOnLeave ? (T.onLeaveStatus || "On Leave") : isYet ? (T.notStarted || "Awaiting Clock-In") : stateKey === "break" ? breakTypeLabel : stateKey === "stopped" ? (T.stoppedWorking || "Stopped Working") : (T.working || "Working");
                            const meta = appName ? getAppMeta(appName) : null;
                            const hoverBg = isDark ? "#243042" : theme.bg;
                            return (
                              <tr key={emp.id} onClick={() => setViewEmp(emp)} style={{ borderBottom: `1px solid ${C.borderLight}`, cursor: "pointer", transition: "background .15s", background: i % 2 === 0 ? "transparent" : C.bg }}
                                onMouseEnter={e => e.currentTarget.style.background = hoverBg}
                                onMouseLeave={e => e.currentTarget.style.background = i % 2 === 0 ? "transparent" : C.bg}>
                                {/* Employee */}
                                <td style={{ padding: "12px 16px" }}>
                                  <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                                    <div style={{ width: 34, height: 34, borderRadius: "50%", background: `linear-gradient(135deg,${C.brand},${C.brandDark})`, display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 800, fontSize: 12, flexShrink: 0 }}>{initials(emp.name)}</div>
                                    <div>
                                      <p style={{ fontWeight: 700, color: C.text }}>{emp.name}</p>
                                      <p style={{ fontSize: 11, color: C.textSub }}>{emp.designation || emp.role || "—"}</p>
                                    </div>
                                  </div>
                                </td>
                                {/* Status */}
                                <td style={{ padding: "12px 16px" }}>
                                  <span style={{ display: "inline-flex", alignItems: "center", gap: 5, padding: "4px 10px", borderRadius: 20, fontSize: 11, fontWeight: 700, background: theme.bg, border: `1px solid ${theme.border}`, color: theme.accent }}>
                                    <span style={{ width: 6, height: 6, borderRadius: "50%", background: theme.accent }} />
                                    {statusLabel}
                                    {stateKey === "break" && (() => {
                                      const s = Math.max(0, Math.floor(+emp.current_break_seconds || 0));
                                      const lbl = s < 60 ? "<1m" : s < 3600 ? `${Math.floor(s / 60)}m` : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
                                      return <span style={{ opacity: .8 }}>· {lbl}</span>;
                                    })()}
                                  </span>
                                  {halfDayLabel && <span style={{ display: "inline-flex", alignItems: "center", marginLeft: 6, padding: "4px 10px", borderRadius: 20, fontSize: 11, fontWeight: 700, background: "#F3E8FF", border: "1px solid #D8B4FE", color: "#6B21A8", whiteSpace: "nowrap" }}>{halfDayLabel}</span>}
                                </td>
                                {/* Clock In */}
                                <td style={{ padding: "12px 16px", color: C.text, fontWeight: 600 }}>{cin ? fmtTimeIST(emp.clock_in_time) : "—"}</td>
                                {/* Hours Worked */}
                                <td style={{ padding: "12px 16px" }}>
                                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                                    <div style={{ width: 80, height: 5, background: theme.progressTrack, borderRadius: 3, overflow: "hidden" }}>
                                      <div style={{ width: `${pctOf(workSecs, emp) ?? 0}%`, height: "100%", background: theme.progress, borderRadius: 3 }} />
                                    </div>
                                    <span style={{ fontWeight: 700, color: C.text, fontSize: 12 }}>{fmtDur(workSecs)}</span>
                                  </div>
                                </td>
                                {/* Active Time % */}
                                <td style={{ padding: "12px 16px" }}>
                                  {showActive ? (
                                    <span style={{ fontWeight: 800, color: C.text, fontSize: 13 }}
                                      title={limitedTracking ? "Limited tracking — native input hook unavailable; activity is estimated." : undefined}>
                                      {displayPct}%{limitedTracking ? " ⚠" : ""}
                                    </span>
                                  ) : <span style={{ color: C.textLight }}>—</span>}
                                </td>
                                {/* App */}
                                <td style={{ padding: "12px 16px" }}>
                                  {meta ? (
                                    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, padding: "4px 10px", borderRadius: 20, fontSize: 12, fontWeight: 600, background: C.bg, border: `1px solid ${C.border}`, color: C.text }}>
                                      <AppIcon app={meta} size={14} />{meta.label}
                                    </span>
                                  ) : <span style={{ color: C.textLight }}>—</span>}
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              );
}

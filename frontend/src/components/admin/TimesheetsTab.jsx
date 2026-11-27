import React, { useMemo } from "react";
import toast from "react-hot-toast";
import { exportAttendanceMuster } from "../../utils/musterReport";
import { getOrgTimeZone } from "../../lib/orgTime";
// Extracted from AdminDashboard.jsx ("timesheets" tab) for lazy-loading.
// Shared theme/helpers/state/ref passed as props. `employees` is used here only
// for names/lookup — not the shared status engine.
export default function TimesheetsTab({
  C, T, isDark, Icon, initials, parseUTC, employees, search, colRef,
  tsPeriod, setTsPeriod, tsDate, setTsDate,
  tsMonthData, setTsMonthData, tsMonthLoading,
  tsColFilter, setTsColFilter, tsColOpen, setTsColOpen,
}) {
              // Apply the navbar search (name or email) to the day-view roster so
              // this tab honours the same search box as the rest of the admin UI.
              const q = (search || "").trim().toLowerCase();
              const visibleEmployees = q
                ? employees.filter(e =>
                    (e.name || "").toLowerCase().includes(q) ||
                    (e.email || "").toLowerCase().includes(q))
                : employees;
              const prevDay = () => { const d = new Date(tsDate); d.setDate(d.getDate() - 1); setTsDate(d); };
              const nextDay = () => { const d = new Date(tsDate); d.setDate(d.getDate() + 1); setTsDate(d); };
              const isToday = tsDate.toDateString() === new Date().toDateString();
              const fmtDateLabel = (d) => { const opts = { day: "2-digit", month: "short", year: "numeric" }; const str = d.toLocaleDateString("en-IN", opts); return isToday ? `TODAY (${str})` : str; };

              const getEmpStats = (emp) => {
                const cin  = emp.clock_in_time  ? parseUTC(emp.clock_in_time)  : null;
                const cout = emp.clock_out_time ? parseUTC(emp.clock_out_time) : null;
                const workSecs = emp.worked_today_seconds != null
                  ? emp.worked_today_seconds
                  : (cin ? Math.max(0, Math.floor(((cout || new Date()) - cin) / 1000) - (emp.break_elapsed_seconds || 0)) : 0);
                const fmt = (s) => { if (!s || s <= 0) return "00h 00m"; return `${String(Math.floor(s/3600)).padStart(2,"0")}h ${String(Math.floor((s%3600)/60)).padStart(2,"0")}m`; };
                return { cinStr: cin ? cin.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: true, timeZone: getOrgTimeZone() }) : null, coutStr: cout ? cout.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: true, timeZone: getOrgTimeZone() }) : null, workStr: fmt(workSecs), activeStr: fmt(workSecs), workedDays: cin ? 1 : 0 };
              };

              const ALL_COLS = [T.clockInCol || "Clock In", T.clockOutCol || "Clock Out", T.work || "Work", T.active || "Active"];

              // Period mode state — drives which layout renders below.
              const isMonthMode = tsPeriod === "this_month" || tsPeriod === "last_month";
              // Memoized so toggling the column-filter dropdown (tsColOpen) or
              // other prop-driven re-renders don't re-sort the whole month grid.
              const monthRows = useMemo(
                () => (isMonthMode && tsMonthData
                  ? [...(tsMonthData.rows || [])]
                      .filter(r => !q || (r.name || "").toLowerCase().includes(q))
                      .sort((a, b) => (a.name || "").localeCompare(b.name || ""))
                  : []),
                [isMonthMode, tsMonthData, q],
              );
              const monthHeaderLabel = isMonthMode && tsMonthData
                ? (() => {
                    const [y, m] = (tsMonthData.month || "").split("-");
                    const label = new Date(parseInt(y, 10), parseInt(m, 10) - 1, 1)
                      .toLocaleDateString("en-IN", { month: "long", year: "numeric" });
                    return `${label.toUpperCase()} · ${tsPeriod === "this_month" ? (T.thisMonth || "THIS MONTH") : (T.lastMonth || "LAST MONTH")}`;
                  })()
                : "";

              const escCsv = (v) => {
                const s = v == null ? "" : String(v);
                // RFC-4180-ish quoting: wrap in quotes and double-up internal quotes.
                return `"${s.replace(/"/g, '""')}"`;
              };

              const handleExport = () => {
                // Month view → polished, colour-banded Attendance Muster
                // workbook (Attendance Report / Dept Summary / Attention
                // Required sheets). Heavy ExcelJS styling lives in the lazily
                // imported util so it stays out of the main bundle.
                if (isMonthMode && tsMonthData) {
                  toast.promise(
                    exportAttendanceMuster(tsMonthData),
                    {
                      loading: T.exporting || "Generating report…",
                      success: T.exportReady || "Attendance muster downloaded",
                      error: T.exportFailed || "Export failed — please try again",
                    },
                  ).catch((err) => console.error("Muster export failed", err));
                  return;
                }
                const rows = [[T.employee || "Employee", T.date || "Date", T.clockInCol || "Clock In", T.clockOutCol || "Clock Out", T.workHoursCol || "Work Hours", T.activeTimeCol || "Active Time", T.workedDays || "Worked Days"]];
                visibleEmployees.forEach(emp => { const s = getEmpStats(emp); rows.push([emp.name, fmtDateLabel(tsDate), s.cinStr || "—", s.coutStr || "—", s.workStr, s.activeStr, s.workedDays]); });
                const csv = rows.map(r => r.map(escCsv).join(",")).join("\n");
                const a = document.createElement("a");
                a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8;" }));
                a.download = `timesheet_${tsDate.toISOString().slice(0,10)}.csv`;
                a.click();
              };

              // Filter chip styling — active uses brand colour, inactive
              // stays neutral. Clear is destructive-flavored so admins
              // notice it's a reset, not just another filter.
              const chipStyle = (active) => ({
                padding: "7px 14px", borderRadius: 20,
                border: `1.5px solid ${active ? C.brand : C.border}`,
                background: active ? C.brandBg : C.surface,
                color: active ? C.brand : C.text,
                fontSize: 12, fontWeight: active ? 700 : 600,
                cursor: "pointer", fontFamily: "inherit", whiteSpace: "nowrap",
                transition: "all .15s",
              });
              const clearAllFilters = () => { setTsPeriod("day"); setTsDate(new Date()); setTsMonthData(null); };

              return (
                <div style={{ display: "flex", flexDirection: "column", gap: 0, margin: "-28px -28px -40px", height: "calc(100vh - 64px)", overflow: "hidden" }}>
                  <div style={{ padding: "12px 24px", borderBottom: `1px solid ${C.border}`, background: C.surface, display: "flex", alignItems: "center", gap: 12, flexShrink: 0, flexWrap: "wrap" }}>
                    {/* Period filter chips — pick "This Month" / "Last Month"
                        to switch to the per-employee monthly summary, or
                        "Clear" to drop back to the live day view. */}
                    <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                      <button onClick={() => setTsPeriod("this_month")} style={chipStyle(tsPeriod === "this_month")}>
                        {T.thisMonth || "This Month"}
                      </button>
                      <button onClick={() => setTsPeriod("last_month")} style={chipStyle(tsPeriod === "last_month")}>
                        {T.lastMonth || "Last Month"}
                      </button>
                      {(tsPeriod !== "day" || !tsDate.toDateString || tsDate.toDateString() !== new Date().toDateString()) && (
                        <button onClick={clearAllFilters}
                          style={{ padding: "7px 14px", borderRadius: 20, border: `1.5px solid ${C.border}`, background: C.surface, color: C.textSub, fontSize: 12, fontWeight: 600, cursor: "pointer", fontFamily: "inherit", display: "flex", alignItems: "center", gap: 5 }}>
                          <Icon n="x" s={11} c={C.textSub} />{T.clearFilters || "Clear Filters"}
                        </button>
                      )}
                    </div>
                    <div style={{ flex: 1 }} />
                    <div ref={colRef} style={{ position: "relative" }}>
                      <button onClick={() => setTsColOpen(v => !v)}
                        style={{ display: "flex", alignItems: "center", gap: 6, padding: "8px 16px", borderRadius: C.rSm, border: `1.5px solid ${C.border}`, background: C.surface, color: C.text, fontSize: 13, fontWeight: 500, cursor: "pointer", fontFamily: "inherit", transition: "all .15s" }}>
                        {T.filterColumns || "Filter Columns"}
                        <span style={{ fontSize: 12, color: C.textSub, maxWidth: 120, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{tsColFilter.join(", ")}</span>
                        <Icon n="chevron_down" s={12} c={C.textSub} />
                      </button>
                      {tsColOpen && (
                        <div style={{ position: "absolute", right: 0, top: "calc(100% + 6px)", background: C.surface, border: `1px solid ${C.border}`, borderRadius: C.r, boxShadow: "0 8px 24px rgba(0,0,0,0.12)", zIndex: 100, padding: 10, minWidth: 160 }}>
                          {ALL_COLS.map(col => (
                            <label key={col} style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 4px", cursor: "pointer", fontSize: 13, color: C.text, userSelect: "none" }}>
                              <input type="checkbox" checked={tsColFilter.includes(col)} onChange={() => setTsColFilter(prev => prev.includes(col) ? prev.filter(c => c !== col) : [...prev, col])} />{col}
                            </label>
                          ))}
                        </div>
                      )}
                    </div>
                    <button onClick={handleExport}
                      style={{ display: "flex", alignItems: "center", gap: 7, padding: "7px 18px", borderRadius: C.rSm, border: "none", background: C.brand, color: "#fff", fontWeight: 700, fontSize: 13, cursor: "pointer", fontFamily: "inherit" }}>
                      <Icon n="download" s={13} c="#fff" />{T.exportCsv || "Export"}
                    </button>
                  </div>
                  <div style={{ flex: 1, overflowY: "auto", overflowX: "auto" }}>
                    {isMonthMode ? (
                      /* ── MONTHLY SUMMARY VIEW ─────────────────────────
                         Renders per-employee aggregates from
                         /reports/monthly-attendance so admins can see the
                         entire month in one table. Pulls Working / Present /
                         Leave / Absent / Total Hours straight from the
                         payroll-grade endpoint to keep the figures
                         consistent across reports. */
                      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                        <thead>
                          <tr style={{ background: C.bg, position: "sticky", top: 0, zIndex: 2 }}>
                            <th style={{ padding: "12px 20px", textAlign: "left", fontSize: 11, fontWeight: 700, color: C.textSub, textTransform: "uppercase", letterSpacing: ".5px", borderBottom: `1px solid ${C.border}` }} colSpan={7}>
                              <div style={{ display: "flex", alignItems: "center", gap: 10, color: C.text, fontSize: 12 }}>
                                <Icon n="calendar" s={14} c={C.brand} />
                                {monthHeaderLabel}
                              </div>
                            </th>
                          </tr>
                          <tr style={{ background: C.bg, position: "sticky", top: 0, zIndex: 1 }}>
                            {[T.employeeCol || "EMPLOYEE", T.team || "TEAM", T.workingDays || "WORKING DAYS", T.presentDays || "PRESENT", T.leave || "LEAVE", T.absent || "ABSENT", T.totalHours || "TOTAL HOURS"].map(h => (
                              <th key={h} style={{ padding: "10px 20px", textAlign: "left", fontSize: 11, fontWeight: 700, color: C.textSub, textTransform: "uppercase", letterSpacing: ".5px", borderBottom: `1px solid ${C.border}` }}>{h}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {tsMonthLoading ? (
                            <tr><td colSpan={7} style={{ padding: "80px 0", textAlign: "center", color: C.textSub }}>{T.loading || "Loading…"}</td></tr>
                          ) : monthRows.length === 0 ? (
                            <tr><td colSpan={7} style={{ padding: "80px 0", textAlign: "center", color: C.textSub }}>{T.noWorkersFound || "No data for this period"}</td></tr>
                          ) : monthRows.map(r => (
                            <tr key={r.id} style={{ borderBottom: `1px solid ${C.border}` }}>
                              <td style={{ padding: "14px 20px" }}>
                                <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                                  <div style={{ width: 32, height: 32, borderRadius: "50%", background: `linear-gradient(135deg,${C.brand},${C.brandDark})`, display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 800, fontSize: 12, flexShrink: 0 }}>{initials(r.name)}</div>
                                  <span style={{ fontWeight: 700, fontSize: 13, color: C.text }}>{r.name}</span>
                                </div>
                              </td>
                              <td style={{ padding: "14px 20px", color: C.textSub, fontSize: 12 }}>{r.team || "—"}</td>
                              <td style={{ padding: "14px 20px", fontWeight: 700, color: C.text }}>{r.working_days ?? 0}</td>
                              <td style={{ padding: "14px 20px", fontWeight: 700, color: "#22C55E" }}>{r.present_days ?? 0}</td>
                              <td style={{ padding: "14px 20px", fontWeight: 700, color: "#7C3AED" }}>{Number.isInteger(Number(r.leave_days)) ? r.leave_days : Number(r.leave_days).toFixed(1)}</td>
                              <td style={{ padding: "14px 20px", fontWeight: 700, color: "#EF4444" }}>{r.absent_days ?? 0}</td>
                              <td style={{ padding: "14px 20px", fontWeight: 700, color: C.text }}>{r.total_duration}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    ) : (
                    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13, tableLayout: "fixed" }}>
                      <colgroup><col style={{ width: 220 }} /><col /><col style={{ width: 220 }} /></colgroup>
                      <thead>
                        <tr style={{ background: C.bg, position: "sticky", top: 0, zIndex: 2 }}>
                          <th style={{ padding: "10px 20px", textAlign: "left", fontSize: 11, fontWeight: 700, color: C.textSub, textTransform: "uppercase", letterSpacing: ".5px", borderBottom: `1px solid ${C.border}`, borderRight: `1px solid ${C.border}` }}>{T.employeeCol || "EMPLOYEE"}</th>
                          <th style={{ padding: "10px 20px", textAlign: "center", fontSize: 12, fontWeight: 700, color: C.text, borderBottom: `1px solid ${C.border}`, borderRight: `1px solid ${C.border}` }}>
                            <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 12 }}>
                              <button onClick={prevDay} style={{ background: "none", border: `1px solid ${C.border}`, borderRadius: "50%", width: 24, height: 24, cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center" }}><Icon n="chevron_left" s={13} c={C.textSub} /></button>
                              <span>{fmtDateLabel(tsDate)}</span>
                              <button onClick={nextDay} disabled={isToday} style={{ background: "none", border: `1px solid ${C.border}`, borderRadius: "50%", width: 24, height: 24, cursor: isToday ? "default" : "pointer", opacity: isToday ? 0.4 : 1, display: "flex", alignItems: "center", justifyContent: "center" }}><Icon n="chevron_right" s={13} c={C.textSub} /></button>
                            </div>
                          </th>
                          <th style={{ padding: "10px 20px", textAlign: "left", fontSize: 11, fontWeight: 700, color: C.textSub, textTransform: "uppercase", letterSpacing: ".5px", borderBottom: `1px solid ${C.border}` }}>{T.totalWorkSummaryCol || "TOTAL WORK SUMMARY"}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {visibleEmployees.length === 0 ? (
                          <tr><td colSpan={3} style={{ padding: "80px 0", textAlign: "center", color: C.textSub }}>{T.noWorkersFound || "No workers found."}</td></tr>
                        ) : visibleEmployees.map(emp => {
                          const s = getEmpStats(emp);
                          const dateStr = tsDate.toLocaleDateString("en-IN", { day: "2-digit", month: "short" });
                          return (
                            <tr key={emp.id} style={{ borderBottom: `1px solid ${C.border}`, verticalAlign: "top" }}>
                              <td style={{ padding: "16px 20px", borderRight: `1px solid ${C.border}`, background: C.surface }}>
                                <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                                  <div style={{ width: 36, height: 36, borderRadius: "50%", background: `linear-gradient(135deg,${C.brand},${C.brandDark})`, display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 800, fontSize: 13, flexShrink: 0 }}>{initials(emp.name)}</div>
                                  <span style={{ fontWeight: 700, fontSize: 14, color: C.text }}>{emp.name}</span>
                                </div>
                              </td>
                              <td style={{ padding: "16px 20px", borderRight: `1px solid ${C.border}`, background: C.bg }}>
                                {s.cinStr ? (
                                  <div style={{ background: C.surface, border: `1px dashed ${C.border}`, borderRadius: 8, padding: "10px 14px", display: "inline-block", minWidth: 180 }}>
                                    <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                                      <span style={{ fontSize: 12, fontWeight: 600, color: C.textSub }}>{T.today || "Today"} ✎</span>
                                      <span style={{ fontSize: 12, color: C.textSub }}>{dateStr}</span>
                                    </div>
                                    {[
                                      tsColFilter.includes(T.clockInCol || "Clock In")  && s.cinStr  && { label: T.inCol || "IN",     val: s.cinStr,   color: C.green },
                                      tsColFilter.includes(T.clockOutCol || "Clock Out") && s.coutStr && { label: T.outCol || "OUT",    val: s.coutStr,  color: C.red },
                                      tsColFilter.includes(T.work || "Work")                   && { label: T.workCol || "WORK",   val: s.workStr,  color: C.text },
                                      tsColFilter.includes(T.active || "Active")                 && { label: T.activeCol || "ACTIVE", val: s.activeStr,color: C.brand },
                                    ].filter(Boolean).map(row => (
                                      <div key={row.label} style={{ display: "flex", justifyContent: "space-between", marginBottom: 4, alignItems: "center" }}>
                                        <span style={{ fontSize: 13, fontWeight: 600, color: row.color, minWidth: 80, fontVariantNumeric: "tabular-nums" }}>{row.val}</span>
                                        <span style={{ fontSize: 10, fontWeight: 700, color: C.textLight, letterSpacing: 1 }}>{row.label}</span>
                                      </div>
                                    ))}
                                  </div>
                                ) : (
                                  <span style={{ fontSize: 12, color: C.textLight, fontStyle: "italic" }}>{T.notClockedIn || "Not clocked in"}</span>
                                )}
                              </td>
                              <td style={{ padding: "16px 20px", background: C.bg }}>
                                <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                                  {[
                                    { val: s.workStr, label: T.workedHours || "Worked hours" },
                                    { val: s.activeStr, label: T.activeDuration || "Active time" },
                                    { val: String(s.workedDays), label: T.workedDays || "Worked days" },
                                  ].map(r => (
                                    <div key={r.label} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "6px 10px", background: isDark ? "#2A2A3A" : "#FFFBEB", borderRadius: 6, border: `1px solid ${isDark ? "#3A3A4A" : "#FEF3C7"}` }}>
                                      <span style={{ fontSize: 13, fontWeight: 700, color: "#B45309" }}>{r.val}</span>
                                      <span style={{ fontSize: 11, color: C.textSub }}>{r.label}</span>
                                    </div>
                                  ))}
                                </div>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                    )}
                  </div>
                </div>
              );
}

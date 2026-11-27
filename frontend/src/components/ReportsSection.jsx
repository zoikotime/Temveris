// ─── REPORTS SECTION ─────────────────────────────────────────────────────────
// Self-contained Reports module for the Admin Dashboard.
//
// Renders a left sub-sidebar with all 13 report categories and a content
// pane that fetches data from the /reports/* backend endpoints. The look &
// feel matches the rest of AdminDashboard via the `C` theme + `Icon` props
// passed in from the parent so we don't duplicate styling.

import React, { useState, useEffect, useCallback, useRef } from "react";
import { useLanguage } from "../context/LanguageContext";
import { getOrgTimeZone } from "../lib/orgTime";
import { useCapability, FeatureLocked } from "../entitlements/Gate";

// xlsx is ~250 KB minified — easily the biggest single dep on the Admin
// route. Loading it on demand (the user has to click "Export to Excel")
// trims that off the AdminDashboard chunk's initial cost. The dynamic
// import shows up as its own chunk in `dist/assets/`.
const loadXLSX = () => import("xlsx");
// Styled variant (cell fills/fonts/borders) — plain `xlsx` can't color cells.
// Loaded on demand for the colorful Employee Directory export only.
const loadXLSXStyle = () => import("xlsx-js-style");

const fmtEmpType = (t) =>
  ({ full_time: "Full Time", part_time: "Part Time" }[t] || "Full Time");

const REPORT_NAV = [
  { k: "attendance", icon: "users",      label: "Attendance Report" },
  { k: "daily",     icon: "file_text",   label: "Daily Attendance" },
  { k: "monthly",   icon: "calendar",    label: "Monthly Attendance" },
  { k: "inout",     icon: "timelapse",   label: "Monthly Clock In/Out" },
  { k: "late",      icon: "clock",       label: "Late Clock-In" },
  { k: "overtime",  icon: "trending_up", label: "Overtime Report" },
  { k: "custom",    icon: "layers",      label: "Custom Report" },
  { k: "joiners",   icon: "user_plus",   label: "New Joiners" },
  { k: "directory", icon: "users",       label: "Employee Directory" },
  { k: "activity",  icon: "activity",    label: "Work Activity Log" },
  { k: "input",     icon: "edit",        label: "Input Activity Report" },
  { k: "loginip",   icon: "shield",      label: "Access Location Report" },
];

const todayIso = () => new Date().toISOString().slice(0, 10);
const monthIso = () => new Date().toISOString().slice(0, 7);
const fmtIST = (utcIso) => {
  if (!utcIso) return "—";
  try {
    const v = String(utcIso);
    const d = new Date(/[Z+]/.test(v.slice(-6)) ? v : v + "Z");
    return d.toLocaleString("en-IN", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: true, timeZone: getOrgTimeZone() });
  } catch { return "—"; }
};
const fmtTime = (utcIso) => {
  if (!utcIso) return "—";
  try {
    const v = String(utcIso);
    const d = new Date(/[Z+]/.test(v.slice(-6)) ? v : v + "Z");
    return d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: true, timeZone: getOrgTimeZone() });
  } catch { return "—"; }
};
const fmtDate = (iso) => {
  if (!iso) return "—";
  try {
    return new Date(iso + "T00:00:00").toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
  } catch { return iso; }
};

const initials = (name) =>
  (name || "").split(" ").map(w => w[0]).join("").toUpperCase().slice(0, 2) || "U";

const sortByName = (arr) => [...arr].sort((a, b) => (a.name || "").localeCompare(b.name || ""));

const exportCSV = (filename, headers, rows) => {
  const csv = [headers, ...rows]
    .map(r => r.map(v => `"${String(v ?? "").replace(/"/g, '""')}"`).join(","))
    .join("\n");
  // UTF-8 BOM ("﻿") prefix tells Excel to decode the file as UTF-8.
  // Without it, Excel falls back to Windows-1252 and characters like the
  // em-dash ("—") render as garbled "â€"" sequences — which is what
  // payroll reviewers were seeing in the Joining Date / Leave Type
  // columns before this fix.
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8" }));
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
};

// ── Module-scope filter primitives ────────────────────────────────────────────
// These must live OUTSIDE ReportsSection so their function identity stays
// stable across re-renders. Local arrow-function components get a new object
// reference every render → React treats them as a new component type →
// unmounts+remounts the entire subtree, which snaps any open native <select>
// or <input> closed. Module-scope functions are created once and reused.

function RFilterBar({ C, Icon, onExport, children }) {
  return (
    <div style={{
      background: C.surface, borderRadius: C.r, border: `1px solid ${C.border}`,
      padding: "14px 18px", display: "flex", alignItems: "center", gap: 12,
      flexWrap: "wrap", marginBottom: 16,
    }}>
      {children}
      <div style={{ flex: 1 }} />
      <button onClick={onExport} style={{
        display: "flex", alignItems: "center", gap: 6,
        padding: "8px 18px", borderRadius: 8,
        border: `1px solid ${C.brand}`, background: C.brand,
        color: "#fff", fontWeight: 700, fontSize: 13,
        cursor: "pointer", fontFamily: "inherit",
      }}>
        <Icon n="download" s={14} c="#fff" /> Export
      </button>
    </div>
  );
}

function RDateInput({ C, value, onChange, label }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
      {label && <span style={{ fontSize: 12, color: C.textSub, fontWeight: 600 }}>{label}</span>}
      <input
        type="date"
        value={value}
        onChange={e => onChange(e.target.value)}
        style={{
          padding: "9px 12px", borderRadius: 8, border: `1px solid ${C.border}`,
          background: C.surface, color: C.text, fontSize: 13,
          fontFamily: "inherit", outline: "none",
        }}
      />
    </div>
  );
}

function RMonthInput({ C, value, onChange }) {
  return (
    <input
      type="month"
      value={value}
      onChange={e => onChange(e.target.value)}
      style={{
        padding: "9px 12px", borderRadius: 8, border: `1px solid ${C.border}`,
        background: C.surface, color: C.text, fontSize: 13,
        fontFamily: "inherit", outline: "none",
      }}
    />
  );
}

function REmpFilterSelect({ C, value, onChange, employees }) {
  return (
    <select
      value={value}
      onChange={e => onChange(e.target.value)}
      style={{
        padding: "9px 14px", borderRadius: 8, border: `1.5px solid ${value !== "all" ? C.brand : C.textSub}`,
        background: value !== "all" ? C.brand : C.surface,
        color: value !== "all" ? "#fff" : C.text,
        fontSize: 13, fontWeight: 600, cursor: "pointer", fontFamily: "inherit",
        outline: "none", minWidth: 200, flexShrink: 0,
        boxShadow: "0 1px 3px rgba(0,0,0,0.1)",
      }}
    >
      <option value="all">All Employees</option>
      {(employees || []).map(e => (
        <option key={e.id} value={e.id}>{e.name}</option>
      ))}
    </select>
  );
}

export default function ReportsSection({ API_BASE, token, C, Icon, employees = [] }) {
  const { T } = useLanguage();
  // reports.standard is All-plan (the Daily Report). reports.advanced (Governed+)
  // gates the Calendar View / attendance heatmap. The backend projection decides
  // per plan/role; interactive === true only when it resolves to SHOW.
  const advReports = useCapability("reports.advanced");
  const [section, setSection] = useState("attendance");
  const contentRef = useRef(null);

  // Reset content scroll to top when section changes
  useEffect(() => {
    if (contentRef.current) contentRef.current.scrollTop = 0;
  }, [section]);

  // Shared filters
  const [date, setDate]               = useState(todayIso());
  const [month, setMonth]             = useState(monthIso());
  const [fromDate, setFromDate]       = useState(() => {
    const d = new Date(); d.setDate(d.getDate() - 6);
    return d.toISOString().slice(0, 10);
  });
  const [toDate, setToDate]           = useState(todayIso());
  const [empFilter, setEmpFilter]     = useState("all");      // "all" | user.id

  // Daily Attendance state
  const [daily, setDaily]             = useState(null);
  const [dailyTab, setDailyTab]       = useState("worked");   // worked | late | on_leave | absent
  // Other section states
  const [monthlyData, setMonthlyData] = useState(null);
  const [inOutData, setInOutData]     = useState(null);
  const [lateData, setLateData]       = useState(null);
  const [otData, setOtData]           = useState(null);
  const [customData, setCustomData]   = useState(null);
  const [joinersData, setJoinersData] = useState(null);
  const [dirData, setDirData]         = useState(null);
  const [activityData, setActivityData] = useState(null);
  const [inputData, setInputData]     = useState(null);
  const [loginIpData, setLoginIpData] = useState(null);
  const [loginIpEmp, setLoginIpEmp]   = useState("all");   // local filter for Access Location Report

  // Attendance Report state
  const [attDetail, setAttDetail]     = useState(null);
  const [calHeatmap, setCalHeatmap]   = useState(null);
  const [attView, setAttView]         = useState("daily");  // "daily" | "calendar"
  const [calMonth, setCalMonth]       = useState(monthIso());
  const [attDate, setAttDate]         = useState(todayIso());
  const [attEmpFilter, setAttEmpFilter] = useState("all");
  const [attLoading, setAttLoading]   = useState(false);
  const [calSelectedDay, setCalSelectedDay] = useState(null);

  const [loading, setLoading]         = useState(false);

  const fetchJSON = useCallback(async (path) => {
    if (!token) return null;
    try {
      const res = await fetch(`${API_BASE}${path}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) return null;
      return await res.json();
    } catch { return null; }
  }, [API_BASE, token]);

  // ── Fetch driver: re-runs whenever the active section or filters change ──
  useEffect(() => {
    if (!token) return;
    setLoading(true);
    const userParam = empFilter !== "all" ? `&user_id=${empFilter}` : "";
    const userParamStandalone = empFilter !== "all" ? `?user_id=${empFilter}` : "";

    const run = async () => {
      try {
        if (section === "attendance") {
          // handled by its own effect below
          setLoading(false);
          return;
        } else if (section === "daily") {
          const d = await fetchJSON(`/reports/daily-attendance?date=${date}${userParam}`);
          setDaily(d);
        } else if (section === "monthly") {
          const d = await fetchJSON(`/reports/monthly-attendance?month=${month}${userParam}`);
          setMonthlyData(d);
        } else if (section === "inout") {
          const d = await fetchJSON(`/reports/monthly-inout?month=${month}${userParam}`);
          setInOutData(d);
        } else if (section === "late") {
          const d = await fetchJSON(`/reports/late-clockin?date=${date}${userParam}`);
          setLateData(d);
        } else if (section === "overtime") {
          const d = await fetchJSON(`/reports/overtime?from=${fromDate}&to=${toDate}${userParam}`);
          setOtData(d);
        } else if (section === "custom") {
          const d = await fetchJSON(`/reports/custom?from=${fromDate}&to=${toDate}${userParam}`);
          setCustomData(d);
        } else if (section === "joiners") {
          const d = await fetchJSON(`/reports/new-joiners?from=${fromDate}&to=${toDate}`);
          setJoinersData(d);
        } else if (section === "directory") {
          const d = await fetchJSON(`/reports/employee-directory`);
          setDirData(d);
        } else if (section === "activity") {
          const d = await fetchJSON(`/reports/work-activity?date=${date}${userParam}`);
          setActivityData(d);
        } else if (section === "input") {
          const d = await fetchJSON(`/reports/input-activity?date=${date}${userParam}`);
          setInputData(d);
        } else if (section === "loginip") {
          const d = await fetchJSON(`/reports/login-ip${userParamStandalone}`);
          setLoginIpData(d);
        }
      } finally { setLoading(false); }
    };
    run();
  }, [section, date, month, fromDate, toDate, empFilter, fetchJSON, token]);

  // ── Attendance Report dedicated fetch ────────────────────────────────────
  useEffect(() => {
    if (section !== "attendance" || !token) return;
    setAttLoading(true);
    const userParam = attEmpFilter !== "all" ? `&user_id=${attEmpFilter}` : "";
    const run = async () => {
      try {
        const [detail, heat] = await Promise.all([
          fetchJSON(`/reports/attendance-detail?date=${attDate}${userParam}`),
          fetchJSON(`/reports/calendar-heatmap?month=${calMonth}`),
        ]);
        setAttDetail(detail);
        setCalHeatmap(heat);
      } finally { setAttLoading(false); }
    };
    run();
  }, [section, attDate, calMonth, attEmpFilter, fetchJSON, token]);

  // ── Common UI primitives ───────────────────────────────────────────────
  const card = (extra = {}) => ({ background: C.surface, borderRadius: C.r, border: `1px solid ${C.border}`, padding: 20, ...extra });

  const Avatar = ({ name }) => (
    <div style={{ width: 32, height: 32, borderRadius: "50%", background: `linear-gradient(135deg,${C.brand},${C.brandDark})`, display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 800, fontSize: 11, flexShrink: 0 }}>
      {initials(name)}
    </div>
  );

  const Empty = ({ msg = "No records found" }) => (
    <tr><td colSpan={20} style={{ padding: "60px 20px", textAlign: "center", color: C.textLight, fontSize: 13 }}>{msg}</td></tr>
  );

  const Th = ({ children, w }) => (
    <th style={{ padding: "12px 16px", textAlign: "left", fontSize: 11, fontWeight: 700, color: C.textSub, textTransform: "uppercase", letterSpacing: ".5px", borderBottom: `1px solid ${C.border}`, whiteSpace: "nowrap", width: w }}>{children}</th>
  );

  const Td = ({ children, style, rowSpan, colSpan }) => (
    <td rowSpan={rowSpan} colSpan={colSpan} style={{ padding: "12px 16px", fontSize: 13, color: C.text, borderBottom: `1px solid ${C.borderLight}`, ...style }}>{children}</td>
  );

  // Refs hold the latest values so useCallback wrappers below can stay
  // stable across renders. C is a new object reference every parent render
  // (computed from isDark each time), so putting it in deps would invalidate
  // FilterBar/DateInput/MonthInput on every keystroke — causing React to
  // unmount the native <select>/<input>, snapping it closed mid-interaction.
  const _cRef = useRef(C);
  const _mRef = useRef(month);
  _cRef.current = C;
  _mRef.current = month;

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const FilterBar  = useCallback((props) => <RFilterBar  C={_cRef.current} Icon={Icon} {...props} />, [Icon]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const DateInput  = useCallback((props) => <RDateInput  C={_cRef.current} {...props} />, []);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const MonthInput = useCallback((props) => <RMonthInput C={_cRef.current} value={_mRef.current} onChange={setMonth} {...props} />, []);
  const empFilterSelect = () => <REmpFilterSelect C={C} value={empFilter} onChange={setEmpFilter} employees={employees} />;

  // ── ATTENDANCE REPORT ─────────────────────────────────────────────────
  const renderAttendance = () => {
    const summary = attDetail?.summary || {};
    const records = sortByName(attDetail?.records || []);
    const days    = calHeatmap?.days || [];
    const totalEmp = summary.total || 0;

    const secToHm = (s) => {
      if (!s && s !== 0) return "—";
      const h = Math.floor(s / 3600);
      const m = Math.floor((s % 3600) / 60);
      return `${h}h ${String(m).padStart(2, "0")}m`;
    };

    const STATUS_COLORS = {
      "Working":     { bg: C.greenBg, color: C.green,  border: C.greenBdr,           dot: C.green  },
      "On Break":    { bg: C.amberBg, color: C.amber,  border: C.amberBdr,           dot: C.amber  },
      "Clocked Out": { bg: C.brandBg, color: C.brand,  border: C.brandBdr,           dot: C.brand  },
      "On Leave":    { bg: "#F5F3FF", color: "#7C3AED", border: "#DDD6FE",           dot: "#7C3AED" },
      "Absent":      { bg: C.redBg,   color: C.red,    border: C.redBdr,             dot: C.red    },
      "Yet to Start":{ bg: C.surfaceAlt, color: C.textSub, border: C.borderLight,    dot: C.textLight },
    };

    const StatusBadge = ({ status }) => {
      const s = STATUS_COLORS[status] || STATUS_COLORS["Absent"];
      return (
        <span style={{
          display: "inline-flex", alignItems: "center", gap: 6,
          background: s.bg, color: s.color, border: `1px solid ${s.border}`,
          padding: "4px 10px", borderRadius: 99,
          fontSize: 11, fontWeight: 700, whiteSpace: "nowrap",
        }}>
          <span style={{ width: 6, height: 6, borderRadius: "50%", background: s.dot, flexShrink: 0 }} />
          {status}
        </span>
      );
    };

    const handleExcelExport = async () => {
      const XLSX = await loadXLSX();
      const wsData = [
        ["Employee Name", "Designation", "Department", "First Clock-In", "Last Clock-Out", "Total Work Hours", "Total Break Time", "Sessions", "Status"],
        ...records.map(r => [
          r.name || "—",
          r.designation || "—",
          r.department || "—",
          r.first_clock_in ? fmtTime(r.first_clock_in) : "—",
          r.last_clock_out ? fmtTime(r.last_clock_out) : r.status === "Working" ? "Currently Working" : "—",
          secToHm(r.total_work_seconds),
          secToHm(r.total_break_seconds),
          r.sessions_count ?? 0,
          r.status || "—",
        ]),
      ];
      const wb = XLSX.utils.book_new();
      const ws = XLSX.utils.aoa_to_sheet(wsData);
      // Column widths
      ws["!cols"] = [{ wch: 22 }, { wch: 18 }, { wch: 18 }, { wch: 20 }, { wch: 20 }, { wch: 16 }, { wch: 16 }, { wch: 10 }, { wch: 14 }];
      XLSX.utils.book_append_sheet(wb, ws, "Attendance");
      XLSX.writeFile(wb, `attendance_${attDate}.xlsx`);
    };

    const kpis = [
      { label: "Total Employees", value: totalEmp,            icon: "users",       color: "#6366F1", bg: "#EEF2FF" },
      { label: "Present",         value: summary.present || 0, icon: "check",       color: "#059669", bg: "#ECFDF5" },
      { label: "Absent",          value: summary.absent || 0,  icon: "x",           color: "#DC2626", bg: "#FEF2F2" },
      { label: "Working",         value: summary.working || 0, icon: "activity",    color: "#0EA5E9", bg: "#F0F9FF" },
      { label: "On Break",        value: summary.on_break || 0,icon: "clock",       color: "#D97706", bg: "#FFFBEB" },
      { label: "Clocked Out",     value: summary.clocked_out||0,icon:"log_out",     color: "#7C3AED", bg: "#F5F3FF" },
    ];

    // Calendar helpers
    const calDayClick = (d) => {
      setCalSelectedDay(calSelectedDay === d.date ? null : d.date);
      setAttDate(d.date);
      setAttView("daily");
    };

    const heatColor = (d) => {
      // Off-day with real attendance → tint it green so weekend/holiday work is
      // visible at a glance, not masked as a flat "Off" cell.
      if (d.is_weekend) return d.present > 0
        ? { bg: "#ECFDF5", text: "#059669", border: "#A7F3D0" }
        : { bg: "#F3F4F6", text: "#9CA3AF", border: "#E5E7EB" };
      if (d.total === 0)  return { bg: "#F9FAFB", text: "#6B7280", border: "#E5E7EB" };
      const pct = d.present / d.total;
      if (pct >= 0.8) return { bg: "#DCFCE7", text: "#166534", border: "#86EFAC" };
      if (pct >= 0.5) return { bg: "#FEF9C3", text: "#854D0E", border: "#FDE047" };
      return { bg: "#FEE2E2", text: "#991B1B", border: "#FCA5A5" };
    };

    const calRows = (() => {
      if (!days.length) return [];
      const first = new Date(days[0].date + "T00:00:00");
      const startDow = first.getDay(); // 0=Sun
      const padded = Array(startDow).fill(null).concat(days);
      const rows = [];
      for (let i = 0; i < padded.length; i += 7) rows.push(padded.slice(i, i + 7));
      return rows;
    })();

    const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

    return (
      <>
        {/* KPI Cards — responsive grid that wraps on narrow screens instead of clipping */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 12, marginBottom: 18 }}>
          {kpis.map(k => (
            <div key={k.label} style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: C.r, padding: "16px 18px", display: "flex", flexDirection: "column", gap: 8, transition: "transform .15s, box-shadow .18s, border-color .18s" }}
              onMouseEnter={e => { e.currentTarget.style.transform = "translateY(-2px)"; e.currentTarget.style.boxShadow = "0 6px 18px rgba(16,24,40,.08)"; e.currentTarget.style.borderColor = k.color + "55"; }}
              onMouseLeave={e => { e.currentTarget.style.transform = ""; e.currentTarget.style.boxShadow = ""; e.currentTarget.style.borderColor = C.border; }}>
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <div style={{ width: 36, height: 36, borderRadius: 10, background: k.bg, display: "flex", alignItems: "center", justifyContent: "center" }}>
                  <Icon n={k.icon} s={17} c={k.color} />
                </div>
                <span style={{ fontSize: 11, fontWeight: 700, color: C.textSub, textTransform: "uppercase", letterSpacing: ".5px" }}>{k.label}</span>
              </div>
              <span style={{ fontSize: 28, fontWeight: 800, color: k.color, lineHeight: 1, fontVariantNumeric: "tabular-nums" }}>{k.value}</span>
            </div>
          ))}
        </div>

        {/* View Toggle + Filters */}
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 14, flexWrap: "wrap" }}>
          <div style={{ display: "flex", background: C.bg, borderRadius: 10, border: `1px solid ${C.border}`, padding: 3, gap: 2 }}>
            {[{ k: "daily", l: "Daily Report" }, { k: "calendar", l: "Calendar View" }].map(v => (
              <button key={v.k} onClick={() => setAttView(v.k)}
                style={{ padding: "7px 18px", borderRadius: 8, border: "none", background: attView === v.k ? C.brand : "transparent", color: attView === v.k ? "#fff" : C.textSub, fontWeight: 700, fontSize: 13, cursor: "pointer", fontFamily: "inherit", transition: "all .15s" }}>
                {v.l}{v.k === "calendar" && !advReports.interactive ? " 🔒" : ""}
              </button>
            ))}
          </div>
          {attView === "daily" && (
            <>
              <input type="date" value={attDate} onChange={e => setAttDate(e.target.value)}
                style={{ padding: "8px 12px", borderRadius: 8, border: `1px solid ${C.border}`, background: C.surface, color: C.text, fontSize: 13, fontFamily: "inherit", outline: "none" }} />
              <select value={attEmpFilter} onChange={e => setAttEmpFilter(e.target.value)}
                style={{ padding: "9px 14px", borderRadius: 8, border: `1.5px solid ${attEmpFilter !== "all" ? C.brand : C.textSub}`, background: attEmpFilter !== "all" ? C.brand : C.surface, color: attEmpFilter !== "all" ? "#fff" : C.text, fontSize: 13, fontWeight: 600, cursor: "pointer", fontFamily: "inherit", outline: "none", minWidth: 180, boxShadow: "0 1px 3px rgba(0,0,0,0.1)" }}>
                <option value="all">All Employees</option>
                {employees.map(e => <option key={e.id} value={e.id}>{e.name}</option>)}
              </select>
            </>
          )}
          {attView === "calendar" && advReports.interactive && (
            <input type="month" value={calMonth} onChange={e => setCalMonth(e.target.value)}
              style={{ padding: "8px 12px", borderRadius: 8, border: `1px solid ${C.border}`, background: C.surface, color: C.text, fontSize: 13, fontFamily: "inherit", outline: "none" }} />
          )}
          <div style={{ flex: 1 }} />
          {attLoading && <span style={{ fontSize: 12, color: C.textSub }}>{T.loadingEllipsis || "Loading…"}</span>}
          {attView === "daily" && (
            <button onClick={handleExcelExport}
              style={{ display: "flex", alignItems: "center", gap: 6, padding: "8px 18px", borderRadius: 8, border: `1px solid #22C55E`, background: "#22C55E", color: "#fff", fontWeight: 700, fontSize: 13, cursor: "pointer", fontFamily: "inherit" }}>
              <Icon n="download" s={14} c="#fff" /> {T.exportExcel || "Export Excel"}
            </button>
          )}
        </div>

        {/* DAILY VIEW: Table — sticky Employee column + horizontal scroll keeps
            Status / Work Hours / Break Time visible no matter how narrow the
            viewport gets. */}
        {attView === "daily" && (
          <div style={{ background: C.surface, borderRadius: C.r, border: `1px solid ${C.border}`, overflow: "hidden" }}>
            <style>{`
              .zt-att-scroll { overflow-x: auto; }
              /* Scrollbar inherits the global wide brand-blue / green-hover
                 style from index.css; no local override needed. */
              .zt-att-table { width: 100%; min-width: 1080px; border-collapse: separate; border-spacing: 0; }
              .zt-att-table th, .zt-att-table td { padding: 12px 16px; font-size: 13px; color: ${C.text}; border-bottom: 1px solid ${C.borderLight}; vertical-align: middle; }
              .zt-att-table thead th { background: ${C.bg}; font-size: 11px; font-weight: 700; color: ${C.textSub}; text-transform: uppercase; letter-spacing: .5px; white-space: nowrap; border-bottom: 1px solid ${C.border}; }
              .zt-att-sticky { position: sticky; left: 0; background: ${C.surface}; z-index: 2; box-shadow: 1px 0 0 ${C.borderLight}; }
              .zt-att-table tbody tr:hover .zt-att-sticky,
              .zt-att-table tbody tr:hover td { background: ${C.bg}; }
              .zt-att-table thead th.zt-att-sticky { background: ${C.bg}; z-index: 3; }
              .zt-att-num { font-variant-numeric: tabular-nums; }
            `}</style>
            <div className="zt-att-scroll">
              <table className="zt-att-table">
                <thead>
                  <tr>
                    <th className="zt-att-sticky" style={{ minWidth: 240 }}>{T.employee || "Employee"}</th>
                    <th>{T.designation || "Designation"}</th>
                    <th>{T.department || "Department"}</th>
                    <th>{T.firstClockIn || "First Clock-In"}</th>
                    <th>{T.lastClockOut || "Last Clock-Out"}</th>
                    <th>{T.workHoursCol || "Work Hours"}</th>
                    <th>{T.breakTime || "Break Time"}</th>
                    <th>{T.sessions || "Sessions"}</th>
                    <th style={{ minWidth: 130 }}>{T.status || "Status"}</th>
                  </tr>
                </thead>
                <tbody>
                  {attLoading && records.length === 0 ? (
                    <tr><td colSpan={9} style={{ padding: 60, textAlign: "center", color: C.textLight, fontSize: 13 }}>{T.loadingEllipsis || "Loading…"}</td></tr>
                  ) : records.length === 0 ? (
                    <tr><td colSpan={9} style={{ padding: 60, textAlign: "center", color: C.textLight, fontSize: 13 }}>{T.noRecordsForDate || "No records for this date"}</td></tr>
                  ) : records.map(r => (
                    <tr key={r.user_id} style={{ transition: "background .12s" }}>
                      <td className="zt-att-sticky">
                        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                          <div style={{ width: 34, height: 34, borderRadius: "50%", background: `linear-gradient(135deg,${C.brand},${C.brandDark})`, display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 800, fontSize: 11, flexShrink: 0 }}>
                            {initials(r.name)}
                          </div>
                          <div style={{ minWidth: 0 }}>
                            <div style={{ fontWeight: 700, fontSize: 13, color: C.text, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", maxWidth: 180 }}>{r.name}</div>
                            <div style={{ fontSize: 11, color: C.textSub, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", maxWidth: 180 }}>{r.email}</div>
                          </div>
                        </div>
                      </td>
                      <td style={{ color: C.textSub }}>{r.designation || "—"}</td>
                      <td style={{ color: C.textSub }}>{r.department || "—"}</td>
                      <td className="zt-att-num" style={{ fontWeight: 600, color: r.first_clock_in ? C.green : C.textLight }}>
                        {r.first_clock_in ? fmtTime(r.first_clock_in) : "—"}
                      </td>
                      <td className="zt-att-num" style={{ fontWeight: 600, color: r.last_clock_out ? C.text : r.status === "Working" ? C.brand : C.textLight }}>
                        {r.last_clock_out
                          ? fmtTime(r.last_clock_out)
                          : r.status === "Working"  ? (T.currentlyWorking || "Currently Working")
                          : r.status === "On Break" ? (T.onBreak || "On Break")
                          : "—"}
                      </td>
                      <td className="zt-att-num" style={{ fontWeight: 700, color: (r.total_work_seconds || 0) > 0 ? C.green : C.textLight }}>
                        {secToHm(r.total_work_seconds)}
                      </td>
                      <td className="zt-att-num" style={{ color: C.textSub }}>{secToHm(r.total_break_seconds)}</td>
                      <td style={{ color: C.textSub, fontWeight: 600 }}>{r.sessions_count ?? 0}</td>
                      <td><StatusBadge status={r.status} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* CALENDAR VIEW — reports.advanced (Governed+). Verified sees a LOCK
            here (never a hidden feature); the Daily Report above stays available. */}
        {attView === "calendar" && !advReports.interactive && (
          <FeatureLocked
            capability="reports.advanced"
            title={T.advancedReportsLockedTitle || "Calendar view is on a higher plan"}
            message={T.advancedReportsLockedMsg || "Upgrade to unlock the attendance calendar and heatmap. The Daily Report stays available on your current plan."}
          />
        )}
        {attView === "calendar" && advReports.interactive && (
          <div style={{ background: C.surface, borderRadius: C.r, border: `1px solid ${C.border}`, padding: 24 }}>
            {/* Legend */}
            <div style={{ display: "flex", alignItems: "center", gap: 16, marginBottom: 20, flexWrap: "wrap" }}>
              <span style={{ fontSize: 12, fontWeight: 700, color: C.textSub }}>{T.attendanceRate || "Attendance Rate"}:</span>
              {[
                { label: "≥ 80%", bg: "#DCFCE7", color: "#166534", border: "#86EFAC" },
                { label: "50–79%", bg: "#FEF9C3", color: "#854D0E", border: "#FDE047" },
                { label: "< 50%", bg: "#FEE2E2", color: "#991B1B", border: "#FCA5A5" },
                { label: "Weekend", bg: "#F3F4F6", color: "#9CA3AF", border: "#E5E7EB" },
              ].map(l => (
                <div key={l.label} style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  <div style={{ width: 14, height: 14, borderRadius: 4, background: l.bg, border: `1px solid ${l.border}` }} />
                  <span style={{ fontSize: 11, color: C.textSub }}>{l.label}</span>
                </div>
              ))}
            </div>

            {/* Day-of-week headers */}
            <div style={{ display: "grid", gridTemplateColumns: "repeat(7,1fr)", gap: 6, marginBottom: 8 }}>
              {DOW.map(d => (
                <div key={d} style={{ textAlign: "center", fontSize: 11, fontWeight: 800, color: C.textSub, padding: "6px 0", textTransform: "uppercase", letterSpacing: ".5px" }}>{d}</div>
              ))}
            </div>

            {/* Calendar grid */}
            {calRows.map((row, ri) => (
              <div key={ri} style={{ display: "grid", gridTemplateColumns: "repeat(7,1fr)", gap: 6, marginBottom: 6 }}>
                {(row.length < 7 ? [...row, ...Array(7 - row.length).fill(null)] : row).map((d, ci) => {
                  if (!d) return <div key={ci} />;
                  const hc = heatColor(d);
                  const dayNum = new Date(d.date + "T00:00:00").getDate();
                  const isToday = d.date === todayIso();
                  return (
                    <div key={d.date} onClick={() => calDayClick(d)}
                      style={{ background: hc.bg, border: `1px solid ${isToday ? C.brand : hc.border}`, borderRadius: 8, padding: "10px 8px", cursor: "pointer", textAlign: "center", transition: "all .12s", boxShadow: isToday ? `0 0 0 2px ${C.brand}40` : "none" }}
                      onMouseEnter={e => { e.currentTarget.style.transform = "scale(1.04)"; e.currentTarget.style.zIndex = "1"; }}
                      onMouseLeave={e => { e.currentTarget.style.transform = ""; e.currentTarget.style.zIndex = ""; }}>
                      <div style={{ fontSize: 15, fontWeight: 800, color: isToday ? C.brand : hc.text, marginBottom: 4 }}>{dayNum}</div>
                      {!d.is_weekend ? (
                        <>
                          <div style={{ fontSize: 10, fontWeight: 700, color: "#059669" }}>{d.present}P</div>
                          <div style={{ fontSize: 10, color: "#DC2626" }}>{d.absent}A</div>
                        </>
                      ) : d.present > 0 ? (
                        // Designated off-day (weekend/holiday) that people actually
                        // worked — surface the present count instead of masking it as
                        // a plain "Off", so weekend operations are visible at a glance.
                        <>
                          <div style={{ fontSize: 10, fontWeight: 700, color: "#059669" }}>{d.present}P</div>
                          <div style={{ fontSize: 9, fontWeight: 700, color: "#9CA3AF", textTransform: "uppercase", letterSpacing: ".4px" }}>Off</div>
                        </>
                      ) : (
                        <div style={{ fontSize: 10, color: "#9CA3AF" }}>Off</div>
                      )}
                    </div>
                  );
                })}
              </div>
            ))}

            {/* Click hint */}
            <p style={{ fontSize: 11, color: C.textLight, marginTop: 12 }}>{T.clickDayHint || "Click a day to view detailed attendance for that date."}</p>
          </div>
        )}
      </>
    );
  };

  // ── DAILY ATTENDANCE ──────────────────────────────────────────────────
  const renderDaily = () => {
    const totals = daily?.totals || { worked: 0, late: 0, on_leave: 0, absent: 0, total: 0 };
    const rows = sortByName((daily?.[dailyTab]) || []);
    const tabs = [
      { k: "worked",   l: T.workedEmployees || "Worked Employees",   icon: "check",   c: "#22C55E", bg: "#F0FDF4" },
      { k: "late",     l: T.lateClockInEmployees || "Late Clock In Employees", icon: "clock", c: "#F59E0B", bg: "#FFFAEB" },
      { k: "on_leave", l: T.onLeaveEmployees || "On Leave Employees", icon: "calendar", c: "#7C3AED", bg: "#F5F3FF" },
      { k: "absent",   l: T.absentEmployees || "Absent Employees",   icon: "x",       c: "#EF4444", bg: "#FEF3F2" },
    ];

    const handleExport = () => {
      let headers, csvRows;
      if (dailyTab === "worked" || dailyTab === "late") {
        headers = ["Employee Name", "Date", "Clock-In", "Clock-Out", "Duration"];
        csvRows = rows.map(r => [r.name, fmtDate(r.date), fmtTime(r.clock_in), r.clock_out ? fmtTime(r.clock_out) : "Working On", r.duration]);
      } else if (dailyTab === "on_leave") {
        headers = ["Employee Name", "Date", "Leave Type", "From", "To", "Description"];
        csvRows = rows.map(r => [r.name, fmtDate(r.date), r.leave_type, r.from_date, r.to_date, r.description]);
      } else {
        headers = ["Employee Name", "Date", "Status"];
        csvRows = rows.map(r => [r.name, fmtDate(r.date), "Absent"]);
      }
      exportCSV(`daily_attendance_${dailyTab}_${date}.csv`, headers, csvRows);
    };

    return (
      <>
        {/* Tab strip */}
        <div style={{ background: C.surface, borderRadius: C.r, border: `1px solid ${C.border}`, padding: "0 24px", marginBottom: 16, display: "flex", alignItems: "center", gap: 28 }}>
          {tabs.map(t => {
            const count = totals[t.k] || 0;
            const active = dailyTab === t.k;
            return (
              <button key={t.k} onClick={() => setDailyTab(t.k)}
                style={{ padding: "16px 0", border: "none", background: "transparent", fontWeight: 700, fontSize: 14, cursor: "pointer", fontFamily: "inherit", color: active ? C.brand : C.textSub, borderBottom: active ? `3px solid ${C.brand}` : "3px solid transparent", display: "flex", alignItems: "center", gap: 8 }}>
                {t.l}
                <span style={{ background: active ? C.brandBg : C.bg, color: active ? C.brand : C.textSub, fontSize: 11, fontWeight: 800, padding: "2px 8px", borderRadius: 99, border: `1px solid ${active ? C.brandBdr : C.border}` }}>{count}</span>
              </button>
            );
          })}
        </div>

        <FilterBar onExport={handleExport}>
          {empFilterSelect()}
          <DateInput value={date} onChange={setDate} />
        </FilterBar>

        <div style={{ ...card(), padding: 0, overflow: "hidden" }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ background: C.bg }}>
                <Th>Employee Name</Th>
                <Th>Date</Th>
                {(dailyTab === "worked" || dailyTab === "late") && (<>
                  <Th>Clock-In</Th>
                  <Th>Clock-Out</Th>
                  <Th>Duration</Th>
                </>)}
                {dailyTab === "on_leave" && (<>
                  <Th>Leave Type</Th>
                  <Th>From</Th>
                  <Th>To</Th>
                </>)}
                {dailyTab === "absent" && (<Th>Status</Th>)}
              </tr>
            </thead>
            <tbody>
              {loading && rows.length === 0 ? (
                <tr><td colSpan={6} style={{ padding: "60px", textAlign: "center", color: C.textLight, fontSize: 13 }}>Loading…</td></tr>
              ) : rows.length === 0 ? <Empty /> : rows.map(r => (
                <tr key={`${r.id}-${r.date}`} style={{ transition: "background .15s" }}
                  onMouseEnter={e => e.currentTarget.style.background = C.bg}
                  onMouseLeave={e => e.currentTarget.style.background = ""}>
                  <Td>
                    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                      <Avatar name={r.name} />
                      <span style={{ fontWeight: 600 }}>{r.name}</span>
                    </div>
                  </Td>
                  <Td style={{ color: C.textSub }}>{fmtDate(r.date)}</Td>
                  {(dailyTab === "worked" || dailyTab === "late") && (<>
                    <Td style={{ color: C.text, fontWeight: 600 }}>{fmtTime(r.clock_in)}</Td>
                    <Td style={{ color: r.clock_out ? C.text : "#F59E0B", fontWeight: r.clock_out ? 600 : 700 }}>
                      {r.clock_out ? fmtTime(r.clock_out) : "Working On"}
                    </Td>
                    <Td style={{ color: C.text, fontWeight: 700 }}>{r.duration}</Td>
                  </>)}
                  {dailyTab === "on_leave" && (<>
                    <Td><span style={{ background: "#F5F3FF", color: "#7C3AED", padding: "3px 10px", borderRadius: 99, fontSize: 11, fontWeight: 700 }}>{r.leave_type}</span></Td>
                    <Td style={{ color: C.textSub }}>{fmtDate(r.from_date)}</Td>
                    <Td style={{ color: C.textSub }}>{fmtDate(r.to_date)}</Td>
                  </>)}
                  {dailyTab === "absent" && (
                    <Td><span style={{ background: "#FEF3F2", color: "#EF4444", padding: "3px 10px", borderRadius: 99, fontSize: 11, fontWeight: 700, border: "1px solid #FECACA" }}>Absent</span></Td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </>
    );
  };

  // ── MONTHLY ATTENDANCE ────────────────────────────────────────────────
  const renderMonthly = () => {
    const rows = sortByName(monthlyData?.rows || []);
    // "0.5" half-days display as "0.5", whole days as "1" — strip the
    // trailing ".0" so payroll exports stay clean.
    const fmtDays = (n) => {
      const v = Number(n || 0);
      return Number.isInteger(v) ? String(v) : v.toFixed(1);
    };
    const handleExport = async () => {
      const XLSX = await loadXLSX();
      // Build title strings from selected month (YYYY-MM)
      const [yStr, mStr] = String(month || monthIso()).split("-");
      const yNum = Number(yStr);
      const mNum = Number(mStr);
      const monthLabel = (!isNaN(yNum) && !isNaN(mNum))
        ? new Date(yNum, mNum - 1, 1).toLocaleDateString("en-US", { month: "long", year: "numeric" })
        : month;
      const lastDay = (!isNaN(yNum) && !isNaN(mNum)) ? new Date(yNum, mNum, 0).getDate() : 31;
      const periodStart = (!isNaN(yNum) && !isNaN(mNum))
        ? new Date(yNum, mNum - 1, 1).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" })
        : "";
      const periodEnd = (!isNaN(yNum) && !isNaN(mNum))
        ? new Date(yNum, mNum - 1, lastDay).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" })
        : "";

      const headers = ["Employee Name", "Total Working Days", "Present Days", "Leave Days", "Absent Days"];
      const dataRows = rows.map(r => [
        r.name || "-",
        Number(r.working_days ?? 0),
        Number(r.present_days ?? 0),
        Number(r.leave_days ?? 0),
        Number(r.absent_days ?? 0),
      ]);

      const wsData = [
        [`Monthly Attendance Report — ${monthLabel}`],
        [`Total Employees: ${rows.length} | Report Period: ${periodStart} – ${periodEnd}`],
        [],
        headers,
        ...dataRows,
      ];

      const ws = XLSX.utils.aoa_to_sheet(wsData);
      ws["!cols"] = [{ wch: 26 }, { wch: 20 }, { wch: 16 }, { wch: 14 }, { wch: 14 }];
      // Merge title and subtitle across all 5 columns
      ws["!merges"] = [
        { s: { r: 0, c: 0 }, e: { r: 0, c: 4 } },
        { s: { r: 1, c: 0 }, e: { r: 1, c: 4 } },
      ];
      // Taller title rows so the merged headings have breathing room
      ws["!rows"] = [{ hpt: 26 }, { hpt: 18 }];

      const wb = XLSX.utils.book_new();
      XLSX.utils.book_append_sheet(wb, ws, "Attendance");
      XLSX.writeFile(wb, `monthly_attendance_${month}.xlsx`);
    };
    return (
      <>
        <FilterBar onExport={handleExport}>
          {empFilterSelect()}
          <MonthInput />
        </FilterBar>
        <div style={{ ...card(), padding: 0, overflow: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead><tr style={{ background: C.bg }}>
              <Th>Employee Name</Th>
              <Th>Total Working Days</Th>
              <Th>Present Days</Th>
              <Th>Leave Days</Th>
              <Th>Absent Days</Th>
            </tr></thead>
            <tbody>
              {rows.length === 0 ? <Empty /> : rows.map(r => (
                <tr key={r.id}>
                  <Td><div style={{ display: "flex", alignItems: "center", gap: 10 }}><Avatar name={r.name} /><span style={{ fontWeight: 600 }}>{r.name}</span></div></Td>
                  <Td><span style={{ color: C.text, fontWeight: 700 }}>{r.working_days ?? 0}</span></Td>
                  <Td><span style={{ color: "#22C55E", fontWeight: 700 }}>{r.present_days ?? 0}</span></Td>
                  <Td><span style={{ color: "#7C3AED", fontWeight: 700 }}>{fmtDays(r.leave_days)}</span></Td>
                  <Td><span style={{ color: "#EF4444", fontWeight: 700 }}>{r.absent_days ?? 0}</span></Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </>
    );
  };

  // ── MONTHLY IN/OUT ────────────────────────────────────────────────────
  const renderInOut = () => {
    const rows = sortByName(inOutData?.rows || []);
    const handleExport = () => {
      const headers = ["Employee", "Date", "Clock-In", "Clock-Out", "Duration"];
      const csv = rows.map(r => [r.name, fmtDate(r.date), fmtTime(r.clock_in), r.clock_out ? fmtTime(r.clock_out) : "—", r.duration]);
      exportCSV(`monthly_inout_${month}.csv`, headers, csv);
    };
    return (
      <>
        <FilterBar onExport={handleExport}>{empFilterSelect()}<MonthInput /></FilterBar>
        <div style={{ ...card(), padding: 0, overflow: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead><tr style={{ background: C.bg }}>
              <Th>Employee</Th><Th>Date</Th><Th>Clock-In</Th><Th>Clock-Out</Th><Th>Duration</Th>
            </tr></thead>
            <tbody>
              {rows.length === 0 ? <Empty /> : rows.map((r, i) => (
                <tr key={`${r.id}-${r.date}-${i}`}>
                  <Td><div style={{ display: "flex", alignItems: "center", gap: 10 }}><Avatar name={r.name} /><span style={{ fontWeight: 600 }}>{r.name}</span></div></Td>
                  <Td style={{ color: C.textSub }}>{fmtDate(r.date)}</Td>
                  <Td style={{ fontWeight: 600 }}>{fmtTime(r.clock_in)}</Td>
                  <Td style={{ color: r.clock_out ? C.text : "#F59E0B", fontWeight: 600 }}>{r.clock_out ? fmtTime(r.clock_out) : "Working On"}</Td>
                  <Td style={{ fontWeight: 700, color: "#22C55E" }}>{r.duration}</Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </>
    );
  };

  // ── LATE CLOCK-IN ─────────────────────────────────────────────────────
  const renderLate = () => {
    const rows = sortByName(lateData?.rows || []);
    const handleExport = () => {
      const headers = ["Employee", "Date", "Clock-In", "Scheduled", "Late By"];
      const csv = rows.map(r => [r.name, fmtDate(r.date), fmtTime(r.clock_in), r.scheduled, r.late_by]);
      exportCSV(`late_clockin_${date}.csv`, headers, csv);
    };
    return (
      <>
        <FilterBar onExport={handleExport}>{empFilterSelect()}<DateInput value={date} onChange={setDate} /></FilterBar>
        <div style={{ ...card(), padding: 0, overflow: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead><tr style={{ background: C.bg }}>
              <Th>Employee</Th><Th>Date</Th><Th>Clock-In</Th><Th>Scheduled</Th><Th>Late By</Th>
            </tr></thead>
            <tbody>
              {rows.length === 0 ? <Empty msg="No late clock-ins for this date" /> : rows.map(r => (
                <tr key={r.id}>
                  <Td><div style={{ display: "flex", alignItems: "center", gap: 10 }}><Avatar name={r.name} /><span style={{ fontWeight: 600 }}>{r.name}</span></div></Td>
                  <Td style={{ color: C.textSub }}>{fmtDate(r.date)}</Td>
                  <Td style={{ fontWeight: 600, color: "#F59E0B" }}>{fmtTime(r.clock_in)}</Td>
                  <Td style={{ color: C.textSub }}>{r.scheduled}</Td>
                  <Td><span style={{ background: "#FEF3F2", color: "#EF4444", padding: "3px 10px", borderRadius: 99, fontSize: 11, fontWeight: 800 }}>{r.late_by}</span></Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </>
    );
  };

  // ── OVERTIME ──────────────────────────────────────────────────────────
  const renderOT = () => {
    const rows = sortByName(otData?.rows || []);
    const summary = sortByName(otData?.summary || []);
    const handleExport = () => {
      const headers = ["Employee", "Date", "Worked", "Overtime"];
      const csv = rows.map(r => [r.name, fmtDate(r.date), r.worked, r.overtime]);
      exportCSV(`overtime_${fromDate}_${toDate}.csv`, headers, csv);
    };
    return (
      <>
        <FilterBar onExport={handleExport}>
          {empFilterSelect()}
          <DateInput value={fromDate} onChange={setFromDate} label="From" />
          <DateInput value={toDate}   onChange={setToDate}   label="To"   />
        </FilterBar>

        {summary.length > 0 && (
          <div style={{ ...card(), marginBottom: 16, padding: 0, overflow: "auto" }}>
            <p style={{ padding: "16px 20px", fontSize: 13, fontWeight: 700, color: C.text, borderBottom: `1px solid ${C.borderLight}` }}>Per-Employee Summary</p>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead><tr style={{ background: C.bg }}>
                <Th>Employee</Th><Th>Days Worked</Th><Th>Total Worked</Th><Th>Total Overtime</Th>
              </tr></thead>
              <tbody>
                {summary.map(r => (
                  <tr key={r.id}>
                    <Td><div style={{ display: "flex", alignItems: "center", gap: 10 }}><Avatar name={r.name} /><span style={{ fontWeight: 600 }}>{r.name}</span></div></Td>
                    <Td>{r.days_worked}</Td>
                    <Td style={{ fontWeight: 700 }}>{r.total_worked}</Td>
                    <Td><span style={{ color: r.overtime_secs > 0 ? "#22C55E" : C.textLight, fontWeight: 700 }}>{r.total_overtime}</span></Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div style={{ ...card(), padding: 0, overflow: "auto" }}>
          <p style={{ padding: "16px 20px", fontSize: 13, fontWeight: 700, color: C.text, borderBottom: `1px solid ${C.borderLight}` }}>Overtime Days</p>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead><tr style={{ background: C.bg }}>
              <Th>Employee</Th><Th>Date</Th><Th>Worked</Th><Th>Overtime</Th>
            </tr></thead>
            <tbody>
              {rows.length === 0 ? <Empty msg="No overtime in this period" /> : rows.map((r, i) => (
                <tr key={`${r.id}-${r.date}-${i}`}>
                  <Td><div style={{ display: "flex", alignItems: "center", gap: 10 }}><Avatar name={r.name} /><span style={{ fontWeight: 600 }}>{r.name}</span></div></Td>
                  <Td style={{ color: C.textSub }}>{fmtDate(r.date)}</Td>
                  <Td>{r.worked}</Td>
                  <Td><span style={{ color: "#22C55E", fontWeight: 800 }}>{r.overtime}</span></Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </>
    );
  };

  // ── CUSTOM REPORT ─────────────────────────────────────────────────────
  const renderCustom = () => {
    const rows = sortByName(customData?.rows || []);
    const handleExport = () => {
      const headers = ["Employee", "Team", "Days Worked", "Total Worked", "Active", "Break", "First Clock-In", "Last Clock-Out"];
      const csv = rows.map(r => [r.name, r.team || "—", r.days_worked, r.total_duration, r.active_duration, r.break_duration, r.first_clock_in ? fmtIST(r.first_clock_in) : "—", r.last_clock_out ? fmtIST(r.last_clock_out) : "—"]);
      exportCSV(`custom_report_${fromDate}_${toDate}.csv`, headers, csv);
    };
    return (
      <>
        <FilterBar onExport={handleExport}>
          {empFilterSelect()}
          <DateInput value={fromDate} onChange={setFromDate} label="From" />
          <DateInput value={toDate}   onChange={setToDate}   label="To"   />
        </FilterBar>
        <div style={{ ...card(), padding: 0, overflow: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead><tr style={{ background: C.bg }}>
              <Th>Employee</Th><Th>Team</Th><Th>Days</Th><Th>Worked</Th><Th>Active</Th><Th>Break</Th><Th>First In</Th><Th>Last Out</Th>
            </tr></thead>
            <tbody>
              {rows.length === 0 ? <Empty /> : rows.map(r => (
                <tr key={r.id}>
                  <Td><div style={{ display: "flex", alignItems: "center", gap: 10 }}><Avatar name={r.name} /><span style={{ fontWeight: 600 }}>{r.name}</span></div></Td>
                  <Td style={{ color: C.textSub }}>{r.team || "—"}</Td>
                  <Td>{r.days_worked}</Td>
                  <Td style={{ fontWeight: 700 }}>{r.total_duration}</Td>
                  <Td style={{ color: "#22C55E", fontWeight: 700 }}>{r.active_duration}</Td>
                  <Td style={{ color: "#F59E0B" }}>{r.break_duration}</Td>
                  <Td style={{ color: C.textSub, fontSize: 12 }}>{r.first_clock_in ? fmtIST(r.first_clock_in) : "—"}</Td>
                  <Td style={{ color: C.textSub, fontSize: 12 }}>{r.last_clock_out ? fmtIST(r.last_clock_out) : "—"}</Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </>
    );
  };

  // ── NEW JOINERS ───────────────────────────────────────────────────────
  // How many employees joined within a date range (by official start date).
  const renderJoiners = () => {
    const rows = sortByName(joinersData?.rows || []);
    const count = joinersData?.count ?? rows.length;
    const handleExport = () => {
      const headers = ["Employee", "Email", "Employee ID", "Department", "Designation", "Joined On"];
      const csv = rows.map(r => [r.name, r.email, r.employee_id || "—", r.department || "—", r.designation || "—", fmtDate(r.joined_on)]);
      exportCSV(`new_joiners_${fromDate}_${toDate}.csv`, headers, csv);
    };
    return (
      <>
        <FilterBar onExport={handleExport}>
          <DateInput value={fromDate} onChange={setFromDate} label="From" />
          <DateInput value={toDate}   onChange={setToDate}   label="To"   />
        </FilterBar>

        {/* Headline count */}
        <div style={{ ...card(), marginBottom: 16, display: "flex", alignItems: "center", gap: 16 }}>
          <div style={{ width: 44, height: 44, borderRadius: 12, background: "#EEF2FF", display: "flex", alignItems: "center", justifyContent: "center" }}>
            <Icon n="user_plus" s={22} c="#6366F1" />
          </div>
          <div>
            <div style={{ fontSize: 30, fontWeight: 800, color: "#6366F1", lineHeight: 1, fontVariantNumeric: "tabular-nums" }}>{count}</div>
            <div style={{ fontSize: 12, color: C.textSub, fontWeight: 600, marginTop: 4 }}>
              {count === 1 ? "employee joined" : "employees joined"} between {fmtDate(fromDate)} and {fmtDate(toDate)}
            </div>
          </div>
        </div>

        <div style={{ ...card(), padding: 0, overflow: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead><tr style={{ background: C.bg }}>
              <Th>Employee</Th><Th>Employee ID</Th><Th>Department</Th><Th>Designation</Th><Th>Joined On</Th>
            </tr></thead>
            <tbody>
              {loading && rows.length === 0 ? (
                <tr><td colSpan={5} style={{ padding: 60, textAlign: "center", color: C.textLight, fontSize: 13 }}>Loading…</td></tr>
              ) : rows.length === 0 ? <Empty msg="No employees joined in this period" /> : rows.map(r => (
                <tr key={r.id}>
                  <Td>
                    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                      <Avatar name={r.name} />
                      <div style={{ minWidth: 0 }}>
                        <div style={{ fontWeight: 600 }}>{r.name}</div>
                        <div style={{ fontSize: 11, color: C.textSub }}>{r.email}</div>
                      </div>
                    </div>
                  </Td>
                  <Td style={{ color: C.textSub }}>{r.employee_id || "—"}</Td>
                  <Td style={{ color: C.textSub }}>{r.department || "—"}</Td>
                  <Td style={{ color: C.textSub }}>{r.designation || "—"}</Td>
                  <Td style={{ fontWeight: 700, color: "#22C55E" }}>{fmtDate(r.joined_on)}</Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </>
    );
  };

  // ── EMPLOYEE DIRECTORY ────────────────────────────────────────────────
  // Full roster: name, employee ID, designation, employment type, join date.
  const renderDirectory = () => {
    const rows = sortByName(dirData?.rows || []);
    const count = dirData?.count ?? rows.length;
    const fullCount = rows.filter(r => (r.employment_type || "full_time") === "full_time").length;
    const partCount = rows.length - fullCount;

    const handleExport = async () => {
      const mod = await loadXLSXStyle();
      const XLSX = mod.utils ? mod : mod.default;   // CJS interop: utils may sit on default
      const headers = ["#", "Employee Name", "Employee ID", "Designation", "Employment Type", "Date of Joining"];
      const dataRows = rows.map((r, i) => [
        i + 1,
        r.name || "—",
        r.employee_id || "—",
        r.designation || "—",
        fmtEmpType(r.employment_type),
        r.joined_on ? fmtDate(r.joined_on) : "—",
      ]);

      const wsData = [
        ["Employee Directory"],
        [`Total Employees: ${rows.length}   •   Full Time: ${fullCount}   •   Part Time: ${partCount}`],
        [],
        headers,
        ...dataRows,
      ];
      const ws = XLSX.utils.aoa_to_sheet(wsData);
      ws["!cols"] = [{ wch: 6 }, { wch: 28 }, { wch: 16 }, { wch: 26 }, { wch: 18 }, { wch: 18 }];
      ws["!rows"] = [{ hpt: 30 }, { hpt: 20 }, { hpt: 8 }];
      ws["!merges"] = [
        { s: { r: 0, c: 0 }, e: { r: 0, c: 5 } },
        { s: { r: 1, c: 0 }, e: { r: 1, c: 5 } },
      ];

      const BRAND = "1D4ED8", BRAND_DK = "1E3A8A", ZEBRA = "EFF4FF", WHITE = "FFFFFF";
      const border = { style: "thin", color: { rgb: "D0D7E2" } };
      const allBorders = { top: border, bottom: border, left: border, right: border };
      const set = (addr, style) => { if (ws[addr]) ws[addr].s = style; };

      // Title
      set("A1", { font: { bold: true, sz: 18, color: { rgb: WHITE } }, fill: { fgColor: { rgb: BRAND_DK } }, alignment: { horizontal: "center", vertical: "center" } });
      // Subtitle
      set("A2", { font: { bold: true, sz: 11, color: { rgb: WHITE } }, fill: { fgColor: { rgb: BRAND } }, alignment: { horizontal: "center", vertical: "center" } });

      // Header row (row 4 → index 3)
      const colLetters = ["A", "B", "C", "D", "E", "F"];
      colLetters.forEach(c => set(`${c}4`, {
        font: { bold: true, sz: 12, color: { rgb: WHITE } },
        fill: { fgColor: { rgb: BRAND } },
        alignment: { horizontal: "center", vertical: "center" },
        border: allBorders,
      }));

      // Data rows — zebra striping + centered meta columns
      dataRows.forEach((_, i) => {
        const rowNum = 5 + i;            // 1-based Excel row
        const zebra = i % 2 === 1;
        colLetters.forEach((c, ci) => set(`${c}${rowNum}`, {
          font: { sz: 11, color: { rgb: "1F2937" }, bold: ci === 1 },
          fill: { fgColor: { rgb: zebra ? ZEBRA : WHITE } },
          alignment: { horizontal: ci === 1 || ci === 3 ? "left" : "center", vertical: "center" },
          border: allBorders,
        }));
      });

      const wb = XLSX.utils.book_new();
      XLSX.utils.book_append_sheet(wb, ws, "Employees");
      XLSX.writeFile(wb, `employee_directory_${todayIso()}.xlsx`);
    };

    const TypeBadge = ({ t }) => {
      const part = (t || "full_time") === "part_time";
      return (
        <span style={{
          background: part ? "#FFFAEB" : "#EEF2FF", color: part ? "#B45309" : "#4338CA",
          border: `1px solid ${part ? "#FDE68A" : "#C7D2FE"}`,
          padding: "3px 10px", borderRadius: 99, fontSize: 11, fontWeight: 700, whiteSpace: "nowrap",
        }}>{fmtEmpType(t)}</span>
      );
    };

    const kpis = [
      { label: "Total Employees", value: count,     color: "#4338CA", bg: "#EEF2FF", icon: "users" },
      { label: "Full Time",       value: fullCount, color: "#059669", bg: "#ECFDF5", icon: "check" },
      { label: "Part Time",       value: partCount, color: "#B45309", bg: "#FFFAEB", icon: "clock" },
    ];

    return (
      <>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 12, marginBottom: 16 }}>
          {kpis.map(k => (
            <div key={k.label} style={{ ...card({ padding: "16px 18px" }), display: "flex", alignItems: "center", gap: 12 }}>
              <div style={{ width: 40, height: 40, borderRadius: 10, background: k.bg, display: "flex", alignItems: "center", justifyContent: "center" }}>
                <Icon n={k.icon} s={18} c={k.color} />
              </div>
              <div>
                <div style={{ fontSize: 26, fontWeight: 800, color: k.color, lineHeight: 1, fontVariantNumeric: "tabular-nums" }}>{k.value}</div>
                <div style={{ fontSize: 11, color: C.textSub, fontWeight: 700, textTransform: "uppercase", letterSpacing: ".5px", marginTop: 4 }}>{k.label}</div>
              </div>
            </div>
          ))}
        </div>

        <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 16 }}>
          <button onClick={handleExport} style={{ display: "flex", alignItems: "center", gap: 6, padding: "8px 18px", borderRadius: 8, border: `1px solid #22C55E`, background: "#22C55E", color: "#fff", fontWeight: 700, fontSize: 13, cursor: "pointer", fontFamily: "inherit" }}>
            <Icon n="download" s={14} c="#fff" /> Export Excel
          </button>
        </div>

        <div style={{ ...card(), padding: 0, overflow: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead><tr style={{ background: C.bg }}>
              <Th>Employee Name</Th><Th>Employee ID</Th><Th>Designation</Th><Th>Employment Type</Th><Th>Date of Joining</Th>
            </tr></thead>
            <tbody>
              {loading && rows.length === 0 ? (
                <tr><td colSpan={5} style={{ padding: 60, textAlign: "center", color: C.textLight, fontSize: 13 }}>Loading…</td></tr>
              ) : rows.length === 0 ? <Empty msg="No employees found" /> : rows.map(r => (
                <tr key={r.id}>
                  <Td>
                    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                      <Avatar name={r.name} />
                      <div style={{ minWidth: 0 }}>
                        <div style={{ fontWeight: 600 }}>{r.name}</div>
                        <div style={{ fontSize: 11, color: C.textSub }}>{r.email}</div>
                      </div>
                    </div>
                  </Td>
                  <Td style={{ color: C.textSub }}>{r.employee_id || "—"}</Td>
                  <Td style={{ color: C.textSub }}>{r.designation || "—"}</Td>
                  <Td><TypeBadge t={r.employment_type} /></Td>
                  <Td style={{ fontWeight: 700, color: r.joined_on ? "#22C55E" : C.textLight }}>{r.joined_on ? fmtDate(r.joined_on) : "—"}</Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </>
    );
  };

  // ── WORK ACTIVITY LOG ─────────────────────────────────────────────────
  const renderActivity = () => {
    const rows = sortByName(activityData?.rows || []);
    const handleExport = () => {
      const headers = ["Employee", "Event", "Time", "Duration", "Reason"];
      const csv = rows.map(r => [r.name, r.label, fmtIST(r.timestamp), r.duration || "", r.reason || ""]);
      exportCSV(`work_activity_${date}.csv`, headers, csv);
    };
    const eventColor = (t) => ({
      clock_in:    { c: "#22C55E", bg: "#F0FDF4" },
      clock_out:   { c: "#EF4444", bg: "#FEF3F2" },
      break_start: { c: "#F59E0B", bg: "#FFFAEB" },
      break_end:   { c: "#3B82F6", bg: "#EFF6FF" },
    }[t] || { c: C.textSub, bg: C.bg });
    return (
      <>
        <FilterBar onExport={handleExport}>{empFilterSelect()}<DateInput value={date} onChange={setDate} /></FilterBar>
        <div style={{ ...card(), padding: 0, overflow: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead><tr style={{ background: C.bg }}>
              <Th>Employee</Th><Th>Event</Th><Th>Time</Th><Th>Duration</Th><Th>Reason</Th>
            </tr></thead>
            <tbody>
              {rows.length === 0 ? <Empty /> : rows.map((r, i) => {
                const ec = eventColor(r.type);
                return (
                  <tr key={`${r.id}-${i}-${r.type}`}>
                    <Td><div style={{ display: "flex", alignItems: "center", gap: 10 }}><Avatar name={r.name} /><span style={{ fontWeight: 600 }}>{r.name}</span></div></Td>
                    <Td><span style={{ background: ec.bg, color: ec.c, padding: "3px 10px", borderRadius: 99, fontSize: 11, fontWeight: 700 }}>{r.label}</span></Td>
                    <Td style={{ fontWeight: 600 }}>{fmtIST(r.timestamp)}</Td>
                    <Td>{r.duration || "—"}</Td>
                    <Td style={{ color: C.textSub, fontSize: 12 }}>{r.reason || "—"}</Td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </>
    );
  };

  // ── INPUT ACTIVITY ────────────────────────────────────────────────────
  const renderInput = () => {
    const rows = sortByName(inputData?.rows || []);
    const handleExport = () => {
      const headers = ["Employee", "Date", "Key Presses", "Mouse Clicks", "Idle"];
      const csv = rows.map(r => [r.name, fmtDate(r.date), r.key_presses, r.mouse_clicks, r.idle_duration]);
      exportCSV(`input_activity_${date}.csv`, headers, csv);
    };
    return (
      <>
        <FilterBar onExport={handleExport}>{empFilterSelect()}<DateInput value={date} onChange={setDate} /></FilterBar>
        <div style={{ ...card(), padding: 0, overflow: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead><tr style={{ background: C.bg }}>
              <Th>Employee</Th><Th>Date</Th><Th>Key Presses</Th><Th>Mouse Clicks</Th><Th>Idle Time</Th><Th>Samples</Th>
            </tr></thead>
            <tbody>
              {rows.length === 0 ? <Empty /> : rows.map(r => (
                <tr key={r.id}>
                  <Td><div style={{ display: "flex", alignItems: "center", gap: 10 }}><Avatar name={r.name} /><span style={{ fontWeight: 600 }}>{r.name}</span></div></Td>
                  <Td style={{ color: C.textSub }}>{fmtDate(r.date)}</Td>
                  <Td style={{ fontWeight: 700, color: C.brand }}>{r.key_presses.toLocaleString()}</Td>
                  <Td style={{ fontWeight: 700, color: "#7C3AED" }}>{r.mouse_clicks.toLocaleString()}</Td>
                  <Td style={{ color: "#F59E0B", fontWeight: 600 }}>{r.idle_duration}</Td>
                  <Td style={{ color: C.textSub }}>{r.samples}</Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </>
    );
  };

  // ── ACCESS LOCATION REPORT ───────────────────────────────────────────
  const renderLoginIp = () => {
    const allRows = sortByName(loginIpData?.rows || []);

    const displayRows = loginIpEmp === "all"
      ? allRows
      : allRows.filter(r => String(r.id) === String(loginIpEmp) || String(r.user_id) === String(loginIpEmp));

    return (
      <>
        {/* Filter bar with local employee select — isolated from shared empFilter */}
        <div style={{ background: C.surface, borderRadius: C.r, border: `1px solid ${C.border}`, padding: "14px 18px", display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap", marginBottom: 16 }}>
          <select value={loginIpEmp} onChange={e => setLoginIpEmp(e.target.value)}
            style={{ padding: "9px 14px", borderRadius: 8, border: `1.5px solid ${loginIpEmp !== "all" ? C.brand : C.textSub}`, background: loginIpEmp !== "all" ? C.brand : C.surface, color: loginIpEmp !== "all" ? "#fff" : C.text, fontSize: 13, fontWeight: 600, cursor: "pointer", fontFamily: "inherit", outline: "none", minWidth: 200, flexShrink: 0, boxShadow: "0 1px 3px rgba(0,0,0,0.1)" }}>
            <option value="all">All Employees</option>
            {employees.map(e => (
              <option key={e.id} value={e.id}>{e.name}</option>
            ))}
          </select>
          <span style={{ fontSize: 12, color: C.textSub }}>
            {displayRows.length} {displayRows.length === 1 ? "employee" : "employees"}
          </span>
          <div style={{ flex: 1 }} />
          <button onClick={() => {
              const headers = ["Employee Name", "No. of IPs", "IP Address", "City", "Region", "Country", "ISP", "Last Login"];
              const csv = displayRows.flatMap(r =>
                (r.ip_details || (r.ip_addresses || []).map(ip => ({ ip }))).map(d => [
                  r.name, r.number_of_ips, d.ip || "", d.city || "", d.region || "", d.country || "", d.isp || "",
                  r.last_login ? fmtIST(r.last_login) : "—",
                ])
              );
              exportCSV(`access_location_report.csv`, headers, csv);
            }}
            style={{ display: "flex", alignItems: "center", gap: 6, padding: "8px 18px", borderRadius: 8, border: `1px solid ${C.brand}`, background: C.brand, color: "#fff", fontWeight: 700, fontSize: 13, cursor: "pointer", fontFamily: "inherit" }}>
            <Icon n="download" s={14} c="#fff" /> Export
          </button>
        </div>
        <div style={{ ...card(), padding: 0, overflow: "hidden" }}>
          {/* Horizontal scroll wrapper — long IPv6 + ISP names can exceed the
              card width; without this the last columns clipped under the border
              and couldn't be scrolled into view. */}
          <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 760 }}>
            <thead>
              <tr style={{ background: C.bg }}>
                <Th>Employee Name</Th>
                <Th>No. of IPs</Th>
                <Th>IP Address</Th>
                <Th>Location</Th>
                <Th>ISP / Network</Th>
                <Th>Last Login</Th>
              </tr>
            </thead>
            <tbody>
              {displayRows.length === 0 ? (
                <Empty msg={allRows.length === 0 ? "No login records found" : "No records for selected employee"} />
              ) : displayRows.map(r => {
                const details = r.ip_details || r.ip_addresses.map(ip => ({ ip }));
                return details.map((d, idx) => (
                  <tr key={`${r.id}-${d.ip}-${idx}`} style={{ borderBottom: `1px solid ${C.borderLight}` }}>
                    {/* Show employee name + avatar only on the first IP row */}
                    {idx === 0 ? (
                      <Td rowSpan={details.length} style={{ verticalAlign: "top", borderRight: `1px solid ${C.borderLight}` }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                          <Avatar name={r.name} />
                          <div>
                            <span style={{ fontWeight: 700, display: "block" }}>{r.name}</span>
                            <span style={{ fontSize: 11, color: C.textSub }}>{r.number_of_ips} IP{r.number_of_ips !== 1 ? "s" : ""}</span>
                          </div>
                        </div>
                      </Td>
                    ) : null}
                    {idx === 0 ? (
                      <Td rowSpan={details.length} style={{ fontWeight: 700, color: C.brand, verticalAlign: "top", textAlign: "center", borderRight: `1px solid ${C.borderLight}` }}>
                        {r.number_of_ips}
                      </Td>
                    ) : null}
                    {/* IP address */}
                    <Td>
                      <span style={{ fontFamily: "Menlo, Monaco, monospace", fontSize: 11, padding: "4px 10px", borderRadius: 6, background: C.bg, border: `1px solid ${C.border}`, color: C.text, fontWeight: 600, whiteSpace: "nowrap" }}>
                        {d.ip}
                      </span>
                    </Td>
                    {/* Location */}
                    <Td>
                      {d.city || d.country ? (
                        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                          <span style={{ fontSize: 16 }}>📍</span>
                          <div>
                            {/* Region+country is reliable; city on mobile/IPv6 carrier
                                IPs is coarse across every geo provider, so lead with the
                                dependable part and flag the city as approximate. */}
                            <span style={{ fontWeight: 600, fontSize: 13, color: C.text }}>{[d.region, d.country].filter(Boolean).join(", ") || d.city}</span>
                            {d.city && <span style={{ fontSize: 11, color: C.textSub, display: "block" }}>~{d.city} (approx.)</span>}
                          </div>
                        </div>
                      ) : (
                        <span style={{ color: C.textLight, fontSize: 12 }}>Private / Unknown</span>
                      )}
                    </Td>
                    {/* ISP */}
                    <Td style={{ fontSize: 12, color: C.textSub }}>{d.isp || "—"}</Td>
                    {/* Last Login — only on first row */}
                    {idx === 0 ? (
                      <Td rowSpan={details.length} style={{ color: C.textSub, fontSize: 12, verticalAlign: "top" }}>
                        {r.last_login ? fmtIST(r.last_login) : "—"}
                      </Td>
                    ) : null}
                  </tr>
                ));
              })}
            </tbody>
          </table>
          </div>
        </div>
      </>
    );
  };

  // ── Main render ───────────────────────────────────────────────────────
  const renderContent = () => {
    switch (section) {
      case "attendance": return renderAttendance();
      case "daily":    return renderDaily();
      case "monthly":  return renderMonthly();
      case "inout":    return renderInOut();
      case "late":     return renderLate();
      case "overtime": return renderOT();
      case "custom":   return renderCustom();
      case "joiners":  return renderJoiners();
      case "directory": return renderDirectory();
      case "activity": return renderActivity();
      case "input":    return renderInput();
      case "loginip":  return renderLoginIp();
      default: return null;
    }
  };

  const activeLabel = REPORT_NAV.find(n => n.k === section)?.label || "Reporting";

  return (
    <div style={{ display: "flex", gap: 0, height: "calc(100vh - 132px)" }}>
      {/* Sub-sidebar — scrolls independently, no sticky needed */}
      <aside style={{ width: 240, flexShrink: 0, background: C.surface, border: `1px solid ${C.border}`, borderRadius: C.r, padding: "10px 6px", marginRight: 18, overflowY: "auto" }}>
        <p style={{ fontSize: 11, fontWeight: 800, color: C.textLight, letterSpacing: 1, padding: "8px 14px 6px", textTransform: "uppercase" }}>Reporting</p>
        {REPORT_NAV.map(item => {
          const active = section === item.k;
          return (
            <button key={item.k} onClick={() => setSection(item.k)}
              style={{ width: "100%", display: "flex", alignItems: "center", gap: 10, padding: "11px 14px", borderRadius: 8, border: "none", background: active ? C.brandBdr : "transparent", color: active ? C.text : C.textSub, fontWeight: active ? 700 : 500, fontSize: 13, cursor: "pointer", fontFamily: "inherit", marginBottom: 1, textAlign: "left", borderLeft: active ? `3px solid ${C.brand}` : "3px solid transparent", transition: "all .12s" }}>
              <Icon n={item.icon} s={15} c={active ? C.brand : C.textSub} />
              {item.label}
            </button>
          );
        })}
      </aside>

      {/* Content — scrolls independently */}
      <div ref={contentRef} style={{ flex: 1, minWidth: 0, overflowY: "auto", paddingRight: 4 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16 }}>
          <h2 style={{ fontSize: 22, fontWeight: 800, color: C.text }}>{activeLabel}</h2>
          {loading && <span style={{ fontSize: 11, color: C.textSub, fontWeight: 600 }}>Loading…</span>}
        </div>
        {renderContent()}
      </div>
    </div>
  );
}

/**
 * MyScheduleTab — Full workforce schedule module
 * Day / Week / Month views, shift blocks, productivity insights,
 * availability marking, leave integration, and manager team view.
 */

import React, { useState, useEffect, useCallback, useRef } from "react";
import { useTheme } from "../context/ThemeContext";
import { useLanguage } from "../context/LanguageContext";
import API from "../api/axios";
import toast from "react-hot-toast";

// ─── HELPERS ────────────────────────────────────────────────────────────────

const getRole = () => localStorage.getItem("role") || "employee";
const isManager = () => ["admin", "hr"].includes(getRole());

// Format a Date as a YYYY-MM-DD string using its LOCAL components.
// Using toISOString() here would convert to UTC and shift the date back
// a day for any user in a timezone ahead of UTC (IST, etc.) — which made
// the week grid render today under the wrong weekday column.
function toLocalISO(d) {
  const y  = d.getFullYear();
  const mo = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${y}-${mo}-${dd}`;
}

const TODAY_STR = () => toLocalISO(new Date());

function fmtDate(d) {
  return new Date(d + "T00:00:00").toLocaleDateString("en-IN", {
    day: "2-digit", month: "short", year: "numeric",
  });
}

function fmtHM(secs) {
  if (!secs || secs < 0) secs = 0;
  const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60);
  return `${h}h ${String(m).padStart(2, "0")}m`;
}

function timeToMinutes(t) {
  if (!t) return 0;
  const [h, m] = t.split(":").map(Number);
  return h * 60 + m;
}

function minutesToTime(mins) {
  const h = Math.floor(mins / 60), m = mins % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

// Return array of YYYY-MM-DD strings for a week containing `baseDate`
function getWeekDates(baseDate) {
  const d = new Date(baseDate + "T00:00:00");
  const day = d.getDay(); // 0=Sun
  const monday = new Date(d);
  monday.setDate(d.getDate() - ((day === 0 ? 7 : day) - 1));
  return Array.from({ length: 7 }, (_, i) => {
    const x = new Date(monday);
    x.setDate(monday.getDate() + i);
    return toLocalISO(x);
  });
}

// Return all dates in a calendar month grid (always 42 cells = 6 rows × 7)
function getMonthGrid(year, month) {
  const first = new Date(year, month, 1);
  const firstDow = (first.getDay() + 6) % 7; // 0=Mon
  const cells = [];
  for (let i = 0; i < firstDow; i++) {
    const d = new Date(year, month, 1 - (firstDow - i));
    cells.push({ date: toLocalISO(d), inMonth: false });
  }
  const days = new Date(year, month + 1, 0).getDate();
  for (let d = 1; d <= days; d++) {
    cells.push({ date: toLocalISO(new Date(year, month, d)), inMonth: true });
  }
  while (cells.length < 42) {
    const last = new Date(cells[cells.length - 1].date + "T00:00:00");
    last.setDate(last.getDate() + 1);
    cells.push({ date: toLocalISO(last), inMonth: false });
  }
  return cells;
}

const TASK_COLORS = {
  working:  { bg: "#DBEAFE", text: "#1D4ED8", border: "#93C5FD" },
  meeting:  { bg: "#FEF3C7", text: "#B45309", border: "#FDE68A" },
  training: { bg: "#D1FAE5", text: "#065F46", border: "#6EE7B7" },
  leave:    { bg: "#FCE7F3", text: "#9D174D", border: "#F9A8D4" },
  break:    { bg: "#F3F4F6", text: "#4B5563", border: "#D1D5DB" },
};

const STATUS_BADGE = {
  completed:   { bg: "#D1FAE5", text: "#065F46", label: "Completed"    },
  in_progress: { bg: "#DBEAFE", text: "#1D4ED8", label: "In Progress"  },
  upcoming:    { bg: "#F3F4F6", text: "#6B7280", label: "Upcoming"     },
  absent:      { bg: "#FEE2E2", text: "#991B1B", label: "Absent"       },
  incomplete:  { bg: "#FEF3C7", text: "#B45309", label: "Incomplete"   },
  scheduled:   { bg: "#EDE9FE", text: "#6D28D9", label: "Scheduled"    },
};

// ─── COMPONENT ──────────────────────────────────────────────────────────────

export default function MyScheduleTab() {
  const { C } = useTheme();
  const { T } = useLanguage();

  // ── View state ────────────────────────────────────────────────────────────
  const [viewMode, setViewMode] = useState("week"); // day | week | month
  const [currentDate, setCurrentDate] = useState(TODAY_STR());
  const [teamMode, setTeamMode] = useState(false);

  // ── Data state ────────────────────────────────────────────────────────────
  const [schedData, setSchedData]   = useState(null);
  // Uppercase weekday names, indexed by Date#getDay() (0 = Sunday).
  const DOW_NAMES = ["SUNDAY", "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY",
                     "FRIDAY", "SATURDAY"];
  const [teamData, setTeamData]     = useState(null);
  const [loading, setLoading]       = useState(true);
  const [selected, setSelected]     = useState(null);   // selected block
  const [insights, setInsights]     = useState(null);
  const [insightsLoading, setInsightsLoading] = useState(false);

  // ── Availability modal ────────────────────────────────────────────────────
  const [availModal, setAvailModal] = useState(null);  // { date }
  const [availReason, setAvailReason] = useState("");
  const [availValue, setAvailValue]   = useState(true);
  const [availSaving, setAvailSaving] = useState(false);

  // ─── Derived date range ────────────────────────────────────────────────────
  function getRange() {
    if (viewMode === "day") {
      return { start: currentDate, end: currentDate };
    }
    if (viewMode === "week") {
      const days = getWeekDates(currentDate);
      return { start: days[0], end: days[6] };
    }
    // month
    const d = new Date(currentDate + "T00:00:00");
    const y = d.getFullYear(), m = d.getMonth();
    const start = toLocalISO(new Date(y, m, 1));
    const end   = toLocalISO(new Date(y, m + 1, 0));
    return { start, end };
  }

  // ─── Fetch schedule ────────────────────────────────────────────────────────
  const fetchSchedule = useCallback(async () => {
    setLoading(true);
    try {
      const { start, end } = getRange();
      const res = await API.get("/schedule/my-schedule", { params: { start, end } });
      setSchedData(res.data);
    } catch (e) {
      if (e?.response?.status !== 401) {
        toast.error("Could not load schedule");
      }
    } finally {
      setLoading(false);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentDate, viewMode]);

  const fetchTeam = useCallback(async () => {
    if (!isManager() || !teamMode) return;
    setLoading(true);
    try {
      const { start, end } = getRange();
      const res = await API.get("/schedule/team", { params: { start, end } });
      setTeamData(res.data);
    } catch (e) {
      if (e?.response?.status !== 401) toast.error("Could not load team schedule");
    } finally {
      setLoading(false);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentDate, viewMode, teamMode]);

  const fetchInsights = useCallback(async (dateStr) => {
    setInsightsLoading(true);
    try {
      const res = await API.get("/schedule/day-insights", { params: { date: dateStr } });
      setInsights(res.data);
    } catch {
      setInsights(null);
    } finally {
      setInsightsLoading(false);
    }
  }, []);

  useEffect(() => { fetchSchedule(); }, [fetchSchedule]);
  useEffect(() => { fetchTeam(); }, [fetchTeam]);

  // Poll every 60 s
  const pollRef = useRef(null);
  useEffect(() => {
    pollRef.current = setInterval(() => {
      if (!teamMode) fetchSchedule();
      else fetchTeam();
    }, 60_000);
    return () => clearInterval(pollRef.current);
  }, [fetchSchedule, fetchTeam, teamMode]);

  // ─── Navigation ────────────────────────────────────────────────────────────
  function navigate(dir) {
    const d = new Date(currentDate + "T00:00:00");
    if (viewMode === "day")   d.setDate(d.getDate() + dir);
    if (viewMode === "week")  d.setDate(d.getDate() + dir * 7);
    if (viewMode === "month") d.setMonth(d.getMonth() + dir);
    setCurrentDate(toLocalISO(d));
    setSelected(null);
  }

  function goToday() {
    setCurrentDate(TODAY_STR());
    setSelected(null);
  }

  // ─── Build data maps ────────────────────────────────────────────────────────
  const assignByDate = {};
  const rawByDate    = {};
  const leaveSet     = new Set(); // "YYYY-MM-DD" strings covered by leave
  const holidayMap   = {};
  const availByDate  = {};

  const workingDays = (schedData?.working_days || []).map((d) => String(d).toUpperCase());

  if (schedData) {
    for (const a of schedData.assignments || []) {
      (assignByDate[a.date] = assignByDate[a.date] || []).push(a);
    }
    for (const r of schedData.raw_sessions || []) {
      rawByDate[r.date] = r;
    }
    for (const l of schedData.leaves || []) {
      const from = new Date(l.from_date + "T00:00:00");
      const to   = new Date(l.to_date   + "T00:00:00");
      for (let d = new Date(from); d <= to; d.setDate(d.getDate() + 1)) {
        leaveSet.add(toLocalISO(d));
      }
    }
    for (const h of schedData.holidays || []) {
      holidayMap[h.date] = h;
    }
    for (const v of schedData.availabilities || []) {
      availByDate[v.date] = v;
    }
  }

  // ─── Header label ──────────────────────────────────────────────────────────
  function headerLabel() {
    const d = new Date(currentDate + "T00:00:00");
    if (viewMode === "month") {
      return d.toLocaleDateString("en-IN", { month: "long", year: "numeric" });
    }
    if (viewMode === "week") {
      const days = getWeekDates(currentDate);
      const f = new Date(days[0] + "T00:00:00");
      const t = new Date(days[6] + "T00:00:00");
      const fm = f.toLocaleDateString("en-IN", { day: "numeric", month: "short" });
      const tm = t.toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
      return `${fm} – ${tm}`;
    }
    return d.toLocaleDateString("en-IN", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
  }

  // ─── Select a block ────────────────────────────────────────────────────────
  function selectBlock(item, dateStr) {
    const key = item.id ? `asgn-${item.id}` : `raw-${dateStr}`;
    if (selected === key) { setSelected(null); setInsights(null); return; }
    setSelected(key);
    fetchInsights(dateStr);
  }

  // ─── AVAILABILITY MODAL ────────────────────────────────────────────────────
  async function saveAvailability() {
    if (!availModal) return;
    setAvailSaving(true);
    try {
      await API.post("/schedule/availability", {
        date: availModal.date,
        is_available: availValue,
        reason: availReason.trim() || undefined,
      });
      toast.success("Availability saved");
      setAvailModal(null);
      fetchSchedule();
    } catch {
      toast.error("Could not save availability");
    } finally {
      setAvailSaving(false);
    }
  }

  // ─── RENDERS ───────────────────────────────────────────────────────────────

  // Single day cell content (used in both week and month views)
  function DayCell({ dateStr, inMonth = true, compact = false }) {
    const isToday   = dateStr === TODAY_STR();
    const holiday   = holidayMap[dateStr];
    const onLeave   = leaveSet.has(dateStr);
    const assigns   = assignByDate[dateStr] || [];
    const raw       = rawByDate[dateStr];
    const avail     = availByDate[dateStr];
    // "Non-working day" is POLICY (schedule.working_days), not Sat/Sun. The
    // server sends this employee's own working days; without them we shade
    // nothing rather than imposing a Mon-Fri week on a Mon-Sat workspace.
    const dow       = new Date(dateStr + "T00:00:00").getDay();
    const isWeekend = workingDays.length > 0
      ? !workingDays.includes(DOW_NAMES[dow])
      : false;

    return (
      <div style={{
        flex: 1, minWidth: 0,
        borderRight: `1px solid ${C.border}`,
        background: !inMonth ? `${C.bg}88`
                  : isToday  ? `${C.brand}08`
                  : isWeekend ? `${C.bg}cc`
                  : C.surface,
        position: "relative",
        minHeight: compact ? 64 : undefined,
      }}>
        {/* Day number */}
        <div style={{ padding: compact ? "4px 6px 2px" : "8px 10px 4px", display: "flex", alignItems: "center", gap: 4 }}>
          <span style={{
            width: 24, height: 24, borderRadius: "50%", display: "flex", alignItems: "center",
            justifyContent: "center", fontSize: compact ? 11 : 12, fontWeight: isToday ? 800 : 600,
            background: isToday ? C.brand : "transparent",
            color: isToday ? "#fff" : inMonth ? C.text : C.textSub,
          }}>
            {new Date(dateStr + "T00:00:00").getDate()}
          </span>
          {holiday && (
            <span style={{ fontSize: 9, fontWeight: 700, color: "#D97706", background: "#FEF3C7",
              padding: "1px 5px", borderRadius: 999, whiteSpace: "nowrap", maxWidth: 80, overflow: "hidden",
              textOverflow: "ellipsis" }}>
              {holiday.name}
            </span>
          )}
        </div>

        {/* Leave banner */}
        {onLeave && (
          <div style={{ margin: "0 4px 2px", background: "#FCE7F3", color: "#9D174D",
            fontSize: 10, fontWeight: 700, padding: "2px 6px", borderRadius: 4, textAlign: "center" }}>
            On Leave
          </div>
        )}

        {/* Shift blocks */}
        {assigns.slice(0, compact ? 2 : 99).map(a => {
          const tc = TASK_COLORS[a.task_type] || TASK_COLORS.working;
          const sb = STATUS_BADGE[a.status] || STATUS_BADGE.scheduled;
          const isSelected = selected === `asgn-${a.id}`;
          return (
            <div key={a.id}
              onClick={() => selectBlock(a, dateStr)}
              style={{
                margin: "0 4px 3px", padding: compact ? "3px 5px" : "5px 7px",
                borderRadius: 6, cursor: "pointer",
                background: isSelected ? a.color : tc.bg,
                border: `1.5px solid ${isSelected ? a.color : tc.border}`,
                color: isSelected ? "#fff" : tc.text,
                fontSize: compact ? 10 : 11, fontWeight: 600,
                transition: "all .15s",
                boxShadow: isSelected ? `0 2px 8px ${a.color}44` : "none",
              }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 4 }}>
                <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}>
                  {a.shift_name}
                </span>
                {!compact && (
                  <span style={{ fontSize: 9, fontWeight: 700, padding: "1px 4px", borderRadius: 999,
                    background: isSelected ? "rgba(255,255,255,0.25)" : sb.bg,
                    color: isSelected ? "#fff" : sb.text, whiteSpace: "nowrap" }}>
                    {sb.label}
                  </span>
                )}
              </div>
              {!compact && (
                <div style={{ fontSize: 10, opacity: .85, marginTop: 1 }}>
                  {a.scheduled_start && a.scheduled_end
                    ? `${a.scheduled_start} – ${a.scheduled_end}`
                    : a.task_type}
                </div>
              )}
              {/* Late / early warning */}
              {a.actual?.late_minutes > 5 && !compact && (
                <div style={{ fontSize: 9, color: isSelected ? "#FDE68A" : "#B45309",
                  marginTop: 1, fontWeight: 700 }}>
                  ⚠ Late {a.actual.late_minutes}m
                </div>
              )}
            </div>
          );
        })}

        {/* Raw session (no formal shift) */}
        {!assigns.length && raw && (
          <div onClick={() => selectBlock(raw, dateStr)}
            style={{
              margin: "0 4px 3px", padding: compact ? "3px 5px" : "5px 7px",
              borderRadius: 6, cursor: "pointer",
              background: selected === `raw-${dateStr}` ? "#6B7280" : "#F3F4F6",
              border: `1.5px solid #D1D5DB`,
              color: selected === `raw-${dateStr}` ? "#fff" : "#374151",
              fontSize: compact ? 10 : 11, fontWeight: 600, transition: "all .15s",
            }}>
            {raw.clock_in} – {raw.clock_out || "Active"}
            {!compact && <div style={{ fontSize: 10, opacity: .8 }}>{fmtHM(raw.total_seconds)}</div>}
          </div>
        )}

        {/* Availability chip — shown when the user has marked themselves
            available/unavailable for this day (and there's no shift/session).
            Clicking re-opens the modal pre-filled so they can edit or flip it. */}
        {!onLeave && !holiday && !assigns.length && !raw && avail && inMonth && (
          <div
            onClick={() => { setAvailModal({ date: dateStr }); setAvailValue(!!avail.is_available); setAvailReason(avail.reason || ""); }}
            title={avail.reason || (avail.is_available ? "Available" : "Unavailable")}
            style={{
              margin: "0 4px 3px", padding: compact ? "3px 5px" : "5px 7px",
              borderRadius: 6, cursor: "pointer",
              background: avail.is_available ? "#DCFCE7" : "#FEE2E2",
              border: `1.5px solid ${avail.is_available ? "#86EFAC" : "#FCA5A5"}`,
              color: avail.is_available ? "#166534" : "#991B1B",
              fontSize: compact ? 10 : 11, fontWeight: 700,
              transition: "all .15s",
              overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
            }}>
            {avail.is_available ? "✓ Available" : "✗ Unavailable"}
            {avail.reason && !compact && (
              <div style={{ fontSize: 10, fontWeight: 500, opacity: .85, marginTop: 1,
                overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {avail.reason}
              </div>
            )}
          </div>
        )}

        {/* Availability button on empty days. Hidden for past dates —
            availability is forward-looking; backfilling history makes no
            sense and would let users invalidate their own time records. */}
        {!onLeave && !holiday && !assigns.length && !raw && !avail && inMonth && dateStr >= TODAY_STR() && (
          <button onClick={() => { setAvailModal({ date: dateStr }); setAvailValue(true); setAvailReason(""); }}
            style={{ display: "block", margin: "0 4px 3px", width: "calc(100% - 8px)",
              background: "transparent", border: `1px dashed ${C.border}`, borderRadius: 5,
              color: C.textSub, fontSize: 9, padding: "4px 0", cursor: "pointer",
              fontFamily: "inherit" }}>
            + availability
          </button>
        )}
      </div>
    );
  }

  // ─── WEEK VIEW ──────────────────────────────────────────────────────────────
  function WeekView() {
    const days = getWeekDates(currentDate);
    const DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

    return (
      <div style={{ overflowX: "auto" }}>
        {/* Header row */}
        <div style={{ display: "flex", borderBottom: `1px solid ${C.border}` }}>
          {days.map((d, i) => {
            const isToday = d === TODAY_STR();
            return (
              <div key={d} style={{
                flex: 1, minWidth: 100, padding: "8px 10px", textAlign: "center",
                borderRight: i < 6 ? `1px solid ${C.border}` : "none",
                background: isToday ? `${C.brand}10` : "transparent",
              }}>
                <div style={{ fontSize: 11, fontWeight: 600, color: C.textSub, textTransform: "uppercase", letterSpacing: ".5px" }}>
                  {DAY_LABELS[i]}
                </div>
                <div style={{ fontSize: isToday ? 16 : 14, fontWeight: isToday ? 800 : 600,
                  color: isToday ? C.brand : C.text, marginTop: 2 }}>
                  {new Date(d + "T00:00:00").getDate()}
                </div>
              </div>
            );
          })}
        </div>
        {/* Day cells */}
        <div style={{ display: "flex", minHeight: 320 }}>
          {days.map((d, i) => (
            <DayCell key={d} dateStr={d} inMonth={true} compact={false} />
          ))}
        </div>
      </div>
    );
  }

  // ─── MONTH VIEW ─────────────────────────────────────────────────────────────
  function MonthView() {
    const d      = new Date(currentDate + "T00:00:00");
    const cells  = getMonthGrid(d.getFullYear(), d.getMonth());
    const DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

    return (
      <div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(7,1fr)",
          borderBottom: `1px solid ${C.border}` }}>
          {DAY_LABELS.map(l => (
            <div key={l} style={{ padding: "8px 0", textAlign: "center", fontSize: 11,
              fontWeight: 700, color: C.textSub, textTransform: "uppercase", letterSpacing: ".5px" }}>
              {l}
            </div>
          ))}
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(7,1fr)" }}>
          {cells.map(({ date: ds, inMonth }) => (
            <div key={ds} style={{ borderRight: `1px solid ${C.border}`,
              borderBottom: `1px solid ${C.border}` }}>
              <DayCell dateStr={ds} inMonth={inMonth} compact={true} />
            </div>
          ))}
        </div>
      </div>
    );
  }

  // ─── DAY VIEW ───────────────────────────────────────────────────────────────
  function DayView() {
    const assigns = assignByDate[currentDate] || [];
    const raw     = rawByDate[currentDate];
    const holiday = holidayMap[currentDate];
    const onLeave = leaveSet.has(currentDate);
    const avail   = availByDate[currentDate];

    return (
      <div style={{ padding: 24 }}>
        {holiday && (
          <div style={{ background: "#FEF3C7", border: "1px solid #FDE68A", borderRadius: 10,
            padding: "10px 16px", marginBottom: 16, display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ fontSize: 18 }}>🎉</span>
            <div>
              <p style={{ fontWeight: 700, color: "#B45309", fontSize: 14 }}>{holiday.name}</p>
              <p style={{ fontSize: 12, color: "#92400E" }}>{holiday.holiday_type} Holiday</p>
            </div>
          </div>
        )}
        {onLeave && (
          <div style={{ background: "#FCE7F3", border: "1px solid #F9A8D4", borderRadius: 10,
            padding: "10px 16px", marginBottom: 16, display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ fontSize: 18 }}>🌿</span>
            <p style={{ fontWeight: 700, color: "#9D174D", fontSize: 14 }}>You are on leave today</p>
          </div>
        )}
        {avail && !onLeave && !holiday && assigns.length === 0 && !raw && (
          <div style={{
            background: avail.is_available ? "#DCFCE7" : "#FEE2E2",
            border: `1px solid ${avail.is_available ? "#86EFAC" : "#FCA5A5"}`,
            borderRadius: 10, padding: "10px 16px", marginBottom: 16,
            display: "flex", alignItems: "center", gap: 10,
          }}>
            <span style={{ fontSize: 16, fontWeight: 800,
              color: avail.is_available ? "#166534" : "#991B1B" }}>
              {avail.is_available ? "✓" : "✗"}
            </span>
            <div style={{ flex: 1 }}>
              <p style={{ fontWeight: 700, fontSize: 14,
                color: avail.is_available ? "#166534" : "#991B1B" }}>
                You marked yourself {avail.is_available ? "available" : "unavailable"}
              </p>
              {avail.reason && (
                <p style={{ fontSize: 12, marginTop: 2,
                  color: avail.is_available ? "#15803D" : "#B91C1C" }}>
                  {avail.reason}
                </p>
              )}
            </div>
            {currentDate >= TODAY_STR() && (
              <button onClick={() => { setAvailModal({ date: currentDate }); setAvailValue(!!avail.is_available); setAvailReason(avail.reason || ""); }}
                style={{ padding: "6px 14px", borderRadius: 6, cursor: "pointer",
                  border: `1px solid ${avail.is_available ? "#86EFAC" : "#FCA5A5"}`,
                  background: "rgba(255,255,255,0.6)",
                  color: avail.is_available ? "#166534" : "#991B1B",
                  fontWeight: 700, fontSize: 12, fontFamily: "inherit" }}>
                Edit
              </button>
            )}
          </div>
        )}

        {assigns.length === 0 && !raw ? (
          <div style={{ textAlign: "center", padding: "48px 0", color: C.textSub }}>
            <div style={{ fontSize: 36, marginBottom: 12 }}>📅</div>
            <p style={{ fontWeight: 700, fontSize: 15, color: C.text }}>No shifts scheduled</p>
            <p style={{ fontSize: 13, marginTop: 4 }}>No formal shift assigned for this day.</p>
            {!avail && currentDate >= TODAY_STR() && (
              <button onClick={() => { setAvailModal({ date: currentDate }); setAvailValue(true); setAvailReason(""); }}
                style={{ marginTop: 16, padding: "8px 20px", borderRadius: 8, cursor: "pointer",
                  border: `1px solid ${C.border}`, background: C.surface, color: C.brand,
                  fontWeight: 700, fontSize: 13, fontFamily: "inherit" }}>
                Mark Availability
              </button>
            )}
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {assigns.map(a => {
              const tc = TASK_COLORS[a.task_type] || TASK_COLORS.working;
              const sb = STATUS_BADGE[a.status]   || STATUS_BADGE.scheduled;
              const isSelected = selected === `asgn-${a.id}`;
              return (
                <div key={a.id} onClick={() => selectBlock(a, currentDate)}
                  className="card-hover"
                  style={{
                    background: C.surface, border: `2px solid ${isSelected ? a.color : C.border}`,
                    borderRadius: 12, padding: 16, cursor: "pointer",
                    boxShadow: isSelected ? `0 4px 16px ${a.color}33` : "0 1px 3px rgba(0,0,0,0.04)",
                    transition: "all .2s",
                  }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
                    <div style={{ width: 12, height: 12, borderRadius: "50%",
                      background: a.color, flexShrink: 0 }} />
                    <p style={{ fontWeight: 800, fontSize: 15, color: C.text, flex: 1 }}>{a.shift_name}</p>
                    <span style={{ fontSize: 11, fontWeight: 700, padding: "3px 10px", borderRadius: 999,
                      background: sb.bg, color: sb.text }}>{sb.label}</span>
                    <span style={{ fontSize: 11, fontWeight: 600, padding: "3px 8px", borderRadius: 999,
                      background: tc.bg, color: tc.text, textTransform: "capitalize" }}>
                      {a.task_type}
                    </span>
                  </div>

                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "6px 16px", fontSize: 13 }}>
                    <Kv k="Scheduled" v={a.scheduled_start && a.scheduled_end
                      ? `${a.scheduled_start} – ${a.scheduled_end}` : "—"} C={C} />
                    <Kv k="Clocked In"  v={a.actual?.clock_in  || "—"} C={C} />
                    <Kv k="Clocked Out" v={a.actual?.clock_out || "—"} C={C} />
                    <Kv k="Total Time"  v={fmtHM(a.actual?.total_seconds)} C={C} />
                    {a.actual?.late_minutes > 0 && (
                      <Kv k="Late" v={`${a.actual.late_minutes} min`} C={C} warn />
                    )}
                    {a.actual?.early_minutes > 0 && (
                      <Kv k="Left Early" v={`${a.actual.early_minutes} min`} C={C} warn />
                    )}
                  </div>
                </div>
              );
            })}

            {/* Raw session card */}
            {raw && !assigns.length && (
              <div onClick={() => selectBlock(raw, currentDate)}
                className="card-hover"
                style={{ background: C.surface, border: `2px solid ${selected === `raw-${currentDate}` ? "#6B7280" : C.border}`,
                  borderRadius: 12, padding: 16, cursor: "pointer" }}>
                <p style={{ fontWeight: 700, color: C.text, marginBottom: 8 }}>Work Session (Unscheduled)</p>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "6px 16px", fontSize: 13 }}>
                  <Kv k="Clock In"  v={raw.clock_in}  C={C} />
                  <Kv k="Clock Out" v={raw.clock_out || "Active"} C={C} />
                  <Kv k="Total"     v={fmtHM(raw.total_seconds)} C={C} />
                  <Kv k="Screenshots" v={raw.screenshot_count} C={C} />
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    );
  }

  // ─── TEAM VIEW ──────────────────────────────────────────────────────────────
  function TeamView() {
    if (!teamData) return (
      <div style={{ padding: 32, textAlign: "center", color: C.textSub }}>
        <p>Loading team schedule…</p>
      </div>
    );
    const rows = teamData.team_schedule || [];
    if (!rows.length) return (
      <div style={{ padding: 48, textAlign: "center", color: C.textSub }}>
        <div style={{ fontSize: 32, marginBottom: 12 }}>👥</div>
        <p style={{ fontWeight: 700, color: C.text }}>No team assignments in this range</p>
        <p style={{ fontSize: 13, marginTop: 4 }}>Assign shifts from the admin panel to see them here.</p>
      </div>
    );

    // Group by date
    const byDate = {};
    for (const r of rows) {
      (byDate[r.date] = byDate[r.date] || []).push(r);
    }

    return (
      <div style={{ padding: "16px 20px", display: "flex", flexDirection: "column", gap: 16 }}>
        {Object.entries(byDate).sort().map(([date, members]) => (
          <div key={date}>
            <p style={{ fontSize: 12, fontWeight: 700, color: C.textSub, textTransform: "uppercase",
              letterSpacing: ".6px", marginBottom: 8 }}>
              {fmtDate(date)}{date === TODAY_STR() ? " — Today" : ""}
            </p>
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {members.map(m => {
                const tc = TASK_COLORS[m.task_type] || TASK_COLORS.working;
                const worked = m.total_seconds ? fmtHM(m.total_seconds) : "—";
                return (
                  <div key={m.assignment_id}
                    style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10,
                      padding: "10px 14px", display: "flex", alignItems: "center", gap: 12 }}>
                    <div style={{ width: 10, height: 10, borderRadius: "50%",
                      background: m.color || "#3B82F6", flexShrink: 0 }} />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <p style={{ fontWeight: 700, fontSize: 13, color: C.text }}>{m.user_name}</p>
                      {m.department && (
                        <p style={{ fontSize: 11, color: C.textSub }}>{m.department}</p>
                      )}
                    </div>
                    <div style={{ fontSize: 12, color: C.textSub, textAlign: "center" }}>
                      <span style={{ fontWeight: 600, color: C.text }}>{m.shift_name}</span>
                      {m.scheduled_start && (
                        <div>{m.scheduled_start} – {m.scheduled_end}</div>
                      )}
                    </div>
                    <div style={{ textAlign: "right" }}>
                      {m.actual_clock_in ? (
                        <p style={{ fontSize: 12, color: "#16A34A", fontWeight: 600 }}>
                          In {m.actual_clock_in}
                        </p>
                      ) : (
                        <p style={{ fontSize: 12, color: C.textSub }}>Not clocked in</p>
                      )}
                      {m.actual_clock_out && (
                        <p style={{ fontSize: 11, color: C.textSub }}>Out {m.actual_clock_out}</p>
                      )}
                      <p style={{ fontSize: 11, color: C.textSub }}>{worked}</p>
                    </div>
                    <span style={{ fontSize: 11, fontWeight: 700, padding: "3px 8px", borderRadius: 999,
                      background: tc.bg, color: tc.text, flexShrink: 0, textTransform: "capitalize" }}>
                      {m.task_type}
                    </span>
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    );
  }

  // ─── INSIGHTS PANEL ─────────────────────────────────────────────────────────
  function InsightsPanel() {
    if (insightsLoading) return (
      <div style={{ padding: 20, textAlign: "center", color: C.textSub, fontSize: 13 }}>Loading insights…</div>
    );
    if (!insights) return null;

    const { session, activity, top_apps, hourly } = insights;
    const activeH = Math.round((activity?.active_ticks || 0) / 60 * 10) / 10; // ~approx hours
    const idleH   = Math.round((activity?.total_idle_seconds || 0) / 3600 * 10) / 10;
    const totalH  = session ? Math.round((session.total_seconds || 0) / 3600 * 10) / 10 : 0;

    // Simple hourly bar chart (max 13 bars from 7am–7pm)
    const maxActive = Math.max(...(hourly || []).map(h => h.active), 1);

    return (
      <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 12,
        padding: 16, marginTop: 16 }}>
        <p style={{ fontWeight: 800, fontSize: 14, color: C.text, marginBottom: 12 }}>
          📊 Day Insights
        </p>

        {/* KPI row */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 8, marginBottom: 12 }}>
          {[
            { label: "Total Time",  value: `${totalH}h`,  color: C.brand },
            { label: "Active",      value: `${activeH}h`, color: "#16A34A" },
            { label: "Idle",        value: `${idleH}h`,   color: "#B45309" },
          ].map(({ label, value, color }) => (
            <div key={label} style={{ background: C.bg, border: `1px solid ${C.border}`,
              borderRadius: 8, padding: "8px 10px", textAlign: "center" }}>
              <p style={{ fontSize: 16, fontWeight: 800, color }}>{value}</p>
              <p style={{ fontSize: 10, color: C.textSub, marginTop: 2 }}>{label}</p>
            </div>
          ))}
        </div>

        {/* Screenshots + productivity score */}
        <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
          <div style={{ flex: 1, background: C.bg, border: `1px solid ${C.border}`,
            borderRadius: 8, padding: "8px 10px" }}>
            <p style={{ fontSize: 12, fontWeight: 700, color: C.text }}>
              📸 {activity?.screenshot_count || 0} screenshots
            </p>
          </div>
          <div style={{ flex: 1, background: C.bg, border: `1px solid ${C.border}`,
            borderRadius: 8, padding: "8px 10px" }}>
            <p style={{ fontSize: 12, fontWeight: 700, color: C.text }}>
              ⭐ {Math.round((activity?.avg_productivity || 0) * 100)}% productivity
            </p>
          </div>
        </div>

        {/* Hourly activity bars */}
        {hourly && hourly.length > 0 && (
          <div style={{ marginBottom: 12 }}>
            <p style={{ fontSize: 11, fontWeight: 600, color: C.textSub, marginBottom: 6 }}>
              Hourly Activity
            </p>
            <div style={{ display: "flex", gap: 2, alignItems: "flex-end", height: 36 }}>
              {hourly.map(h => {
                const pct = Math.round((h.active / maxActive) * 100);
                return (
                  <div key={h.hour} title={`${h.hour}:00 — ${h.active} active ticks`}
                    style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center" }}>
                    <div style={{ width: "100%", height: Math.max(2, pct * 0.32),
                      background: pct > 50 ? C.brand : "#D1D5DB", borderRadius: 2,
                      transition: "height .3s" }} />
                  </div>
                );
              })}
            </div>
            <div style={{ display: "flex", justifyContent: "space-between",
              fontSize: 9, color: C.textSub, marginTop: 2 }}>
              <span>{hourly[0]?.hour}:00</span>
              <span>{hourly[hourly.length - 1]?.hour}:00</span>
            </div>
          </div>
        )}

        {/* Top apps */}
        {top_apps && top_apps.length > 0 && (
          <div>
            <p style={{ fontSize: 11, fontWeight: 600, color: C.textSub, marginBottom: 6 }}>
              Top Apps
            </p>
            {top_apps.slice(0, 4).map(a => {
              const maxCnt = top_apps[0]?.count || 1;
              const pct = Math.round((a.count / maxCnt) * 100);
              return (
                <div key={a.app} style={{ marginBottom: 5 }}>
                  <div style={{ display: "flex", justifyContent: "space-between",
                    fontSize: 11, color: C.text, marginBottom: 2 }}>
                    <span style={{ fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis",
                      whiteSpace: "nowrap", maxWidth: "70%" }}>{a.app}</span>
                    <span style={{ color: C.textSub }}>{a.count}</span>
                  </div>
                  <div style={{ height: 4, borderRadius: 2, background: C.border }}>
                    <div style={{ height: "100%", width: `${pct}%`, background: C.brand,
                      borderRadius: 2, transition: "width .4s" }} />
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    );
  }

  // ─── AVAILABILITY MODAL ─────────────────────────────────────────────────────
  function AvailabilityModal() {
    if (!availModal) return null;
    return (
      <div onClick={() => setAvailModal(null)}
        style={{ position: "fixed", inset: 0, zIndex: 1000, display: "flex",
          alignItems: "center", justifyContent: "center",
          background: "rgba(0,0,0,.45)", backdropFilter: "blur(4px)" }}>
        <div onClick={e => e.stopPropagation()}
          style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 14,
            padding: 24, width: 360, boxShadow: "0 20px 60px rgba(0,0,0,.2)" }}>
          <p style={{ fontWeight: 800, fontSize: 15, color: C.text, marginBottom: 4 }}>
            Mark Availability
          </p>
          <p style={{ fontSize: 12, color: C.textSub, marginBottom: 16 }}>
            {fmtDate(availModal.date)}
          </p>

          <div style={{ display: "flex", gap: 8, marginBottom: 14 }}>
            {[true, false].map(v => (
              <button key={String(v)} onClick={() => setAvailValue(v)}
                style={{ flex: 1, padding: "9px 0", borderRadius: 8, cursor: "pointer",
                  fontFamily: "inherit", fontWeight: 700, fontSize: 13,
                  border: `2px solid ${availValue === v ? (v ? "#16A34A" : "#DC2626") : C.border}`,
                  background: availValue === v ? (v ? "#DCFCE7" : "#FEE2E2") : "transparent",
                  color: availValue === v ? (v ? "#16A34A" : "#DC2626") : C.textSub,
                  transition: "all .15s" }}>
                {v ? "✓ Available" : "✗ Unavailable"}
              </button>
            ))}
          </div>

          <input placeholder="Reason (optional)"
            value={availReason} onChange={e => setAvailReason(e.target.value)}
            style={{ width: "100%", padding: "9px 12px", borderRadius: 8,
              border: `1px solid ${C.border}`, background: C.bg, color: C.text,
              fontSize: 13, fontFamily: "inherit", marginBottom: 14, boxSizing: "border-box" }} />

          <div style={{ display: "flex", gap: 8 }}>
            <button onClick={() => setAvailModal(null)}
              style={{ flex: 1, padding: "9px 0", borderRadius: 8, cursor: "pointer",
                border: `1px solid ${C.border}`, background: "transparent", color: C.textSub,
                fontFamily: "inherit", fontWeight: 600, fontSize: 13 }}>
              Cancel
            </button>
            <button onClick={saveAvailability} disabled={availSaving}
              style={{ flex: 2, padding: "9px 0", borderRadius: 8, cursor: "pointer",
                border: "none", background: C.brand, color: "#fff",
                fontFamily: "inherit", fontWeight: 700, fontSize: 13,
                opacity: availSaving ? .7 : 1 }}>
              {availSaving ? "Saving…" : "Save"}
            </button>
          </div>
        </div>
      </div>
    );
  }

  // ─── LEGEND ─────────────────────────────────────────────────────────────────
  function Legend() {
    return (
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "center",
        padding: "8px 16px", borderTop: `1px solid ${C.border}` }}>
        <span style={{ fontSize: 11, fontWeight: 600, color: C.textSub, marginRight: 4 }}>Legend:</span>
        {Object.entries(TASK_COLORS).map(([k, v]) => (
          <span key={k} style={{ display: "inline-flex", alignItems: "center", gap: 4,
            fontSize: 11, color: v.text }}>
            <span style={{ width: 8, height: 8, borderRadius: "50%",
              background: v.border, display: "inline-block" }} />
            {k.charAt(0).toUpperCase() + k.slice(1)}
          </span>
        ))}
      </div>
    );
  }

  // ─── EMPTY STATE ────────────────────────────────────────────────────────────
  function EmptyState() {
    return (
      <div style={{ padding: "48px 24px", textAlign: "center" }}>
        <div style={{ width: 60, height: 60, borderRadius: "50%", background: `${C.brand}15`,
          display: "flex", alignItems: "center", justifyContent: "center",
          margin: "0 auto 16px", fontSize: 28 }}>📅</div>
        <p style={{ fontWeight: 800, fontSize: 15, color: C.text, marginBottom: 6 }}>
          No shifts scheduled
        </p>
        <p style={{ fontSize: 13, color: C.textSub, maxWidth: 320, margin: "0 auto 20px", lineHeight: 1.6 }}>
          Your manager hasn't assigned any shifts yet. You can still mark your
          availability so they can schedule you.
        </p>
        {currentDate >= TODAY_STR() && (
          <button onClick={() => { setAvailModal({ date: currentDate }); setAvailValue(true); setAvailReason(""); }}
            style={{ padding: "9px 22px", borderRadius: 8, cursor: "pointer",
              border: "none", background: C.brand, color: "#fff",
              fontFamily: "inherit", fontWeight: 700, fontSize: 13 }}>
            Mark Availability
          </button>
        )}
      </div>
    );
  }

  // ─── MAIN RENDER ────────────────────────────────────────────────────────────
  const hasAnyData = schedData &&
    ((schedData.assignments?.length || 0) +
     (schedData.raw_sessions?.length  || 0) +
     (schedData.leaves?.length        || 0) +
     (schedData.holidays?.length      || 0)) > 0;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>

      {/* ── Main calendar card ────────────────────────────────────────── */}
      <div style={{ background: C.surface, border: `1px solid ${C.border}`,
        borderRadius: 14, overflow: "hidden", boxShadow: "0 1px 3px rgba(0,0,0,0.04)" }}>

        {/* Toolbar */}
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap",
          padding: "12px 16px", borderBottom: `1px solid ${C.border}` }}>
          {/* View toggles */}
          <div style={{ display: "flex", gap: 2, background: C.bg, borderRadius: 8,
            padding: 3, border: `1px solid ${C.border}` }}>
            {["day", "week", "month"].map(v => (
              <button key={v} onClick={() => { setViewMode(v); setSelected(null); }}
                style={{ padding: "5px 12px", borderRadius: 6, cursor: "pointer",
                  border: "none", fontFamily: "inherit", fontWeight: 600, fontSize: 12,
                  background: viewMode === v ? C.brand : "transparent",
                  color: viewMode === v ? "#fff" : C.textSub,
                  transition: "all .15s", textTransform: "capitalize" }}>
                {v}
              </button>
            ))}
          </div>

          {/* Nav */}
          <button onClick={() => navigate(-1)}
            style={{ width: 30, height: 30, borderRadius: 7, cursor: "pointer",
              border: `1px solid ${C.border}`, background: C.surface, color: C.text,
              fontFamily: "inherit", fontSize: 14, display: "flex", alignItems: "center", justifyContent: "center" }}>
            ‹
          </button>
          <span style={{ fontWeight: 700, fontSize: 13, color: C.text, minWidth: 160, textAlign: "center" }}>
            {headerLabel()}
          </span>
          <button onClick={() => navigate(1)}
            style={{ width: 30, height: 30, borderRadius: 7, cursor: "pointer",
              border: `1px solid ${C.border}`, background: C.surface, color: C.text,
              fontFamily: "inherit", fontSize: 14, display: "flex", alignItems: "center", justifyContent: "center" }}>
            ›
          </button>
          <button onClick={goToday}
            style={{ padding: "5px 12px", borderRadius: 7, cursor: "pointer",
              border: `1px solid ${C.border}`, background: C.surface, color: C.brand,
              fontFamily: "inherit", fontWeight: 700, fontSize: 12 }}>
            Today
          </button>

          {/* Refresh */}
          <button onClick={teamMode ? fetchTeam : fetchSchedule}
            title="Refresh" disabled={loading}
            style={{ marginLeft: "auto", width: 30, height: 30, borderRadius: 7, cursor: "pointer",
              border: `1px solid ${C.border}`, background: C.surface, color: C.textSub,
              display: "flex", alignItems: "center", justifyContent: "center",
              fontSize: 14, opacity: loading ? .5 : 1 }}>
            ↻
          </button>

          {/* Team view toggle (managers only) */}
          {isManager() && (
            <button onClick={() => { setTeamMode(t => !t); setSelected(null); }}
              style={{ padding: "5px 12px", borderRadius: 7, cursor: "pointer",
                border: `1px solid ${teamMode ? C.brand : C.border}`,
                background: teamMode ? `${C.brand}15` : "transparent",
                color: teamMode ? C.brand : C.textSub,
                fontFamily: "inherit", fontWeight: 600, fontSize: 12 }}>
              👥 {teamMode ? "My Schedule" : "Team View"}
            </button>
          )}
        </div>

        {/* Calendar body */}
        {loading ? (
          <div style={{ padding: 48, textAlign: "center", color: C.textSub }}>
            <div style={{ fontSize: 24, marginBottom: 8 }}>⏳</div>
            <p style={{ fontSize: 13 }}>Loading schedule…</p>
          </div>
        ) : teamMode ? (
          <TeamView />
        ) : !hasAnyData && viewMode !== "day" ? (
          <EmptyState />
        ) : viewMode === "week" ? (
          <WeekView />
        ) : viewMode === "month" ? (
          <MonthView />
        ) : (
          <DayView />
        )}

        <Legend />
      </div>

      {/* ── Insights panel (shows when a block is selected) ───────────── */}
      {selected && (
        <div style={{ background: C.surface, border: `1px solid ${C.border}`,
          borderRadius: 14, overflow: "hidden", boxShadow: "0 1px 3px rgba(0,0,0,0.04)" }}>
          <div style={{ padding: "12px 16px", borderBottom: `1px solid ${C.border}`,
            display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <p style={{ fontWeight: 800, fontSize: 14, color: C.text }}>Productivity Insights</p>
            <button onClick={() => { setSelected(null); setInsights(null); }}
              style={{ background: "transparent", border: "none", cursor: "pointer",
                color: C.textSub, fontSize: 18, lineHeight: 1 }}>×</button>
          </div>
          <div style={{ padding: "0 16px 16px" }}>
            <InsightsPanel />
          </div>
        </div>
      )}

      {/* ── Leave summary card ────────────────────────────────────────── */}
      {schedData?.leaves?.length > 0 && (
        <div style={{ background: C.surface, border: `1px solid ${C.border}`,
          borderRadius: 14, padding: 16, boxShadow: "0 1px 3px rgba(0,0,0,0.04)" }}>
          <p style={{ fontWeight: 800, fontSize: 13, color: C.text, marginBottom: 10 }}>
            🌿 Leave in This Period
          </p>
          {schedData.leaves.map(l => (
            <div key={l.id} style={{ display: "flex", alignItems: "center", gap: 10,
              padding: "8px 0", borderBottom: `1px solid ${C.borderLight}` }}>
              <div style={{ flex: 1 }}>
                <p style={{ fontWeight: 700, fontSize: 13, color: C.text,
                  textTransform: "capitalize" }}>{l.leave_type.replace(/_/g, " ")}</p>
                <p style={{ fontSize: 11, color: C.textSub }}>
                  {fmtDate(l.from_date)} – {fmtDate(l.to_date)}
                </p>
              </div>
              <span style={{ fontSize: 11, fontWeight: 700, padding: "3px 10px", borderRadius: 999,
                background: l.status === "approved" ? "#DCFCE7" : "#FEF3C7",
                color: l.status === "approved" ? "#16A34A" : "#B45309",
                textTransform: "capitalize" }}>
                {l.status}
              </span>
            </div>
          ))}
        </div>
      )}

      {/* ── Upcoming holidays ─────────────────────────────────────────── */}
      {schedData?.holidays?.length > 0 && (
        <div style={{ background: C.surface, border: `1px solid ${C.border}`,
          borderRadius: 14, padding: 16, boxShadow: "0 1px 3px rgba(0,0,0,0.04)" }}>
          <p style={{ fontWeight: 800, fontSize: 13, color: C.text, marginBottom: 10 }}>
            🎉 Holidays
          </p>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {schedData.holidays.map(h => (
              <div key={h.id} style={{ display: "flex", alignItems: "center", gap: 10,
                padding: "6px 10px", borderRadius: 8, background: "#FEF3C7",
                border: "1px solid #FDE68A" }}>
                <span style={{ fontWeight: 700, fontSize: 13, color: "#B45309", flex: 1 }}>{h.name}</span>
                <span style={{ fontSize: 11, color: "#92400E" }}>{fmtDate(h.date)}</span>
                <span style={{ fontSize: 10, fontWeight: 700, color: "#D97706",
                  background: "#FEF9C3", padding: "1px 6px", borderRadius: 999 }}>
                  {h.holiday_type}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Availability modal — call as a function, NOT as <AvailabilityModal />.
          Using JSX-element syntax with a function declared inside the parent
          gives the modal a fresh component identity on every parent render,
          which makes React unmount + remount it on every keystroke and the
          Reason <input> loses focus character-by-character (BUG-004). */}
      {AvailabilityModal()}
    </div>
  );
}

// ─── HELPER COMPONENTS ────────────────────────────────────────────────────────

function Kv({ k, v, C, warn }) {
  return (
    <div>
      <p style={{ fontSize: 11, color: C.textSub, marginBottom: 1 }}>{k}</p>
      <p style={{ fontSize: 13, fontWeight: 700,
        color: warn ? "#B45309" : C.text }}>{String(v ?? "—")}</p>
    </div>
  );
}

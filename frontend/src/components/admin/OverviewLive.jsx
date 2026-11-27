/**
 * Live Workforce Overview: the top of the admin Overview tab.
 *
 * Presentational only. AdminDashboard owns every number shown here: the
 * canonical status counts (one worker, one bucket, see `statusCounts` there),
 * the WebSocket state, the historical summary when a past date is picked, and
 * the filtered, paged worker list. Nothing in this file fetches.
 *
 * Active Time in the worker table is the same figure the grid cards show
 * (EmpCard / MyTeamTab: the server's `active_pct`, floored at 65), so the List
 * and Grid views of one worker always agree.
 */
import React, { useEffect, useMemo, useState } from "react";
import {
  Users, Coffee, Clock, CirclePause, CalendarDays, UserX, TriangleAlert,
  ChevronRight, ChevronDown, Info, UserPlus, CalendarPlus, FileText, Settings,
  Ellipsis, CircleCheck, ChartPie, Zap, Calendar, LayoutGrid, List,
} from "lucide-react";
import { initials } from "./adminShared";
import {
  OV_COLORS, workforcePct, navyFor, rowView, avatarTone,
  distributionRows, historyDistributionRows, LIVE_GRACE_MS,
} from "./overviewModel";
import { getOrgTimeZone } from "../../lib/orgTime";

const CARD_SHADOW = "0 1px 2px rgba(16,24,40,.06), 0 1px 3px rgba(16,24,40,.04)";

// A status colour washed over the theme surface, so a tinted card reads in
// light and dark mode alike.
const tint = (C, color, alpha = "12") =>
  `linear-gradient(0deg, ${color}${alpha}, ${color}${alpha}), ${C.surface}`;

// ── Title row ────────────────────────────────────────────────────────────────

export function OverviewTitle({ C, T, wsStatus, historical, dateLabel }) {
  // The clock ticks inside this component so the minute change re-renders the
  // stamp only, not the dashboard around it.
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const iv = setInterval(() => setNow(new Date()), 30000);
    return () => clearInterval(iv);
  }, []);

  // "live" as soon as the socket connects; "reconnecting" only after it has
  // been down for LIVE_GRACE_MS; "starting" until the first connection.
  const [liveState, setLiveState] = useState(wsStatus === "connected" ? "live" : "starting");
  useEffect(() => {
    const connected = wsStatus === "connected";
    const t = setTimeout(() => setLiveState(connected ? "live" : "reconnecting"), connected ? 0 : LIVE_GRACE_MS);
    return () => clearTimeout(t);
  }, [wsStatus]);

  const stamp = historical
    ? dateLabel
    : now.toLocaleString("en-US", {
        month: "short", day: "numeric", year: "numeric",
        hour: "numeric", minute: "2-digit", hour12: true,
        timeZone: getOrgTimeZone(),
      });
  const live = liveState === "live"
    ? { label: T.liveUpper || "LIVE", color: "#16A34A", hint: T.liveUpdatesOn || "Live updates are on." }
    : liveState === "reconnecting"
      ? { label: T.reconnectingUpper || "RECONNECTING", color: "#D97706",
          hint: T.liveUpdatesPaused || "Live updates paused while reconnecting. The figures still refresh every few minutes." }
      : { label: T.liveUpper || "LIVE", color: "#94A3B8", hint: T.liveUpdatesStarting || "Connecting to live updates…" };

  return (
    <div style={{ display: "flex", alignItems: "center", gap: "10px 28px", flexWrap: "wrap" }}>
      <div style={{ minWidth: 0 }}>
        <h1 style={{ fontSize: 30, fontWeight: 800, color: C.text, letterSpacing: "-.6px", lineHeight: 1.15, margin: 0 }}>
          {T.liveWorkforceOverview || "Live Workforce Overview"}
        </h1>
        <p style={{ fontSize: 14.5, color: C.textSub, margin: "6px 0 0" }}>
          {T.liveWorkforceOverviewSub || "Real-time workforce status, attendance, and availability."}
        </p>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 14, alignSelf: "flex-start", marginTop: 6 }}>
        {!historical && (
          <>
            <span data-testid="ov-live-pill" data-state={liveState} title={live.hint}
              style={{
                display: "inline-flex", alignItems: "center", gap: 7, padding: "5px 12px",
                borderRadius: 999, background: `${live.color}1F`, color: live.color,
                fontSize: 12, fontWeight: 800, letterSpacing: ".6px",
              }}>
              <span aria-hidden style={{ width: 8, height: 8, borderRadius: "50%", background: live.color }} />
              {live.label}
            </span>
            <span aria-hidden style={{ width: 1, height: 22, background: C.border }} />
          </>
        )}
        <span data-testid="ov-stamp" style={{ display: "inline-flex", alignItems: "center", gap: 8, fontSize: 14, color: C.text, fontWeight: 500, whiteSpace: "nowrap" }}>
          <Calendar size={17} color={C.textSub} aria-hidden />
          {stamp}
        </span>
      </div>
    </div>
  );
}

// ── Stat cards ───────────────────────────────────────────────────────────────

function BigCard(props) {
  const { C, T, label, val, color, Icon, total, hint, onClick, testId } = props;
  const pct = workforcePct(val, total);
  return (
    <button type="button" className="zt-ov-card" onClick={onClick} data-testid={testId}
      aria-label={`${label}: ${val}, ${pct}% ${T.ofWorkforce || "of workforce"}`}
      style={{
        background: tint(C, color, "10"), border: `1.5px solid ${color}55`, borderRadius: 16,
        padding: "18px 18px 0", textAlign: "left", cursor: "pointer", fontFamily: "inherit",
        color: C.text, display: "flex", flexDirection: "column", minWidth: 0,
      }}>
      <span style={{ display: "flex", alignItems: "center", gap: 14, minHeight: 60 }}>
        <span aria-hidden className="zt-ov-big-ic" style={{
          width: 56, height: 56, borderRadius: 14, flexShrink: 0,
          background: `linear-gradient(135deg, ${color}D9, ${color})`,
          display: "inline-flex", alignItems: "center", justifyContent: "center",
          boxShadow: `0 6px 14px -6px ${color}`,
        }}>
          <Icon size={28} color="#fff" strokeWidth={2.2} />
        </span>
        <span style={{ minWidth: 0 }}>
          <span data-testid={`${testId}-value`} className="zt-ov-big-num" style={{ display: "block", fontSize: 32, fontWeight: 800, lineHeight: 1, letterSpacing: "-1px" }}>{val}</span>
          <span style={{ display: "block", fontSize: 14, fontWeight: 600, marginTop: 6, lineHeight: 1.25 }}>{label}</span>
        </span>
      </span>
      <span className="zt-ov-big-foot" style={{
        display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8,
        borderTop: `1px solid ${color}2E`, marginTop: 16, padding: "10px 0 12px",
        fontSize: 12.5, color: C.textSub,
      }}>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 5, minWidth: 0 }}>
          <span data-testid={`${testId}-pct`} style={{ whiteSpace: "nowrap" }}><strong style={{ color, fontWeight: 700 }}>{pct}%</strong> {T.ofWorkforce || "of workforce"}</span>
          {hint && <span title={hint} style={{ display: "inline-flex" }}><Info size={13} aria-hidden /></span>}
        </span>
        <ChevronRight size={16} color={C.textSub} aria-hidden />
      </span>
    </button>
  );
}

function SmallCard(props) {
  const { C, T, label, val, color, Icon, total, onClick, testId } = props;
  const pct = workforcePct(val, total);
  return (
    <button type="button" className="zt-ov-card" onClick={onClick} data-testid={testId}
      aria-label={`${label}: ${val}, ${pct}% ${T.ofWorkforce || "of workforce"}`}
      style={{
        background: C.surface, border: `1px solid ${C.border}`, borderRadius: 14,
        padding: "14px 16px", display: "flex", alignItems: "center", gap: 12,
        textAlign: "left", cursor: "pointer", fontFamily: "inherit", color: C.text,
        minWidth: 0, boxShadow: CARD_SHADOW,
      }}>
      <Icon size={28} color={color} strokeWidth={1.8} aria-hidden style={{ flexShrink: 0 }} />
      <span style={{ fontSize: 14.5, fontWeight: 600, flex: "1 1 auto", minWidth: 0, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{label}</span>
      <span data-testid={`${testId}-value`} style={{ fontSize: 28, fontWeight: 800, letterSpacing: "-1px", lineHeight: 1, flexShrink: 0 }}>{val}</span>
      <span aria-hidden style={{ width: 1, alignSelf: "stretch", background: C.border, flexShrink: 0 }} />
      <span data-testid={`${testId}-pct`} style={{ fontSize: 11.5, color: C.textSub, lineHeight: 1.3, width: 58, flexShrink: 0 }}>
        <strong style={{ color }}>{pct}%</strong> {T.ofWorkforce || "of workforce"}
      </span>
    </button>
  );
}

export function OverviewStatCards({ C, T, counts, total, requiresAttention, history, onOpen }) {
  if (history) {
    // A past date: the attendance report's summary, not live states.
    const tot = Number(history.total) || 0;
    const cards = [
      { k: "present",     label: T.present || "Present",              val: history.present ?? 0,     color: OV_COLORS.working,  Icon: CircleCheck },
      { k: "absent",      label: T.absent || "Absent",                val: history.absent ?? 0,      color: OV_COLORS.absent,   Icon: UserX },
      { k: "clocked_out", label: T.clockedOutStatus || "Clocked Out", val: history.clocked_out ?? 0, color: OV_COLORS.idle,     Icon: CirclePause },
      { k: "on_leave",    label: T.onLeaveStatus || "On Leave",       val: history.on_leave ?? 0,    color: OV_COLORS.on_leave, Icon: CalendarDays },
    ];
    return (
      <div className="zt-ov-big">
        {cards.map((c) => (
          <BigCard key={c.k} C={C} T={T} {...c} total={tot} testId={`ov-card-${c.k}`} onClick={() => onOpen(c.k, true)} />
        ))}
      </div>
    );
  }

  const big = [
    { k: "working",     label: T.working || "Working",                     val: counts.working,     color: OV_COLORS.working,     Icon: Users,
      hint: T.ovHintWorking || "Clocked in and working right now." },
    { k: "on_break",    label: T.onBreak || "On Break",                    val: counts.on_break,    color: OV_COLORS.on_break,    Icon: Coffee,
      hint: T.ovHintOnBreak || "Clocked in and on a break right now." },
    { k: "not_started", label: T.awaitingClockIn || "Awaiting Clock-In",   val: counts.not_started, color: OV_COLORS.not_started, Icon: Clock,
      hint: T.ovHintAwaiting || "Not clocked in yet today, and not late yet." },
    { k: "idle",        label: T.stoppedWorking || "Stopped Working",      val: counts.idle,        color: OV_COLORS.idle,        Icon: CirclePause,
      hint: T.ovHintStopped || "Worked earlier today and is not working now." },
  ];
  return (
    <>
      <div className="zt-ov-big">
        {big.map((c) => (
          <BigCard key={c.k} C={C} T={T} {...c} total={total} testId={`ov-card-${c.k}`} onClick={() => onOpen(c.k)} />
        ))}
      </div>
      <div className="zt-ov-small">
        <SmallCard C={C} T={T} label={T.onLeave || "On Leave"} val={counts.on_leave} color={OV_COLORS.on_leave}
          Icon={CalendarDays} total={total} testId="ov-card-on_leave" onClick={() => onOpen("on_leave")} />
        <SmallCard C={C} T={T} label={T.absent || "Absent"} val={counts.absent} color={OV_COLORS.absent}
          Icon={UserX} total={total} testId="ov-card-absent" onClick={() => onOpen("absent")} />
        <button type="button" className="zt-ov-card" data-testid="ov-card-attention"
          onClick={() => onOpen("requires_attention")}
          aria-label={`${T.requiresAttention || "Requires Attention"}: ${requiresAttention}`}
          style={{
            background: tint(C, "#F59E0B", "17"), border: "1px solid #F59E0B66", borderRadius: 14,
            padding: "12px 16px", display: "flex", alignItems: "center", gap: 14, textAlign: "left",
            cursor: "pointer", fontFamily: "inherit", color: C.text, minWidth: 0,
          }}>
          <TriangleAlert size={34} color="#D97706" strokeWidth={1.8} aria-hidden style={{ flexShrink: 0 }} />
          <span style={{ flex: 1, minWidth: 0 }}>
            <span style={{ display: "flex", alignItems: "center", gap: 18 }}>
              <span style={{ fontSize: 15, fontWeight: 700 }}>{T.requiresAttention || "Requires Attention"}</span>
              <span data-testid="ov-card-attention-value" style={{ fontSize: 24, fontWeight: 800, lineHeight: 1 }}>{requiresAttention}</span>
            </span>
            <span style={{ display: "block", fontSize: 11.5, color: C.textSub, marginTop: 3, lineHeight: 1.35 }}>
              {T.ovAttentionDesc || "Workers past their start time who haven't clocked in, including today's absences."}
            </span>
          </span>
          <ChevronRight size={18} color={C.textSub} aria-hidden style={{ flexShrink: 0 }} />
        </button>
      </div>
    </>
  );
}

// ── Filter bar ───────────────────────────────────────────────────────────────

function FilterSelect({ C, value, onChange, children, active, label }) {
  return (
    <span style={{ position: "relative", display: "inline-flex" }}>
      <select value={value} onChange={(e) => onChange(e.target.value)} aria-label={label}
        style={{
          appearance: "none", WebkitAppearance: "none", MozAppearance: "none",
          padding: "9px 36px 9px 14px", minWidth: 150, borderRadius: 10,
          border: `1.5px solid ${active ? C.brand : C.border}`, background: C.surface,
          color: C.text, fontSize: 13, fontWeight: active ? 700 : 500, fontFamily: "inherit",
          outline: "none", cursor: "pointer",
        }}>
        {children}
      </select>
      <ChevronDown size={16} color={C.textSub} aria-hidden
        style={{ position: "absolute", right: 12, top: "50%", transform: "translateY(-50%)", pointerEvents: "none" }} />
    </span>
  );
}

export function OverviewFilterBar({
  C, T, isDark, statusTabs, statusFilter, setStatusFilter,
  teams, teamFilter, setTeamFilter, empTypeFilter, setEmpTypeFilter, empTypeCounts,
  viewMode, setViewMode,
}) {
  const navy = navyFor(isDark);
  const views = [
    { k: "grid",     label: T.grid || "Grid",         Icon: LayoutGrid },
    { k: "list",     label: T.list || "List",         Icon: List },
    { k: "timeline", label: T.timeline || "Timeline", Icon: CalendarDays },
  ];
  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 14, padding: 14, display: "flex", flexDirection: "column", gap: 12, boxShadow: CARD_SHADOW }}>
      <div role="group" aria-label={T.filterByStatus || "Filter by status"} style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        {statusTabs.map((s) => {
          const on = statusFilter === s.k;
          return (
            <button key={s.k} type="button" className="zt-ov-chip" aria-pressed={on}
              onClick={() => setStatusFilter(s.k)}
              style={{
                display: "inline-flex", alignItems: "center", gap: 8, padding: "8px 14px",
                borderRadius: 999, border: `1.5px solid ${on ? navy : C.border}`,
                background: on ? navy : C.surface, color: on ? "#fff" : C.text,
                fontSize: 13, fontWeight: on ? 700 : 500, cursor: "pointer", fontFamily: "inherit",
                whiteSpace: "nowrap",
              }}>
              {s.k !== "all" && <span aria-hidden style={{ width: 8, height: 8, borderRadius: "50%", background: s.dot }} />}
              {s.l.replace(/\s+/g, " ")}
            </button>
          );
        })}
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <FilterSelect C={C} value={teamFilter} onChange={setTeamFilter} active={teamFilter !== "all"} label={T.allTeams || "All Teams"}>
          {teams.map((tm) => <option key={tm} value={tm}>{tm === "all" ? (T.allTeams || "All Teams") : tm}</option>)}
        </FilterSelect>
        <FilterSelect C={C} value={empTypeFilter} onChange={setEmpTypeFilter} active={empTypeFilter !== "all"} label={T.allWorkerTypes || "All Worker Types"}>
          <option value="all">{T.allWorkerTypes || "All Worker Types"}</option>
          <option value="full_time">{T.fullTime || "Full Time"} ({empTypeCounts.full_time})</option>
          <option value="part_time">{T.partTime || "Part Time"} ({empTypeCounts.part_time})</option>
          <option value="temporary">{T.temporary || "Temporary"} ({empTypeCounts.temporary})</option>
          <option value="casual">{T.casual || "Casual"} ({empTypeCounts.casual})</option>
          <option value="archived">{T.archived || "Archived"} ({empTypeCounts.archived})</option>
        </FilterSelect>
        <span style={{ flex: 1 }} />
        <div role="group" aria-label={T.view || "View"} style={{ display: "flex", gap: 6 }}>
          {views.map((v) => {
            const { k, label, Icon } = v;
            const on = viewMode === k;
            return (
              <button key={k} type="button" className="zt-ov-seg" aria-pressed={on} onClick={() => setViewMode(k)}
                style={{
                  display: "inline-flex", alignItems: "center", gap: 7, padding: "8px 14px",
                  borderRadius: 10, border: `1.5px solid ${on ? navy : C.border}`,
                  background: on ? navy : C.surface, color: on ? "#fff" : C.text,
                  fontSize: 13, fontWeight: 600, cursor: "pointer", fontFamily: "inherit",
                }}>
                <Icon size={15} aria-hidden />{label}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ── Worker table (List view) ─────────────────────────────────────────────────

const DASH = "—";

export function OverviewWorkerTable({ C, T, isDark, emps, onView, onEdit, onDelete }) {
  const [menuFor, setMenuFor] = useState(null);
  useEffect(() => {
    if (menuFor == null) return undefined;
    const onDown = (e) => { if (!e.target.closest?.("[data-ov-menu]")) setMenuFor(null); };
    const onKey = (e) => { if (e.key === "Escape") setMenuFor(null); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey); };
  }, [menuFor]);

  const now = useMemo(() => new Date(), [emps]); // eslint-disable-line react-hooks/exhaustive-deps
  const rows = useMemo(() => emps.map((e) => ({ emp: e, v: rowView(e, now) })), [emps, now]);

  const th = {
    padding: "12px 12px", textAlign: "left", fontSize: 12.5, fontWeight: 700, color: C.text,
    borderBottom: `1px solid ${C.border}`, background: C.surfaceAlt, lineHeight: 1.25,
  };
  const td = { padding: "12px 12px", fontSize: 13, color: C.text, borderBottom: `1px solid ${C.borderLight}`, whiteSpace: "nowrap" };
  const headers = [
    T.employee || "Employee", T.status || "Status", T.workerType || "Worker Type",
    T.clockInCol || "Clock-In", T.workedToday || "Worked Today", T.active_time || "Active Time",
    T.progress || "Progress",
  ];

  return (
    <div className="zt-ov-table-wrap" data-testid="ov-worker-table"
      style={{
        background: C.surface, border: `1px solid ${C.border}`, borderRadius: 14, boxShadow: CARD_SHADOW,
        "--zt-ov-row-hover": isDark ? C.surfaceAlt : "#F8FAFC",
      }}>
      <table style={{ width: "100%", minWidth: 720, borderCollapse: "collapse" }}>
        <thead>
          <tr>
            {headers.map((h) => <th key={h} scope="col" style={th}>{h}</th>)}
            <th scope="col" style={{ ...th, width: 44 }}><span className="sr-only" style={{ position: "absolute", width: 1, height: 1, overflow: "hidden", clip: "rect(0 0 0 0)" }}>{T.actions || "Actions"}</span></th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ emp, v }) => {
            const [abg, afg] = avatarTone(emp.name || "");
            const dot = v.cfg.dot;
            return (
              <tr key={emp.id} className="zt-ov-row" style={{ cursor: "pointer" }} onClick={() => onView(emp)}>
                <td style={td}>
                  <div style={{ display: "flex", alignItems: "center", gap: 12, minWidth: 0 }}>
                    <span aria-hidden style={{ width: 36, height: 36, borderRadius: 10, background: abg, color: afg, display: "inline-flex", alignItems: "center", justifyContent: "center", fontWeight: 800, fontSize: 13, flexShrink: 0 }}>
                      {initials(emp.name)}
                    </span>
                    <span style={{ minWidth: 0 }}>
                      <span style={{ display: "block", fontWeight: 700, fontSize: 13.5, overflow: "hidden", textOverflow: "ellipsis", maxWidth: 180 }}>{emp.name}</span>
                      <span style={{ display: "block", fontSize: 12, color: C.textSub, overflow: "hidden", textOverflow: "ellipsis", maxWidth: 180 }}>{emp.designation || emp.email}</span>
                    </span>
                  </div>
                </td>
                <td style={td}>
                  <span data-testid="ov-row-status" style={{
                    display: "inline-flex", alignItems: "center", gap: 6, padding: "4px 10px", borderRadius: 999,
                    background: `${dot}1F`, color: isDark ? dot : v.cfg.color, fontSize: 12, fontWeight: 700,
                  }}>
                    <span aria-hidden style={{ width: 7, height: 7, borderRadius: "50%", background: dot }} />
                    {v.cfg.label}
                  </span>
                </td>
                <td style={td}>
                  <span style={{ display: "inline-block", padding: "4px 10px", borderRadius: 8, background: isDark ? C.surfaceAlt : "#F1F5F9", border: `1px solid ${C.border}`, fontSize: 12, fontWeight: 600 }}>
                    {v.typeLabel}
                  </span>
                </td>
                <td style={td}>{v.clockIn || DASH}</td>
                <td style={td}>{v.worked || DASH}</td>
                <td style={td}>{v.activePct != null ? <strong>{v.activePct}%</strong> : DASH}</td>
                <td style={td} title={v.progressTitle}>
                  {v.progress != null ? (
                    <span style={{ display: "inline-flex", flexDirection: "column", gap: 4, width: 84 }}>
                      <span style={{ fontSize: 12, fontWeight: 700 }}>{v.progress}%</span>
                      <span aria-hidden style={{ height: 6, borderRadius: 999, background: isDark ? "#262626" : "#E5E7EB", overflow: "hidden" }}>
                        <span style={{ display: "block", height: "100%", width: `${v.progress}%`, background: OV_COLORS.working, borderRadius: 999 }} />
                      </span>
                    </span>
                  ) : DASH}
                </td>
                <td style={{ ...td, position: "relative" }} onClick={(e) => e.stopPropagation()}>
                  <span data-ov-menu style={{ position: "relative", display: "inline-flex" }}>
                    <button type="button" aria-label={`${T.actions || "Actions"}: ${emp.name}`} aria-haspopup="menu" aria-expanded={menuFor === emp.id}
                      onClick={() => setMenuFor((m) => (m === emp.id ? null : emp.id))}
                      style={{ width: 32, height: 32, borderRadius: 8, border: "none", background: menuFor === emp.id ? C.bg : "transparent", color: C.text, cursor: "pointer", display: "inline-flex", alignItems: "center", justifyContent: "center" }}>
                      <Ellipsis size={18} aria-hidden />
                    </button>
                    {menuFor === emp.id && (
                      <span role="menu" style={{
                        position: "absolute", right: 0, top: 36, zIndex: 20, minWidth: 150, padding: 6,
                        background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10,
                        boxShadow: "0 12px 32px rgba(16,24,40,.16)", display: "flex", flexDirection: "column",
                      }}>
                        {[
                          { k: "view", label: T.adminDashboardListView || "View", fn: onView, color: C.text },
                          { k: "edit", label: T.adminDashboardListEdit || "Edit", fn: onEdit, color: C.text },
                          { k: "delete", label: T.adminDashboardListDelete || "Delete", fn: onDelete, color: C.red },
                        ].map((a) => (
                          <button key={a.k} type="button" role="menuitem"
                            onClick={() => { setMenuFor(null); a.fn(emp); }}
                            style={{ textAlign: "left", padding: "8px 10px", borderRadius: 6, border: "none", background: "transparent", color: a.color, fontSize: 13, fontWeight: 600, cursor: "pointer", fontFamily: "inherit" }}
                            onMouseEnter={(e) => { e.currentTarget.style.background = C.bg; }}
                            onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}>
                            {a.label}
                          </button>
                        ))}
                      </span>
                    )}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ── Right column: summary, distribution, quick actions ───────────────────────

const panel = (C) => ({
  background: C.surface, border: `1px solid ${C.border}`, borderRadius: 16,
  padding: "18px 20px", boxShadow: CARD_SHADOW, minWidth: 0,
});

function PanelTitle(props) {
  const { C, Icon, children } = props;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 12, paddingBottom: 12, borderBottom: `1px solid ${C.borderLight}`, marginBottom: 6 }}>
      <Icon size={24} color={C.text} strokeWidth={1.9} aria-hidden />
      <h2 style={{ fontSize: 18, fontWeight: 800, color: C.text, margin: 0, letterSpacing: "-.2px" }}>{children}</h2>
    </div>
  );
}

function Donut({ C, T, rows, total, size = 124, stroke = 20 }) {
  const r = (size - stroke) / 2;
  const c = size / 2;
  const circ = 2 * Math.PI * r;
  const live = rows.filter((x) => x.value > 0);
  const sum = live.reduce((s, x) => s + x.value, 0);
  const gap = live.length > 1 ? 2 : 0;
  const segs = live.reduce((acc, x) => {
    const len = sum > 0 ? (x.value / sum) * circ : 0;
    const start = acc.length ? acc[acc.length - 1].end : 0;
    acc.push({ ...x, len, start, end: start + len });
    return acc;
  }, []);
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" style={{ flexShrink: 0 }}
      aria-label={live.map((x) => `${x.label} ${x.value}`).join(", ") || `${total}`}>
      <circle cx={c} cy={c} r={r} fill="none" stroke={C.borderLight} strokeWidth={stroke} />
      {segs.map((s) => {
        const visible = Math.max(0, s.len - gap);
        return (
          <circle key={s.k} cx={c} cy={c} r={r} fill="none" stroke={s.color} strokeWidth={stroke}
            strokeDasharray={`${visible} ${circ - visible}`} strokeDashoffset={-s.start}
            transform={`rotate(-90 ${c} ${c})`} />
        );
      })}
      <text x={c} y={c + 2} textAnchor="middle" fontSize="26" fontWeight="800" fill={C.text}>{total}</text>
      <text x={c} y={c + 20} textAnchor="middle" fontSize="12.5" fontWeight="500" fill={C.textSub}>{T.totalWord || "Total"}</text>
    </svg>
  );
}

export function OverviewSidePanel({ C, T, counts, total, history, dateLabel, onAddWorker, onNavigate }) {
  const summaryRows = history
    ? [
        { k: "total",       label: T.totalTeamMembers || "Total Team Members", val: Number(history.total) || 0,   Icon: Users,        color: C.text },
        { k: "present",     label: T.present || "Present",                     val: history.present ?? 0,          Icon: CircleCheck,  color: OV_COLORS.working },
        { k: "on_leave",    label: T.onLeave || "On Leave",                    val: history.on_leave ?? 0,         Icon: CalendarDays, color: OV_COLORS.on_leave },
        { k: "absent",      label: T.absent || "Absent",                       val: history.absent ?? 0,           Icon: UserX,        color: OV_COLORS.absent },
        { k: "clocked_out", label: T.clockedOutStatus || "Clocked Out",        val: history.clocked_out ?? 0,      Icon: CirclePause,  color: OV_COLORS.idle },
      ]
    : [
        { k: "total",    label: T.totalTeamMembers || "Total Team Members", val: total, Icon: Users, color: C.text },
        {
          k: "present",
          label: (counts.in_meeting || 0) > 0
            ? (T.presentWorkingBreakMeeting || "Present (Working, Break, Meeting)")
            : (T.presentWorkingBreak || "Present (Working + On Break)"),
          val: (counts.working || 0) + (counts.on_break || 0) + (counts.in_meeting || 0),
          Icon: CircleCheck, color: OV_COLORS.working,
        },
        { k: "on_leave", label: T.onLeave || "On Leave",               val: counts.on_leave || 0, Icon: CalendarDays, color: OV_COLORS.on_leave },
        { k: "absent",   label: T.absent || "Absent",                  val: counts.absent || 0,   Icon: UserX,        color: OV_COLORS.absent },
        { k: "idle",     label: T.stoppedWorking || "Stopped Working", val: counts.idle || 0,     Icon: CirclePause,  color: OV_COLORS.idle },
      ];

  const distTotal = history ? Number(history.total) || 0 : total;
  const dist = history ? historyDistributionRows(history, distTotal, T) : distributionRows(counts, total, T);

  const actions = [
    { k: "add",      label: T.addWorker || "Add Worker",           Icon: UserPlus,     onClick: onAddWorker },
    { k: "leave",    label: T.requestLeave || "Request Leave",     Icon: CalendarPlus, onClick: () => onNavigate("/leave") },
    { k: "report",   label: T.runReport || "Run Report",           Icon: FileText,     onClick: () => onNavigate("/admin?tab=reports") },
    { k: "policies", label: T.managePolicies || "Manage Policies", Icon: Settings,     onClick: () => onNavigate("/settings/policies") },
  ];

  return (
    <>
      <section style={panel(C)} data-testid="ov-summary">
        <PanelTitle C={C} Icon={CalendarDays}>
          {history ? `${T.summaryWord || "Summary"} · ${dateLabel}` : (T.todaysSummary || "Today's Summary")}
        </PanelTitle>
        {summaryRows.map((r, i) => (
          <div key={r.k} style={{ display: "flex", alignItems: "center", gap: 12, padding: "9px 0", borderBottom: i < summaryRows.length - 1 ? `1px solid ${C.borderLight}` : "none" }}>
            <r.Icon size={20} color={r.color} strokeWidth={1.9} aria-hidden />
            <span style={{ flex: 1, fontSize: 13.5, color: C.text }}>{r.label}</span>
            <span data-testid={`ov-summary-${r.k}`} style={{ fontSize: 16, fontWeight: 800, color: C.text }}>{r.val}</span>
          </div>
        ))}
      </section>

      <section style={panel(C)} data-testid="ov-distribution">
        <PanelTitle C={C} Icon={ChartPie}>{T.workforceDistribution || "Workforce Distribution"}</PanelTitle>
        <div style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap", paddingTop: 8 }}>
          <Donut C={C} T={T} rows={dist} total={distTotal} />
          <div style={{ flex: "1 1 130px", display: "flex", flexDirection: "column", gap: 7, minWidth: 0 }}>
            {dist.map((r) => (
              <div key={r.k} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12.5, color: C.text }}>
                <span aria-hidden style={{ width: 9, height: 9, borderRadius: "50%", background: r.color, flexShrink: 0 }} />
                <span style={{ flex: 1, minWidth: 0, lineHeight: 1.25 }}>{r.label}</span>
                <span data-testid={`ov-dist-${r.k}`} style={{ whiteSpace: "nowrap" }}>
                  <strong>{r.value}</strong> <span style={{ color: C.textSub }}>({workforcePct(r.value, distTotal)}%)</span>
                </span>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section style={panel(C)} data-testid="ov-quick-actions">
        <PanelTitle C={C} Icon={Zap}>{T.quickActions || "Quick Actions"}</PanelTitle>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: 8, paddingTop: 8 }}>
          {actions.map((a) => (
            <button key={a.k} type="button" className="zt-ov-qa" onClick={a.onClick}
              style={{
                display: "flex", flexDirection: "column", alignItems: "center", gap: 8,
                padding: "14px 4px", borderRadius: 12, border: `1px solid ${C.border}`,
                background: C.surface, color: C.text, cursor: "pointer", fontFamily: "inherit",
                fontSize: 12, fontWeight: 600, lineHeight: 1.25, textAlign: "center", minWidth: 0,
              }}>
              <a.Icon size={26} color={C.brand} strokeWidth={1.8} aria-hidden />
              {a.label}
            </button>
          ))}
        </div>
      </section>
    </>
  );
}

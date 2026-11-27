import React, { useState } from "react";
import {
  PieChart, Pie, Cell, Sector,
  AreaChart, Area,
  BarChart, Bar,
  XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  RadialBarChart, RadialBar,
  ReferenceLine,
} from "recharts";
// `Ico` is a shared icon helper that stays in Dashboard.jsx (used across the
// whole employee dashboard). It is re-exported there so this lazy chunk can
// reuse it without duplicating the PATHS map. recharts lives here so the
// Dashboard route chunk no longer eagerly downloads vendor-charts (~321KB);
// it only loads when the user opens the AI Daily Summary tab.
import { Ico } from "../pages/Dashboard";

// ─── DAILY SUMMARY VIEW ────────────────────────────────────────────────────
// Self-contained employee Daily Summary tab. All data is dynamic — driven by
// the props supplied from Dashboard (which fetches /ai/summary/{uid} and
// /ai/summary-weekly/{uid}). The component owns its own chart layout, date
// picker, and refresh button; Dashboard just hands in the data + handlers.
function DailySummaryView({
  summary, weekly, loading, isDark, C, T,
  date, onDateChange, onRefresh,
}) {
  // ── Chart interaction state ─────────────────────────────────────────────
  // Donut hover index drives the "active shape" — the focused segment expands
  // a few pixels and the centre overlay swaps to that segment's value/label.
  const [donutHover, setDonutHover] = useState(-1);
  // Area chart series visibility — clickable legend toggles Hours / Productivity.
  const [seriesOn, setSeriesOn] = useState({ hours: true, productivity: true });
  // Top app pinned via click — highlighted bar + small "View activity" hint.
  const [pinnedApp, setPinnedApp] = useState(null);
  // Which stat card is expanded inline (one at a time). null = none.
  const [expandedCard, setExpandedCard] = useState(null);

  const card = (ex = {}) => ({
    background: C.surface,
    borderRadius: 14,
    border: `1px solid ${C.border}`,
    boxShadow: isDark ? "0 1px 3px rgba(0,0,0,0.3)" : "0 1px 3px rgba(0,0,0,0.04)",
    ...ex,
  });

  // Today's date (YYYY-MM-DD in local time) — used as max for the picker so
  // workers can't request "tomorrow's" summary.
  const todayLocal = (() => {
    const d = new Date();
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${dd}`;
  })();
  const viewingToday = !date || date === todayLocal;

  // ── Loading skeleton ────────────────────────────────────────────────────
  if (loading) {
    return (
      <div style={{ maxWidth: 1280, display: "flex", flexDirection: "column", gap: 16 }}>
        <div style={{ ...card(), padding: 28, textAlign: "center" }}>
          <div style={{
            width: 28, height: 28, margin: "0 auto 12px",
            border: `3px solid ${C.brandBg}`, borderTopColor: C.brand,
            borderRadius: "50%", animation: "zt-spin 0.8s linear infinite",
          }} />
          <p style={{ fontSize: 13, color: C.textSub }}>{T.loading || "Loading your summary…"}</p>
          <style>{`@keyframes zt-spin{to{transform:rotate(360deg)}}`}</style>
        </div>
      </div>
    );
  }

  // ── Empty state ─────────────────────────────────────────────────────────
  if (!summary) {
    return (
      <div style={{ maxWidth: 1280 }}>
        <div style={{ ...card(), padding: 40, textAlign: "center" }}>
          <Ico n="info" s={32} c={C.textLight} />
          <p style={{ fontSize: 14, color: C.textSub, marginTop: 12 }}>
            {T.aiNoData || "No activity data available for this day."}
          </p>
          <div style={{ marginTop: 16, display: "flex", gap: 8, justifyContent: "center", flexWrap: "wrap" }}>
            <input
              type="date"
              value={date || todayLocal}
              max={todayLocal}
              onChange={(e) => onDateChange(e.target.value)}
              style={{ padding: "8px 12px", borderRadius: 8, border: `1px solid ${C.border}`, background: C.surface, color: C.text, fontSize: 13, fontFamily: "inherit" }}
            />
            <button
              onClick={onRefresh}
              style={{ display: "flex", alignItems: "center", gap: 6, padding: "8px 14px", borderRadius: 8, border: `1px solid ${C.border}`, background: C.surface, color: C.text, fontSize: 13, fontWeight: 600, cursor: "pointer", fontFamily: "inherit" }}
            >
              <Ico n="refresh" s={13} c={C.text} />
              {T.refresh || "Refresh"}
            </button>
          </div>
        </div>
      </div>
    );
  }

  // ── Derived numbers ─────────────────────────────────────────────────────
  const hoursWorked   = Number(summary.hours_worked || 0);
  const productiveHrs = Number(summary.productive_hours || 0);
  const idleHrs       = Number(summary.idle_hours || 0);
  const breakMins     = Number(summary.break_minutes || 0);
  const breakHrs      = breakMins / 60;
  const prodPct       = Number(summary.productivity_percentage || 0);
  const idlePct       = Number(summary.idle_percentage || 0);
  const sessions      = Number(summary.sessions_count || 0);
  const screenshots   = Number(summary.activity_count || 0);
  const mouseClicks   = Number(summary.total_mouse_clicks || 0);
  const keyPresses    = Number(summary.total_key_presses || 0);
  const distraction   = summary.distraction_level || "Low";

  // Daily target. 8h is the universal full-time default; surface it as a
  // single constant so a future /worker/settings field can override it.
  const DAILY_TARGET_HOURS = 8;
  const hoursRemaining = Math.max(0, DAILY_TARGET_HOURS - hoursWorked);
  const goalPct        = Math.min(100, Math.max(0, (hoursWorked / DAILY_TARGET_HOURS) * 100));

  const distractionTone = distraction === "Low" ? "green" : distraction === "Medium" ? "amber" : "red";
  const distractionColor = distractionTone === "green" ? C.green : distractionTone === "amber" ? C.amber : C.red;
  const distractionBg    = distractionTone === "green" ? C.greenBg : distractionTone === "amber" ? C.amberBg : C.redBg;
  const distractionBdr   = distractionTone === "green" ? C.greenBdr : distractionTone === "amber" ? C.amberBdr : C.redBdr;

  // Time-composition palette — kept distinct so the three segments are
  // immediately readable even at small sizes. Each segment also paints a
  // gradient (see <defs> in the donut) for the colourful look.
  const COL_PROD  = "#22C55E";    // vivid green
  const COL_IDLE  = "#F59E0B";    // amber
  const COL_BREAK = "#3B82F6";    // brand-blue mate

  // Productivity donut: split work time into productive + idle + break.
  // NOTE: we no longer filter zero values — every category renders in the
  // legend even when its slice is 0h, so users can see "Break: 0m" at a
  // glance and the donut never looks half-empty.
  const donutData = [
    { name: T.productive    || "Productive", value: Math.max(0, productiveHrs), color: COL_PROD,  gid: "ztDonutProd"  },
    { name: T.idleHoursLabel|| "Idle",       value: Math.max(0, idleHrs),       color: COL_IDLE,  gid: "ztDonutIdle"  },
    { name: T.breakMinutes  || "Break",      value: Math.max(0, breakHrs),      color: COL_BREAK, gid: "ztDonutBreak" },
  ];
  const donutTotal = donutData.reduce((a, b) => a + b.value, 0);

  // Weekly area data — fall back to a 7-empty-day placeholder if backend
  // didn't return weekly. Each entry needs `day` (label) + `hours` (number)
  // + `productivity` (0-100). aiWeekly returns: [{date, day, hours_worked, productivity_percentage, break_minutes}, ...]
  const weeklyData = (weekly?.days || []).map(d => ({
    day:          d.day,
    date:         d.date,
    hours:        Number(d.hours_worked || 0),
    productivity: Number(d.productivity_percentage || 0),
    breakMin:     Number(d.break_minutes || 0),
  }));
  const weeklyTotal = weeklyData.reduce((a, b) => a + b.hours, 0);
  const weeklyAvg   = weeklyData.length > 0 ? weeklyTotal / weeklyData.length : 0;
  const weeklyBest  = weeklyData.reduce((best, d) => (d.hours > (best?.hours ?? -1) ? d : best), null);

  // Top apps — sort + cap. Each bar gets its own vibrant brand palette colour
  // so a multi-app day is visually scanable.
  const APP_PALETTE = [
    "#22C55E", // green
    "#3B82F6", // blue
    "#A855F7", // violet
    "#F59E0B", // amber
    "#EF4444", // red
    "#06B6D4", // cyan
  ];
  const topApps = (summary.top_apps || []).slice(0, 6).map((a, i) => ({
    name:  a.app_name || "Unknown",
    count: a.count || 0,
    color: APP_PALETTE[i % APP_PALETTE.length],
    gid:   `ztBarApp${i}`,
  }));

  // Stat-card deltas — always computed so the comparison panel never goes
  // missing. weeklyAvg falls back to 0 when there's no history so the first
  // day after onboarding still shows a meaningful "+X.Xh" vs. zero.
  const hoursVsAvgDelta = hoursWorked - weeklyAvg;
  const yesterdayHours  = weeklyData.length >= 2 ? weeklyData[weeklyData.length - 2].hours : null;
  const hoursVsYesterday = yesterdayHours != null ? hoursWorked - yesterdayHours : null;

  // ── Tooltip styling (used by all charts) ────────────────────────────────
  const tooltipStyle = {
    background:    C.surface,
    border:        `1px solid ${C.border}`,
    borderRadius:  10,
    padding:       "8px 12px",
    fontSize:      12,
    color:         C.text,
    boxShadow:     "0 6px 18px rgba(0,0,0,0.12)",
  };
  const fmtH = (n) => `${Number(n).toFixed(1)}h`;

  // ────────────────────────────────────────────────────────────────────────
  return (
    <div style={{ maxWidth: 1280, display: "flex", flexDirection: "column", gap: 16, animation: "fadeIn 0.2s" }}>

      {/* ── Header row ── */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <div style={{
          width: 38, height: 38, borderRadius: 10, flexShrink: 0,
          background: "linear-gradient(135deg,#44B97B,#11496A)",
          display: "flex", alignItems: "center", justifyContent: "center",
        }}>
          <Ico n="insights" s={20} c="#fff" />
        </div>
        <div style={{ flex: 1, minWidth: 200 }}>
          <h2 style={{ fontSize: 20, fontWeight: 800, color: C.text, margin: 0, letterSpacing: "-.2px" }}>
            {T.aiDailySummary || "Daily Summary"}
          </h2>
          <p style={{ fontSize: 12, color: C.textSub, margin: "2px 0 0" }}>
            {viewingToday
              ? (T.todayLive || "Live snapshot of today's activity")
              : (T.historicalDay || "Historical view · {date}").replace("{date}", date)}
          </p>
        </div>
        {viewingToday && (
          <span style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 11, fontWeight: 700, padding: "5px 10px", borderRadius: 99, background: `${C.green}15`, color: C.green }}>
            <span style={{ width: 6, height: 6, borderRadius: "50%", background: C.green, animation: "pulse 2s infinite" }} />
            {T.live || "LIVE"}
          </span>
        )}
        <input
          type="date"
          value={date || todayLocal}
          max={todayLocal}
          onChange={(e) => onDateChange(e.target.value)}
          aria-label={T.pickDate || "Pick date"}
          style={{ padding: "7px 10px", borderRadius: 8, border: `1px solid ${C.border}`, background: C.surface, color: C.text, fontSize: 12, fontFamily: "inherit" }}
        />
        <button
          onClick={onRefresh}
          title={T.refresh || "Refresh"}
          aria-label={T.refresh || "Refresh"}
          style={{ display: "flex", alignItems: "center", justifyContent: "center", width: 34, height: 34, borderRadius: 8, border: `1px solid ${C.border}`, background: C.surface, color: C.text, cursor: "pointer", fontFamily: "inherit" }}
        >
          <Ico n="refresh" s={14} c={C.text} />
        </button>
        <style>{`@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}`}</style>
      </div>

      {/* ── AI Insight banner ── */}
      {summary.summary && (
        <div style={{
          ...card({
            padding: 18,
            background: isDark
              ? "linear-gradient(135deg, #1A1A1A 0%, #0F0F0F 100%)"
              : "linear-gradient(135deg, #F5F3FF 0%, #E7F0F6 100%)",
            border: `1px solid ${isDark ? "#4C1D95" : "#C4B5FD"}`,
          }),
        }}>
          <div style={{ display: "flex", alignItems: "flex-start", gap: 14 }}>
            <div style={{
              width: 38, height: 38, borderRadius: 10, flexShrink: 0,
              background: "linear-gradient(135deg,#44B97B,#3B82F6)",
              display: "flex", alignItems: "center", justifyContent: "center",
            }}>
              <Ico n="star" s={18} c="#fff" />
            </div>
            <div style={{ flex: 1 }}>
              <p style={{ fontSize: 12, fontWeight: 700, color: C.brand, marginBottom: 4, letterSpacing: ".4px", textTransform: "uppercase" }}>
                {T.aiInsight || "AI Insight"}
              </p>
              <p style={{ fontSize: 14, color: C.text, lineHeight: 1.55, margin: 0 }}>
                {summary.summary}
              </p>
            </div>
          </div>
        </div>
      )}

      {/* ── Stat cards (4) — each is interactive: click to expand a detail
          panel with backend-driven metrics specific to that metric. ── */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(230px,1fr))", gap: 14, alignItems: "start" }}>
        <StatCard C={C} card={card}
          label={T.hoursWorked || "Hours Worked"}
          value={
            <span>
              {fmtH(hoursWorked)}
              <span style={{ fontSize: 14, fontWeight: 600, color: C.textLight, marginLeft: 4 }}>
                {" / "}{fmtH(DAILY_TARGET_HOURS)}
              </span>
            </span>
          }
          icon="clock" tone={{ color: C.brand, bg: C.brandBg, bdr: C.brandBdr }}
          progress={goalPct}
          footer={
            <div style={{ display: "flex", justifyContent: "space-between", gap: 8, marginTop: 6 }}>
              <span style={{ fontSize: 11, color: C.textSub, fontWeight: 600 }}>
                <span style={{ color: C.textLight }}>{T.remaining || "Remaining"}: </span>
                <strong style={{ color: hoursRemaining > 0 ? C.amber : C.green }}>{fmtH(hoursRemaining)}</strong>
              </span>
              <span style={{ fontSize: 11, fontWeight: 700, color: hoursVsAvgDelta >= 0 ? C.green : C.amber }}>
                {hoursVsAvgDelta >= 0 ? "▲" : "▼"} {Math.abs(hoursVsAvgDelta).toFixed(1)}h
                <span style={{ color: C.textLight, fontWeight: 500, marginLeft: 4 }}>{T.dashboardVsAvg || "vs avg"}</span>
              </span>
            </div>
          }
          expanded={expandedCard === "hours"}
          onClick={() => setExpandedCard(c => c === "hours" ? null : "hours")}
          expandedContent={(
            // 7-day mini sparkline using real weekly data from /ai/summary-weekly.
            <>
              <p style={{ fontSize: 11, color: C.textSub, fontWeight: 700, margin: "0 0 6px", textTransform: "uppercase", letterSpacing: ".5px" }}>
                {T.sevenDayHours || "7-day hours"}
              </p>
              {weeklyData.length > 0 ? (
                <>
                  <div style={{ height: 70 }}>
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart
                        data={weeklyData}
                        margin={{ top: 4, right: 0, left: 0, bottom: 0 }}
                        onClick={(e) => {
                          const row = e?.activePayload?.[0]?.payload;
                          if (row?.date) onDateChange(row.date);
                        }}
                        style={{ cursor: "pointer" }}
                      >
                        <Tooltip
                          contentStyle={tooltipStyle}
                          cursor={{ fill: `${C.brand}15` }}
                          formatter={(v) => [`${Number(v).toFixed(1)}h`, T.hours || "Hours"]}
                          labelStyle={{ color: C.textSub, fontWeight: 700, fontSize: 11 }}
                        />
                        <Bar
                          dataKey="hours"
                          radius={[4, 4, 0, 0]}
                        >
                          {weeklyData.map((d, i) => {
                            const isToday = d.date === date || (!date && i === weeklyData.length - 1);
                            const reached = d.hours >= DAILY_TARGET_HOURS;
                            const fill = reached ? C.green : isToday ? C.brand : `${C.brand}66`;
                            return (
                              <Cell
                                key={i}
                                fill={fill}
                                stroke={isToday ? C.text : "none"}
                                strokeWidth={isToday ? 1.5 : 0}
                                cursor="pointer"
                              />
                            );
                          })}
                        </Bar>
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: C.textLight, marginTop: 4 }}>
                    {weeklyData.map((d, i) => {
                      const isToday = d.date === date || (!date && i === weeklyData.length - 1);
                      return (
                        <span key={d.date} style={{ flex: 1, textAlign: "center", fontWeight: isToday ? 700 : 500, color: isToday ? C.brand : C.textLight }}>
                          {d.day}
                        </span>
                      );
                    })}
                  </div>
                </>
              ) : (
                <p style={{ fontSize: 11, color: C.textLight, margin: 0 }}>
                  {T.noWeeklyYet || "Weekly data will appear once you log activity."}
                </p>
              )}
              {weeklyBest && (
                <p style={{ fontSize: 10, color: C.textSub, marginTop: 6, fontWeight: 600 }}>
                  🏆 {T.best || "Best"}: <strong style={{ color: C.green }}>{weeklyBest.day} · {fmtH(weeklyBest.hours)}</strong>
                </p>
              )}
            </>
          )}
        />

        <StatCard C={C} card={card}
          label={T.productivityPct || "Productivity"}
          value={`${prodPct}%`}
          icon="check" tone={{ color: C.green, bg: C.greenBg, bdr: C.greenBdr }}
          progress={prodPct}
          footer={
            <div style={{ fontSize: 11, color: C.textSub, fontWeight: 600, marginTop: 6 }}>
              <span style={{ color: C.textLight }}>{T.productive || "Productive"}: </span>
              <strong style={{ color: C.green }}>{fmtH(productiveHrs)}</strong>
              <span style={{ color: C.textLight, marginLeft: 8 }}>{T.dashboardOf || "of"} {fmtH(hoursWorked)}</span>
            </div>
          }
          expanded={expandedCard === "productivity"}
          onClick={() => setExpandedCard(c => c === "productivity" ? null : "productivity")}
          expandedContent={(
            <>
              <p style={{ fontSize: 11, color: C.textSub, fontWeight: 700, margin: "0 0 8px", textTransform: "uppercase", letterSpacing: ".5px" }}>
                {T.todayBreakdown || "Today's breakdown"}
              </p>
              {/* Stacked horizontal bar: productive | idle | break, scaled to hoursWorked. */}
              {hoursWorked > 0 ? (
                <>
                  <div style={{ display: "flex", height: 14, borderRadius: 6, overflow: "hidden", background: C.borderLight }}>
                    <div title={`Productive ${productiveHrs.toFixed(1)}h`} style={{ width: `${(productiveHrs/hoursWorked)*100}%`, background: "linear-gradient(90deg,#34D399,#0F9F5C)", transition: "width .5s" }} />
                    <div title={`Idle ${idleHrs.toFixed(1)}h`} style={{ width: `${(idleHrs/hoursWorked)*100}%`, background: "linear-gradient(90deg,#FBBF24,#D97706)", transition: "width .5s" }} />
                    <div title={`Break ${(breakMins/60).toFixed(1)}h`} style={{ width: `${(breakHrs/hoursWorked)*100}%`, background: "linear-gradient(90deg,#60A5FA,#1D4ED8)", transition: "width .5s" }} />
                  </div>
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: C.textSub, marginTop: 6, fontWeight: 600 }}>
                    <span><span style={{ display: "inline-block", width: 6, height: 6, borderRadius: 2, background: "#22C55E", marginRight: 4 }} />{T.productive || "Productive"} <strong style={{ color: C.text }}>{Math.round((productiveHrs/hoursWorked)*100)}%</strong></span>
                    <span><span style={{ display: "inline-block", width: 6, height: 6, borderRadius: 2, background: "#F59E0B", marginRight: 4 }} />{T.idle || "Idle"} <strong style={{ color: C.text }}>{Math.round((idleHrs/hoursWorked)*100)}%</strong></span>
                    <span><span style={{ display: "inline-block", width: 6, height: 6, borderRadius: 2, background: "#3B82F6", marginRight: 4 }} />{T.breakLabel || "Break"} <strong style={{ color: C.text }}>{Math.round((breakHrs/hoursWorked)*100)}%</strong></span>
                  </div>
                </>
              ) : (
                <p style={{ fontSize: 11, color: C.textLight, margin: 0 }}>{T.noTimeYet || "No time logged yet."}</p>
              )}
              {/* Inline activity rate */}
              <p style={{ fontSize: 10, color: C.textSub, marginTop: 10, fontWeight: 600 }}>
                ⌨ {keyPresses.toLocaleString()} {T.keyPresses || "key presses"}
                <span style={{ marginLeft: 10 }}>🖱 {mouseClicks.toLocaleString()} {T.mouseClicks || "clicks"}</span>
              </p>
            </>
          )}
        />

        <StatCard C={C} card={card}
          label={T.idleTimePct || "Idle Time"}
          value={`${idlePct}%`}
          icon="moon" tone={{ color: C.amber, bg: C.amberBg, bdr: C.amberBdr }}
          progress={idlePct}
          footer={
            <div style={{ fontSize: 11, color: C.textSub, fontWeight: 600, marginTop: 6 }}>
              <span style={{ color: C.textLight }}>{T.idle || "Idle"}: </span>
              <strong style={{ color: C.amber }}>{fmtH(idleHrs)}</strong>
              <span style={{ color: C.textLight, marginLeft: 8 }}>+ {breakMins}m {T.breakLabel || "break"}</span>
            </div>
          }
          expanded={expandedCard === "idle"}
          onClick={() => setExpandedCard(c => c === "idle" ? null : "idle")}
          expandedContent={(
            <>
              <p style={{ fontSize: 11, color: C.textSub, fontWeight: 700, margin: "0 0 8px", textTransform: "uppercase", letterSpacing: ".5px" }}>
                {T.idleBreakdown || "Idle breakdown"}
              </p>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
                <div style={{ padding: "8px 10px", borderRadius: 8, background: `${C.amber}15`, border: `1px solid ${C.amber}33` }}>
                  <p style={{ fontSize: 10, color: C.textSub, margin: 0, fontWeight: 700, textTransform: "uppercase", letterSpacing: ".4px" }}>{T.idleHoursLabel || "Idle"}</p>
                  <p style={{ fontSize: 16, fontWeight: 800, color: C.amber, margin: "2px 0 0" }}>{fmtH(idleHrs)}</p>
                </div>
                <div style={{ padding: "8px 10px", borderRadius: 8, background: `${C.brand}15`, border: `1px solid ${C.brand}33` }}>
                  <p style={{ fontSize: 10, color: C.textSub, margin: 0, fontWeight: 700, textTransform: "uppercase", letterSpacing: ".4px" }}>{T.breakLabel || "Break"}</p>
                  <p style={{ fontSize: 16, fontWeight: 800, color: C.brand, margin: "2px 0 0" }}>{breakMins}m</p>
                </div>
              </div>
              <p style={{ fontSize: 10, color: C.textLight, marginTop: 8, lineHeight: 1.5 }}>
                {idlePct < 15
                  ? (T.idleHintLow  || "Great focus — idle time is well within the healthy range.")
                  : idlePct < 30
                  ? (T.idleHintMid  || "Some idle time is normal. Short breaks help sustain focus.")
                  : (T.idleHintHigh || "High idle time. Consider Pomodoro-style sprints to lift focus.")}
              </p>
            </>
          )}
        />

        <StatCard C={C} card={card}
          label={T.distractionLevel || "Distraction"}
          value={distraction}
          icon="info" tone={{ color: distractionColor, bg: distractionBg, bdr: distractionBdr }}
          footer={
            hoursVsYesterday != null && (
              <div style={{ fontSize: 11, fontWeight: 700, marginTop: 6, color: hoursVsYesterday >= 0 ? C.green : C.amber }}>
                {hoursVsYesterday >= 0 ? "▲" : "▼"} {Math.abs(hoursVsYesterday).toFixed(1)}h
                <span style={{ color: C.textLight, fontWeight: 500, marginLeft: 4 }}>{T.vsYesterday || "vs yesterday"}</span>
              </div>
            )
          }
          expanded={expandedCard === "distraction"}
          onClick={() => setExpandedCard(c => c === "distraction" ? null : "distraction")}
          expandedContent={(
            <>
              <p style={{ fontSize: 11, color: C.textSub, fontWeight: 700, margin: "0 0 8px", textTransform: "uppercase", letterSpacing: ".5px" }}>
                {T.focusContext || "Focus context"}
              </p>
              <div style={{ display: "flex", gap: 6, marginBottom: 8 }}>
                {["Low", "Medium", "High"].map(level => {
                  const active = level === distraction;
                  const col = level === "Low" ? C.green : level === "Medium" ? C.amber : C.red;
                  return (
                    <div key={level} style={{
                      flex: 1,
                      padding: "5px 8px",
                      borderRadius: 6,
                      background: active ? `${col}22` : C.borderLight,
                      border: `1px solid ${active ? col : C.border}`,
                      textAlign: "center",
                      fontSize: 10,
                      fontWeight: active ? 800 : 600,
                      color: active ? col : C.textLight,
                    }}>{level}</div>
                  );
                })}
              </div>
              <p style={{ fontSize: 10, color: C.textLight, lineHeight: 1.55, margin: 0 }}>
                {distraction === "Low"
                  ? (T.distractTipLow  || "You're in a strong focus zone. Keep momentum on deep work.")
                  : distraction === "Medium"
                  ? (T.distractTipMid  || "Moderate context switching. Try a 25-min focus sprint to reset.")
                  : (T.distractTipHigh || "Heavy context switching. Block notifications & batch similar tasks.")}
              </p>
              <p style={{ fontSize: 10, color: C.textSub, marginTop: 8, fontWeight: 700 }}>
                📷 {screenshots.toLocaleString()} {T.captures || "captures"}
                <span style={{ marginLeft: 10 }}>▶ {sessions} {T.sessionsCount || "sessions"}</span>
              </p>
            </>
          )}
        />
      </div>

      {/* ── Productivity Donut + Weekly Area Trend ── */}
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) minmax(0,1.6fr)", gap: 14 }}>

        {/* Donut */}
        <div style={{ ...card(), padding: 18 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
            <p style={{ fontSize: 14, fontWeight: 700, color: C.text, margin: 0 }}>
              {T.timeComposition || "Time composition"}
            </p>
            <span style={{ fontSize: 11, color: C.textLight }}>{fmtH(hoursWorked)} {T.dashboardTotalSuffix || "total"}</span>
          </div>
          {donutTotal > 0 ? (
            <div style={{ position: "relative", height: 240 }}>
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <defs>
                    {/* Gradient fills per segment so the donut feels rich, not flat. */}
                    <linearGradient id="ztDonutProd" x1="0" y1="0" x2="1" y2="1">
                      <stop offset="0%"   stopColor="#34D399" />
                      <stop offset="100%" stopColor="#0F9F5C" />
                    </linearGradient>
                    <linearGradient id="ztDonutIdle" x1="0" y1="0" x2="1" y2="1">
                      <stop offset="0%"   stopColor="#FBBF24" />
                      <stop offset="100%" stopColor="#D97706" />
                    </linearGradient>
                    <linearGradient id="ztDonutBreak" x1="0" y1="0" x2="1" y2="1">
                      <stop offset="0%"   stopColor="#60A5FA" />
                      <stop offset="100%" stopColor="#1D4ED8" />
                    </linearGradient>
                    {/* Goal-ring gradient (sky→teal) for the outer progress arc. */}
                    <linearGradient id="ztGoalRing" x1="0" y1="0" x2="1" y2="0">
                      <stop offset="0%"   stopColor="#06B6D4" />
                      <stop offset="100%" stopColor={COL_PROD} />
                    </linearGradient>
                  </defs>

                  {/* Outer goal ring: full track + progress arc.
                      Tracks hoursWorked vs DAILY_TARGET_HOURS. */}
                  <Pie
                    data={[{ v: DAILY_TARGET_HOURS }]}
                    dataKey="v"
                    cx="50%" cy="50%"
                    innerRadius={102} outerRadius={112}
                    fill={C.borderLight}
                    stroke="none"
                    isAnimationActive={false}
                  />
                  <Pie
                    data={[
                      { v: Math.min(hoursWorked, DAILY_TARGET_HOURS) },
                      { v: Math.max(0, DAILY_TARGET_HOURS - hoursWorked) },
                    ]}
                    dataKey="v"
                    cx="50%" cy="50%"
                    innerRadius={102} outerRadius={112}
                    startAngle={90} endAngle={-270}
                    stroke="none"
                    isAnimationActive={true}
                    animationDuration={700}
                  >
                    <Cell fill="url(#ztGoalRing)" />
                    <Cell fill="transparent" />
                  </Pie>

                  {/* Inner composition donut. */}
                  <Pie
                    data={donutData}
                    dataKey="value"
                    cx="50%" cy="50%"
                    innerRadius={60} outerRadius={92}
                    paddingAngle={3}
                    stroke="none"
                    activeIndex={donutHover}
                    activeShape={(props) => (
                      <g>
                        <Sector
                          {...props}
                          outerRadius={(props.outerRadius || 92) + 6}
                          innerRadius={(props.innerRadius || 60) - 2}
                        />
                        <Sector
                          cx={props.cx}
                          cy={props.cy}
                          startAngle={props.startAngle}
                          endAngle={props.endAngle}
                          innerRadius={(props.outerRadius || 92) + 8}
                          outerRadius={(props.outerRadius || 92) + 10}
                          fill={props.fill}
                          opacity={0.55}
                        />
                      </g>
                    )}
                    onMouseEnter={(_, i) => setDonutHover(i)}
                    onMouseLeave={() => setDonutHover(-1)}
                    isAnimationActive={true}
                    animationDuration={520}
                  >
                    {donutData.map((d, i) => <Cell key={i} fill={`url(#${d.gid})`} cursor="pointer" />)}
                  </Pie>

                  <Tooltip
                    contentStyle={tooltipStyle}
                    formatter={(v, name) => name ? [`${Number(v).toFixed(2)}h`, name] : null}
                  />
                </PieChart>
              </ResponsiveContainer>
              <div style={{ position: "absolute", inset: 0, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", pointerEvents: "none" }}>
                {donutHover >= 0 && donutData[donutHover] ? (
                  <>
                    <p style={{ fontSize: 24, fontWeight: 800, color: donutData[donutHover].color, margin: 0, lineHeight: 1, letterSpacing: "-.5px", transition: "color .2s" }}>
                      {donutData[donutHover].value.toFixed(1)}h
                    </p>
                    <p style={{ fontSize: 11, color: C.textSub, margin: "2px 0 0", textTransform: "uppercase", letterSpacing: ".6px", fontWeight: 600 }}>
                      {donutData[donutHover].name}
                    </p>
                    <p style={{ fontSize: 10, color: C.textLight, margin: "2px 0 0", fontWeight: 600 }}>
                      {donutTotal > 0 ? `${((donutData[donutHover].value / donutTotal) * 100).toFixed(0)}% of work time` : "—"}
                    </p>
                  </>
                ) : (
                  <>
                    <p style={{ fontSize: 28, fontWeight: 800, color: C.text, margin: 0, lineHeight: 1, letterSpacing: "-.5px" }}>{prodPct}%</p>
                    <p style={{ fontSize: 11, color: C.textSub, margin: "2px 0 0", textTransform: "uppercase", letterSpacing: ".6px", fontWeight: 600 }}>
                      {T.productive || "Productive"}
                    </p>
                    <p style={{ fontSize: 10, color: C.textLight, margin: "4px 0 0", fontWeight: 600 }}>
                      {fmtH(hoursWorked)} / {fmtH(DAILY_TARGET_HOURS)} {T.goal || "goal"}
                    </p>
                  </>
                )}
              </div>
            </div>
          ) : (
            <ChartEmpty C={C} icon="clock" message={T.noTimeYet || "No time recorded yet today."} height={240} />
          )}

          {/* Always-visible composition legend (no zero-filter — users see
              the full breakdown even when a category is 0h). */}
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 12, justifyContent: "center" }}>
            {donutData.map((d, i) => {
              const pct = donutTotal > 0 ? Math.round((d.value / donutTotal) * 100) : 0;
              return (
                <button
                  key={d.name}
                  type="button"
                  onMouseEnter={() => setDonutHover(i)}
                  onMouseLeave={() => setDonutHover(-1)}
                  style={{
                    display: "inline-flex", alignItems: "center", gap: 6, fontSize: 11, fontWeight: 600,
                    color: donutHover === i ? d.color : C.textSub,
                    padding: "5px 10px", borderRadius: 8,
                    border: `1px solid ${donutHover === i ? d.color : C.border}`,
                    background: donutHover === i ? `${d.color}15` : C.bg,
                    cursor: "pointer", fontFamily: "inherit", transition: "all .15s",
                  }}>
                  <span style={{ width: 9, height: 9, borderRadius: 3, background: d.color }} />
                  {d.name}
                  <span style={{ color: C.textLight, fontWeight: 500 }}>{d.value.toFixed(1)}h · {pct}%</span>
                </button>
              );
            })}
          </div>

          {/* Goal-progress strip — always shown, tracks hoursWorked vs target. */}
          <div style={{
            marginTop: 12, padding: "10px 12px", borderRadius: 10,
            background: `linear-gradient(90deg, ${COL_PROD}10, transparent)`,
            border: `1px solid ${C.border}`,
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6, fontSize: 11 }}>
              <span style={{ color: C.textSub, fontWeight: 700, textTransform: "uppercase", letterSpacing: ".5px" }}>
                {T.dailyGoal || "Daily goal"}
              </span>
              <span style={{ color: C.text, fontWeight: 700 }}>
                {fmtH(hoursWorked)} / {fmtH(DAILY_TARGET_HOURS)}
                <span style={{ color: hoursRemaining > 0 ? C.amber : C.green, fontWeight: 700, marginLeft: 8 }}>
                  · {hoursRemaining > 0 ? `${fmtH(hoursRemaining)} ${T.remaining || "remaining"}` : (T.goalReached || "Goal reached")}
                </span>
              </span>
            </div>
            <div style={{ height: 6, borderRadius: 4, background: C.borderLight, overflow: "hidden" }}>
              <div style={{
                height: "100%",
                width: `${goalPct}%`,
                background: `linear-gradient(90deg, #06B6D4, ${COL_PROD})`,
                transition: "width .5s",
                boxShadow: `0 0 8px ${COL_PROD}66`,
              }} />
            </div>
          </div>
        </div>

        {/* Weekly Area */}
        <div style={{ ...card(), padding: 18 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8, flexWrap: "wrap", gap: 8 }}>
            <p style={{ fontSize: 14, fontWeight: 700, color: C.text, margin: 0 }}>
              {T.weeklyTrend || "Weekly hours trend"}
            </p>
            <div style={{ display: "flex", gap: 14, fontSize: 11, color: C.textSub }}>
              <span><span style={{ color: C.textLight }}>{T.total || "Total"}: </span><strong style={{ color: C.text }}>{fmtH(weeklyTotal)}</strong></span>
              <span><span style={{ color: C.textLight }}>{T.avg || "Avg"}: </span><strong style={{ color: C.text }}>{fmtH(weeklyAvg)}</strong></span>
              {weeklyBest && (
                <span><span style={{ color: C.textLight }}>{T.best || "Best"}: </span><strong style={{ color: C.green }}>{weeklyBest.day} · {fmtH(weeklyBest.hours)}</strong></span>
              )}
            </div>
          </div>

          {/* Clickable legend — toggles each series on/off in the chart. */}
          <div style={{ display: "flex", gap: 8, marginBottom: 6, flexWrap: "wrap" }}>
            {[
              { key: "hours",        label: T.hours      || "Hours",        color: C.brand },
              { key: "productivity", label: T.productive || "Productivity", color: C.green },
            ].map(s => {
              const on = seriesOn[s.key];
              return (
                <button
                  key={s.key}
                  type="button"
                  onClick={() => setSeriesOn(v => ({ ...v, [s.key]: !v[s.key] }))}
                  aria-pressed={on}
                  style={{
                    display: "inline-flex", alignItems: "center", gap: 6,
                    fontSize: 11, fontWeight: 700,
                    padding: "4px 10px", borderRadius: 99,
                    background: on ? `${s.color}15` : "transparent",
                    color: on ? s.color : C.textLight,
                    border: `1px solid ${on ? s.color : C.border}`,
                    cursor: "pointer", fontFamily: "inherit",
                    textDecoration: on ? "none" : "line-through",
                    transition: "all .15s",
                  }}>
                  <span style={{ width: 8, height: 8, borderRadius: 2, background: s.color, opacity: on ? 1 : 0.4 }} />
                  {s.label}
                </button>
              );
            })}
            <span style={{ marginLeft: "auto", fontSize: 10, color: C.textLight, alignSelf: "center" }}>
              {T.clickDayToJump || "Click a day to view it"}
            </span>
          </div>

          {weeklyData.length > 0 && weeklyTotal > 0 ? (
            <div style={{ height: 260 }}>
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart
                  data={weeklyData}
                  margin={{ top: 12, right: 16, left: -8, bottom: 0 }}
                  onClick={(e) => {
                    const row = e?.activePayload?.[0]?.payload;
                    if (row?.date) onDateChange(row.date);
                  }}
                  style={{ cursor: "pointer" }}
                >
                  <defs>
                    {/* Vibrant gradient fills — brand-blue for hours,
                        green for productivity %. */}
                    <linearGradient id="ztHoursGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%"   stopColor="#3B82F6" stopOpacity={0.65} />
                      <stop offset="100%" stopColor="#3B82F6" stopOpacity={0.0}  />
                    </linearGradient>
                    <linearGradient id="ztProdGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%"   stopColor="#22C55E" stopOpacity={0.45} />
                      <stop offset="100%" stopColor="#22C55E" stopOpacity={0.0}  />
                    </linearGradient>
                  </defs>
                  <CartesianGrid stroke={C.borderLight} vertical={false} strokeDasharray="3 3" />
                  <XAxis
                    dataKey="day"
                    stroke={C.textLight}
                    fontSize={11}
                    tickLine={false}
                    axisLine={false}
                    tick={({ x, y, payload, index }) => {
                      const row = weeklyData[index];
                      const isActive = row && (row.date === date || (!date && index === weeklyData.length - 1));
                      return (
                        <g transform={`translate(${x},${y + 6})`}>
                          <text
                            textAnchor="middle"
                            fontSize={11}
                            fontWeight={isActive ? 800 : 500}
                            fill={isActive ? "#3B82F6" : C.textLight}
                          >{payload.value}</text>
                          {isActive && (
                            <circle cx={0} cy={10} r={2.5} fill="#3B82F6" />
                          )}
                        </g>
                      );
                    }}
                  />
                  {/* Left axis: hours (0–10 typical). Right axis: productivity %. */}
                  <YAxis
                    yAxisId="hours"
                    stroke={C.textLight} fontSize={11}
                    tickLine={false} axisLine={false}
                    tickFormatter={(v) => `${v}h`}
                    width={36}
                  />
                  <YAxis
                    yAxisId="prod" orientation="right"
                    stroke={C.textLight} fontSize={11}
                    tickLine={false} axisLine={false}
                    tickFormatter={(v) => `${v}%`}
                    domain={[0, 100]}
                    width={36}
                  />
                  <Tooltip
                    contentStyle={tooltipStyle}
                    labelStyle={{ color: C.textSub, fontWeight: 700 }}
                    formatter={(v, name) => name === "hours"
                      ? [`${Number(v).toFixed(1)}h`, T.hours || "Hours"]
                      : [`${Number(v).toFixed(0)}%`, T.productive || "Productive"]
                    }
                    cursor={{ stroke: "#3B82F6", strokeWidth: 1, strokeDasharray: "3 3" }}
                  />

                  {/* Reference lines — daily target (8h) + weekly avg.
                      Always rendered so workers see context even with sparse data. */}
                  <ReferenceLine
                    yAxisId="hours"
                    y={DAILY_TARGET_HOURS}
                    stroke="#06B6D4"
                    strokeDasharray="4 4"
                    strokeWidth={1.5}
                    label={{
                      value: `${T.target || "Target"} ${DAILY_TARGET_HOURS}h`,
                      position: "insideTopRight",
                      fill: "#06B6D4",
                      fontSize: 10,
                      fontWeight: 700,
                    }}
                  />
                  {weeklyAvg > 0 && (
                    <ReferenceLine
                      yAxisId="hours"
                      y={Number(weeklyAvg.toFixed(2))}
                      stroke={C.textLight}
                      strokeDasharray="2 4"
                      strokeWidth={1}
                      label={{
                        value: `${T.avg || "Avg"} ${weeklyAvg.toFixed(1)}h`,
                        position: "insideBottomRight",
                        fill: C.textSub,
                        fontSize: 10,
                        fontWeight: 700,
                      }}
                    />
                  )}

                  {seriesOn.hours && (
                    <Area
                      yAxisId="hours"
                      type="monotone" dataKey="hours"
                      stroke="#3B82F6" strokeWidth={2.5}
                      fill="url(#ztHoursGrad)"
                      dot={{ r: 3, fill: "#3B82F6", stroke: C.surface, strokeWidth: 1 }}
                      activeDot={{ r: 6, stroke: C.surface, strokeWidth: 2, fill: "#3B82F6", cursor: "pointer" }}
                      isAnimationActive={true} animationDuration={520}
                    />
                  )}
                  {seriesOn.productivity && (
                    <Area
                      yAxisId="prod"
                      type="monotone" dataKey="productivity"
                      stroke="#22C55E" strokeWidth={2}
                      fill="url(#ztProdGrad)"
                      dot={{ r: 2, fill: "#22C55E", stroke: C.surface, strokeWidth: 1 }}
                      activeDot={{ r: 5, stroke: C.surface, strokeWidth: 2, fill: "#22C55E", cursor: "pointer" }}
                      isAnimationActive={true} animationDuration={520}
                    />
                  )}
                </AreaChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <ChartEmpty C={C} icon="trending_up" message={T.noWeeklyYet || "Weekly trend will appear once you log activity."} height={260} />
          )}
        </div>
      </div>

      {/* ── Top Apps Bar + Engagement detail ── */}
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1.4fr) minmax(0,1fr)", gap: 14 }}>

        {/* Top Apps */}
        <div style={{ ...card(), padding: 18 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
            <p style={{ fontSize: 14, fontWeight: 700, color: C.text, margin: 0 }}>
              {T.topApps || "Top apps & websites"}
            </p>
            {pinnedApp && (
              <button
                type="button"
                onClick={() => setPinnedApp(null)}
                style={{ fontSize: 11, color: C.textSub, background: "transparent", border: "none", padding: "2px 6px", cursor: "pointer", fontFamily: "inherit", fontWeight: 600 }}>
                {T.clearSelection || "Clear"}
              </button>
            )}
          </div>
          {topApps.length > 0 ? (
            <>
              <div style={{ height: Math.max(160, topApps.length * 40 + 24) }}>
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart
                    layout="vertical"
                    data={topApps}
                    margin={{ top: 4, right: 60, left: 4, bottom: 0 }}
                    barCategoryGap={12}
                    onClick={(e) => {
                      const row = e?.activePayload?.[0]?.payload;
                      if (!row) return;
                      setPinnedApp(prev => prev?.name === row.name ? null : row);
                    }}
                    style={{ cursor: "pointer" }}
                  >
                    <defs>
                      {/* Each bar gets its own horizontal gradient (light→dark
                          of its base colour) so multi-app days are scanable
                          at a glance. */}
                      {topApps.map((a) => (
                        <linearGradient key={a.gid} id={a.gid} x1="0" y1="0" x2="1" y2="0">
                          <stop offset="0%"   stopColor={a.color} stopOpacity={0.55} />
                          <stop offset="100%" stopColor={a.color} stopOpacity={1.0}  />
                        </linearGradient>
                      ))}
                    </defs>
                    <CartesianGrid stroke={C.borderLight} horizontal={false} strokeDasharray="3 3" />
                    <XAxis type="number" stroke={C.textLight} fontSize={11} tickLine={false} axisLine={false} />
                    <YAxis type="category" dataKey="name" stroke={C.text} fontSize={12} fontWeight={600} tickLine={false} axisLine={false} width={110} />
                    <Tooltip
                      contentStyle={tooltipStyle}
                      formatter={(v) => [`${v} ${T.captures || "captures"}`, ""]}
                      cursor={{ fill: isDark ? "rgba(255,255,255,0.04)" : "rgba(0,0,0,0.04)" }}
                    />
                    <Bar
                      dataKey="count"
                      radius={[0, 8, 8, 0]}
                      isAnimationActive={true}
                      animationDuration={520}
                      label={({ x, y, width, height, value }) => {
                        const total = topApps.reduce((s, b) => s + b.count, 0) || 1;
                        const pct = Math.round((value / total) * 100);
                        return (
                          <text
                            x={x + width + 8}
                            y={y + height / 2 + 4}
                            fill={C.textSub}
                            fontSize={11}
                            fontWeight={700}
                          >
                            {pct}%
                          </text>
                        );
                      }}
                    >
                      {topApps.map((a, i) => (
                        <Cell
                          key={i}
                          fill={`url(#${a.gid})`}
                          opacity={pinnedApp && pinnedApp.name !== a.name ? 0.35 : 1}
                          stroke={pinnedApp?.name === a.name ? a.color : "none"}
                          strokeWidth={pinnedApp?.name === a.name ? 2 : 0}
                          cursor="pointer"
                        />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>

              {/* Pinned app detail strip — appears when a user clicks a bar. */}
              {pinnedApp && (() => {
                const totalCaps = topApps.reduce((a, b) => a + b.count, 0) || 1;
                const sharePct = ((pinnedApp.count / totalCaps) * 100).toFixed(0);
                return (
                  <div style={{
                    marginTop: 10, padding: "10px 12px", borderRadius: 10,
                    border: `1px solid ${pinnedApp.color}55`,
                    background: `${pinnedApp.color}10`,
                    display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap",
                  }}>
                    <span style={{ width: 10, height: 10, borderRadius: 3, background: pinnedApp.color, flexShrink: 0 }} />
                    <div style={{ flex: 1, minWidth: 140 }}>
                      <p style={{ fontSize: 13, fontWeight: 800, color: C.text, margin: 0 }}>{pinnedApp.name}</p>
                      <p style={{ fontSize: 11, color: C.textSub, margin: "2px 0 0" }}>
                        {pinnedApp.count} {T.captures || "captures"} · {sharePct}% {T.ofTotal || "of total"}
                      </p>
                    </div>
                    <div style={{ height: 6, background: C.borderLight, borderRadius: 3, flex: 1, minWidth: 120, overflow: "hidden" }}>
                      <div style={{ height: "100%", width: `${sharePct}%`, background: pinnedApp.color, transition: "width .4s" }} />
                    </div>
                  </div>
                );
              })()}
            </>
          ) : (
            <ChartEmpty C={C} icon="monitor" message={T.aiNoApps || "No app data recorded."} height={160} />
          )}
        </div>

        {/* Engagement detail */}
        <div style={{ ...card(), padding: 18 }}>
          <p style={{ fontSize: 14, fontWeight: 700, color: C.text, margin: "0 0 12px" }}>
            {T.engagementDetail || "Engagement detail"}
          </p>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
            <MiniStat C={C} label={T.mouseClicks   || "Mouse clicks"}   value={mouseClicks.toLocaleString()} icon="activity" color={C.brand} />
            <MiniStat C={C} label={T.keyPresses    || "Key presses"}    value={keyPresses.toLocaleString()}  icon="hash"     color={C.green} />
            <MiniStat C={C} label={T.screenshots   || "Screenshots"}    value={screenshots.toLocaleString()} icon="camera"   color="#06B6D4" />
            <MiniStat C={C} label={T.sessionsCount || "Sessions"}       value={sessions.toLocaleString()}    icon="play"     color="#A855F7" />
          </div>
          <div style={{ marginTop: 14, padding: "10px 12px", borderRadius: 10, background: isDark ? "rgba(255,255,255,0.03)" : "#F8FAFC", border: `1px solid ${C.borderLight}` }}>
            <p style={{ fontSize: 11, fontWeight: 700, color: C.textSub, margin: "0 0 6px", textTransform: "uppercase", letterSpacing: ".5px" }}>
              {T.activityBreakdown || "Activity breakdown"}
            </p>
            <BreakdownRow C={C} label={T.productiveHours || "Productive hours"} value={fmtH(productiveHrs)} color={C.green} />
            <BreakdownRow C={C} label={T.idleHoursLabel || "Idle hours"}        value={fmtH(idleHrs)}       color={C.amber} />
            <BreakdownRow C={C} label={T.breakMinutes || "Break time"}          value={`${breakMins}m`}     color={C.brand} />
          </div>
        </div>
      </div>
    </div>
  );
}

const StatCard = React.memo(function StatCard({
  C, card, label, value, icon, tone, delta, progress, footer,
  expanded, onClick, expandedContent,
}) {
  const [hovered, setHovered] = useState(false);
  const interactive = !!onClick;

  return (
    <div
      role={interactive ? "button" : undefined}
      tabIndex={interactive ? 0 : undefined}
      aria-expanded={interactive ? !!expanded : undefined}
      onClick={onClick}
      onKeyDown={interactive ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onClick(e); } } : undefined}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        ...card(),
        padding: "16px 18px",
        borderLeft: `3px solid ${tone.color}`,
        borderColor: hovered || expanded ? tone.color : undefined,
        position: "relative",
        overflow: "hidden",
        background: `linear-gradient(135deg, ${tone.bg}${hovered || expanded ? "88" : "55"} 0%, transparent 60%), ${C.surface}`,
        cursor: interactive ? "pointer" : "default",
        transform: hovered && !expanded ? "translateY(-2px)" : "none",
        boxShadow: expanded
          ? `0 12px 32px -8px ${tone.color}55, 0 0 0 1px ${tone.color}66`
          : hovered
            ? `0 10px 24px -10px ${tone.color}66`
            : (card().boxShadow),
        transition: "transform .18s ease, box-shadow .25s ease, background .25s ease, border-color .2s ease",
      }}
    >
      {/* subtle animated sheen on hover */}
      {(hovered || expanded) && (
        <div style={{
          position: "absolute", inset: 0, pointerEvents: "none",
          background: `radial-gradient(circle at ${expanded ? "50% 0%" : "100% 0%"}, ${tone.color}1f, transparent 60%)`,
          transition: "background .3s",
        }} />
      )}

      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10, position: "relative" }}>
        <div style={{
          width: 32, height: 32, borderRadius: 8,
          background: tone.bg, border: `1px solid ${tone.bdr}`,
          display: "flex", alignItems: "center", justifyContent: "center",
          transition: "transform .25s ease",
          transform: hovered ? "scale(1.08) rotate(-3deg)" : "none",
        }}>
          <Ico n={icon} s={15} c={tone.color} />
        </div>
        <span style={{ fontSize: 12, color: C.textSub, fontWeight: 600 }}>{label}</span>
        {interactive && (
          <span style={{
            marginLeft: "auto", fontSize: 10, fontWeight: 700,
            color: expanded ? tone.color : C.textLight,
            transform: expanded ? "rotate(180deg)" : "rotate(0deg)",
            transition: "transform .25s ease, color .2s",
            display: "inline-block",
          }}>
            ▾
          </span>
        )}
      </div>

      <p style={{
        fontSize: 26, fontWeight: 800, color: C.text,
        fontVariantNumeric: "tabular-nums", margin: 0,
        letterSpacing: "-.5px", position: "relative",
        transition: "transform .25s ease",
        transform: hovered ? "translateX(2px)" : "none",
      }}>
        {value}
      </p>

      {delta && (
        <p style={{ fontSize: 11, color: delta.value >= 0 ? C.green : C.amber, fontWeight: 700, margin: "4px 0 0", position: "relative" }}>
          {delta.format(delta.value)} <span style={{ color: C.textLight, fontWeight: 500 }}>{delta.label}</span>
        </p>
      )}

      {progress != null && (
        <div style={{ marginTop: 10, height: 5, borderRadius: 4, background: C.borderLight, overflow: "hidden", position: "relative" }}>
          <div style={{
            height: "100%",
            width: `${Math.min(100, Math.max(0, progress))}%`,
            background: `linear-gradient(90deg, ${tone.color}, ${tone.color}cc)`,
            transition: "width .4s",
            boxShadow: `0 0 8px ${tone.color}44`,
          }} />
          {/* sheen sweep on hover */}
          {hovered && (
            <div style={{
              position: "absolute", top: 0, left: 0, height: "100%", width: "30%",
              background: "linear-gradient(90deg, transparent, rgba(255,255,255,0.45), transparent)",
              animation: "ztSheen 1.2s ease-out",
              pointerEvents: "none",
            }} />
          )}
        </div>
      )}

      <div style={{ position: "relative" }}>{footer}</div>

      {/* Expanded detail panel — slides open below the card. */}
      <div
        style={{
          maxHeight: expanded ? 240 : 0,
          opacity: expanded ? 1 : 0,
          overflow: "hidden",
          transition: "max-height .35s ease, opacity .25s ease, margin-top .25s ease",
          marginTop: expanded ? 12 : 0,
          position: "relative",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div style={{
          paddingTop: 10,
          borderTop: `1px dashed ${C.borderLight}`,
        }}>
          {expandedContent}
        </div>
      </div>

      <style>{`@keyframes ztSheen{from{transform:translateX(-100%)}to{transform:translateX(400%)}}`}</style>
    </div>
  );
});

const MiniStat = React.memo(function MiniStat({ C, label, value, icon, color }) {
  return (
    <div style={{ padding: "10px 12px", borderRadius: 10, background: C.bg, border: `1px solid ${C.border}`, display: "flex", flexDirection: "column", gap: 4 }}>
      <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 11, color: C.textSub, fontWeight: 600 }}>
        <Ico n={icon} s={12} c={color} />
        {label}
      </span>
      <span style={{ fontSize: 17, fontWeight: 800, color: C.text, fontVariantNumeric: "tabular-nums", letterSpacing: "-.3px" }}>
        {value}
      </span>
    </div>
  );
});

const BreakdownRow = React.memo(function BreakdownRow({ C, label, value, color }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "4px 0", fontSize: 12 }}>
      <span style={{ color: C.textSub }}>
        <span style={{ display: "inline-block", width: 6, height: 6, borderRadius: 3, background: color, marginRight: 6, verticalAlign: "middle" }} />
        {label}
      </span>
      <span style={{ color: C.text, fontWeight: 700 }}>{value}</span>
    </div>
  );
});

const ChartEmpty = React.memo(function ChartEmpty({ C, icon, message, height }) {
  return (
    <div style={{ height, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 8, color: C.textLight }}>
      <Ico n={icon} s={28} c={C.textLight} />
      <p style={{ fontSize: 12, margin: 0 }}>{message}</p>
    </div>
  );
});

export default DailySummaryView;

import React from "react";
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, PieChart, Pie, Cell, Legend, AreaChart, Area, LineChart, Line, Sector, LabelList, Label } from "recharts";
// Extracted from AdminDashboard.jsx ("appshistory" tab) for lazy-loading.
// Shared theme/helpers/constants/state passed as props.
export default function AppsHistoryTab({
  C, T, isDark, Icon, card, APP_COLORS, employeesAlpha, fmtAppDur,
  fetchAppsHistory, appsAnalytics, appsHistoryLoading,
  appsHistoryDate, setAppsHistoryDate, appsHistoryUserId, setAppsHistoryUserId,
  appExpandedUser, setAppExpandedUser,
}) {
              const kpi = appsAnalytics?.kpi || {};
              const usersData = appsAnalytics?.users || [];
              const appsData  = appsAnalytics?.apps  || [];
              const thS = { padding: "10px 14px", textAlign: "left", color: C.textSub, fontWeight: 700, fontSize: 11, textTransform: "uppercase", letterSpacing: "0.5px", borderBottom: `1px solid ${C.borderLight}` };
              const tdS = { padding: "12px 14px", fontVariantNumeric: "tabular-nums" };
              const maxAppSec = appsData[0]?.total_seconds || 1;

              return (
              <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>

                {/* ── Filters ── */}
                <div style={{ ...card(), padding: "16px 20px", display: "flex", flexWrap: "wrap", gap: 12, alignItems: "flex-end" }}>
                  <div>
                    <p style={{ fontSize: 11, fontWeight: 700, color: C.textSub, marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.5px" }}>{T.dateFilter || "Date"}</p>
                    <input type="date" value={appsHistoryDate} onChange={e => setAppsHistoryDate(e.target.value)}
                      style={{ padding: "8px 12px", borderRadius: 8, border: `1px solid ${C.border}`, background: C.surface, color: C.text, fontSize: 13, fontFamily: "inherit" }} />
                  </div>
                  <div>
                    <p style={{ fontSize: 11, fontWeight: 700, color: C.textSub, marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.5px" }}>{T.employee || "Employee"}</p>
                    <select value={appsHistoryUserId} onChange={e => setAppsHistoryUserId(e.target.value)}
                      style={{ padding: "8px 12px", borderRadius: 8, border: `1px solid ${C.border}`, background: C.surface, color: C.text, fontSize: 13, minWidth: 200, fontFamily: "inherit" }}>
                      <option value="">{T.allEmployeesLabel || "All employees"}</option>
                      {employeesAlpha.map(e => <option key={e.id} value={e.id}>{e.name}</option>)}
                    </select>
                  </div>
                  <button onClick={fetchAppsHistory}
                    style={{ padding: "8px 20px", borderRadius: 8, border: "none", background: C.brand, color: "#fff", fontWeight: 700, fontSize: 12, cursor: "pointer", fontFamily: "inherit" }}>
                    {T.refreshBtn || "Refresh"}
                  </button>
                </div>

                {appsHistoryLoading ? (
                  <div style={{ ...card(), padding: 60, textAlign: "center", color: C.textSub }}>{T.loadingAppsAnalytics || "Loading analytics..."}</div>
                ) : !appsAnalytics || (!usersData.length && !appsData.length) ? (
                  <div style={{ ...card(), padding: 60, textAlign: "center", color: C.textSub, fontSize: 13 }}>{T.noAppsActivityRecorded || "No app activity recorded for this selection."}</div>
                ) : (
                <>
                  {/* ── KPI Cards (gradient halos, animated, hover lift) ── */}
                  <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: 12 }}>
                    {[
                      { label: T.activeTimeCol || "Active Time",    value: fmtAppDur(kpi.total_active_seconds),      from: "#22D3A6", to: "#0F9D6E", icon: "clock"    },
                      { label: T.idleTimeCol   || "Idle Time",      value: fmtAppDur(kpi.total_idle_seconds),        from: "#FCD34D", to: "#D97706", icon: "pause"    },
                      { label: T.productivity  || "Productivity",   value: `${kpi.productivity_pct || 0}%`,          from: "#38BDF8", to: "#0284C7", icon: "activity" },
                      { label: T.topApp        || "Top App",        value: kpi.top_app || "—",                       from: "#C4B5FD", to: "#7C3AED", icon: "monitor"  },
                      { label: T.mouseClicksCol|| "Mouse Clicks",   value: (kpi.total_clicks || 0).toLocaleString(), from: "#F472B6", to: "#DB2777", icon: "mouse"    },
                      { label: T.keyPressesCol || "Key Presses",    value: (kpi.total_keys   || 0).toLocaleString(), from: "#67E8F9", to: "#0891B2", icon: "type"     },
                    ].map(k => (
                      <div
                        key={k.label}
                        className="ah-kpi"
                        style={{
                          background: `linear-gradient(135deg, ${k.from}14 0%, ${k.to}08 100%), ${C.surface}`,
                          border: `1px solid ${C.border}`,
                          borderRadius: 14, padding: "16px 18px 14px",
                          position: "relative", overflow: "hidden",
                          boxShadow: "0 1px 2px rgba(15,23,42,0.04)",
                          transition: "transform .15s ease, box-shadow .2s ease, border-color .2s ease",
                          cursor: "default",
                        }}
                        onMouseEnter={e => {
                          e.currentTarget.style.transform = "translateY(-2px)";
                          e.currentTarget.style.boxShadow = `0 10px 28px -14px ${k.to}88`;
                          e.currentTarget.style.borderColor = `${k.to}66`;
                        }}
                        onMouseLeave={e => {
                          e.currentTarget.style.transform = "translateY(0)";
                          e.currentTarget.style.boxShadow = "0 1px 2px rgba(15,23,42,0.04)";
                          e.currentTarget.style.borderColor = C.border;
                        }}
                      >
                        {/* Top accent stripe */}
                        <span style={{
                          position: "absolute", top: 0, left: 0, right: 0, height: 3,
                          background: `linear-gradient(90deg, ${k.from}, ${k.to})`,
                        }} />
                        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
                          <p style={{ fontSize: 11, fontWeight: 700, color: k.to, textTransform: "uppercase", letterSpacing: "0.6px" }}>{k.label}</p>
                          <div style={{
                            width: 32, height: 32, borderRadius: 9,
                            background: `${k.from}22`,
                            display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0,
                          }}>
                            <Icon n={k.icon} s={16} c={k.to} />
                          </div>
                        </div>
                        <p style={{
                          fontSize: 22, fontWeight: 800, lineHeight: 1.1,
                          fontVariantNumeric: "tabular-nums",
                          background: `linear-gradient(135deg, ${k.from}, ${k.to})`,
                          WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent",
                          backgroundClip: "text",
                          overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                        }}>{k.value}</p>
                      </div>
                    ))}
                  </div>

                  {/* ── Charts Row: App Usage Bar + Productivity Donut ── */}
                  <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: 16 }}>
                    {/* App Usage Horizontal Bar — gradient bars + soft tooltip */}
                    <div style={{ ...card(), padding: 20 }}>
                      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 14 }}>
                        <p style={{ fontSize: 14, fontWeight: 700, color: C.text }}>{T.appUsage || "App Usage"}</p>
                        <span style={{ fontSize: 11, color: C.textLight, fontWeight: 600 }}>
                          {appsData.slice(0, 10).length} {T.appsShownLabel || "apps shown"}
                        </span>
                      </div>
                      <ResponsiveContainer width="100%" height={Math.max(220, appsData.slice(0, 10).length * 38)}>
                        <BarChart data={appsData.slice(0, 10).map(a => ({ ...a, minutes: +(a.total_seconds / 60).toFixed(1) }))} layout="vertical" margin={{ left: 10, right: 20, top: 4, bottom: 0 }}>
                          <defs>
                            {APP_COLORS.map((c, i) => (
                              <linearGradient key={i} id={`ah-bar-${i}`} x1="0" y1="0" x2="1" y2="0">
                                <stop offset="0%"  stopColor={c} stopOpacity={0.55} />
                                <stop offset="100%" stopColor={c} stopOpacity={1} />
                              </linearGradient>
                            ))}
                          </defs>
                          <CartesianGrid strokeDasharray="3 3" stroke={C.borderLight} horizontal={false} />
                          <XAxis type="number" tick={{ fontSize: 11, fill: C.textSub }} unit="m" axisLine={false} tickLine={false} />
                          <YAxis type="category" dataKey="app_name" width={110} tick={{ fontSize: 12, fill: C.text, fontWeight: 600 }} axisLine={false} tickLine={false} />
                          <Tooltip
                            cursor={{ fill: isDark ? "rgba(99,102,241,0.08)" : "rgba(99,102,241,0.06)" }}
                            contentStyle={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10, fontSize: 12, boxShadow: "0 6px 24px -8px rgba(15,23,42,0.25)" }}
                            labelStyle={{ color: C.text }}
                            itemStyle={{ color: C.text }}
                            formatter={(v) => [`${v} min`, T.usageLabel || "Usage"]}
                          />
                          <Bar dataKey="minutes" radius={[0, 8, 8, 0]} barSize={20} isAnimationActive>
                            {appsData.slice(0, 10).map((_, i) => <Cell key={i} fill={`url(#ah-bar-${i % APP_COLORS.length})`} />)}
                          </Bar>
                        </BarChart>
                      </ResponsiveContainer>
                    </div>

                    {/* Activity Split — radial gauge w/ gradient ring + centered % */}
                    <div style={{ ...card(), padding: 20, display: "flex", flexDirection: "column" }}>
                      <p style={{ fontSize: 14, fontWeight: 700, color: C.text, marginBottom: 8 }}>{T.activitySplit || "Activity Split"}</p>
                      <div style={{ position: "relative", width: "100%", height: 220 }}>
                        <ResponsiveContainer width="100%" height="100%">
                          <PieChart>
                            <defs>
                              <linearGradient id="ah-act" x1="0" y1="0" x2="0" y2="1">
                                <stop offset="0%"  stopColor="#22D3A6" stopOpacity={0.9} />
                                <stop offset="100%" stopColor="#0F9D6E" stopOpacity={0.95} />
                              </linearGradient>
                              <linearGradient id="ah-idle" x1="0" y1="0" x2="0" y2="1">
                                <stop offset="0%"  stopColor="#FCD34D" stopOpacity={0.9} />
                                <stop offset="100%" stopColor="#D97706" stopOpacity={0.95} />
                              </linearGradient>
                            </defs>
                            <Pie data={[
                              { name: T.active || "Active", value: kpi.total_active_seconds || 0, fill: "url(#ah-act)" },
                              { name: T.idle   || "Idle",   value: kpi.total_idle_seconds   || 1, fill: "url(#ah-idle)" },
                            ]} cx="50%" cy="50%" innerRadius={62} outerRadius={92} paddingAngle={3} cornerRadius={6} dataKey="value" stroke="none" isAnimationActive />
                            <Tooltip
                              contentStyle={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10, fontSize: 12, boxShadow: "0 6px 24px -8px rgba(15,23,42,0.25)" }}
                              labelStyle={{ color: C.text }}
                              itemStyle={{ color: C.text }}
                              formatter={(v, n) => [fmtAppDur(v), n]}
                            />
                          </PieChart>
                        </ResponsiveContainer>
                        {/* Center label */}
                        <div style={{
                          position: "absolute", top: 0, left: 0, right: 0, bottom: 0,
                          display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
                          pointerEvents: "none",
                        }}>
                          <span style={{
                            fontSize: 32, fontWeight: 900, lineHeight: 1,
                            background: "linear-gradient(135deg,#22D3A6,#0F9D6E)",
                            WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent", backgroundClip: "text",
                          }}>{kpi.productivity_pct || 0}%</span>
                          <span style={{ fontSize: 10, fontWeight: 700, color: C.textSub, letterSpacing: 0.5, textTransform: "uppercase", marginTop: 4 }}>
                            {T.productivityScore || "Productivity Score"}
                          </span>
                        </div>
                      </div>
                      {/* Inline legend */}
                      <div style={{ display: "flex", justifyContent: "center", gap: 14, marginTop: 8, flexWrap: "wrap" }}>
                        <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, color: C.textSub, fontWeight: 600 }}>
                          <span style={{ width: 10, height: 10, borderRadius: 3, background: "linear-gradient(135deg,#22D3A6,#0F9D6E)" }} />
                          {T.active || "Active"} <span style={{ color: C.textLight, fontWeight: 500 }}>· {fmtAppDur(kpi.total_active_seconds || 0)}</span>
                        </span>
                        <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, color: C.textSub, fontWeight: 600 }}>
                          <span style={{ width: 10, height: 10, borderRadius: 3, background: "linear-gradient(135deg,#FCD34D,#D97706)" }} />
                          {T.idle || "Idle"} <span style={{ color: C.textLight, fontWeight: 500 }}>· {fmtAppDur(kpi.total_idle_seconds || 0)}</span>
                        </span>
                      </div>
                    </div>
                  </div>

                  {/* ── Per-Employee Performance ── */}
                  {usersData.length > 0 && (
                    <div style={{ ...card(), padding: 20 }}>
                      <p style={{ fontSize: 14, fontWeight: 700, color: C.text, marginBottom: 16 }}>{T.employeePerformance || "Employee Performance"}</p>
                      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(330px, 1fr))", gap: 14 }}>
                        {usersData.map((u, ui) => {
                          const isExpanded = appExpandedUser === u.user_id;
                          const init = (u.name || "?").split(" ").map(w => w[0]).join("").toUpperCase().slice(0, 2);
                          // Productivity-tier gradient: green ≥70, amber ≥40, red below.
                          const tier = u.productivity >= 70
                            ? { from: "#22D3A6", to: "#0F9D6E" }
                            : u.productivity >= 40
                              ? { from: "#FCD34D", to: "#D97706" }
                              : { from: "#FB7185", to: "#E11D48" };
                          // Conic-gradient ring: paints the productive arc + a track.
                          const ringPct = Math.max(0, Math.min(100, u.productivity || 0));
                          return (
                            <div key={u.user_id} style={{
                              border: `1px solid ${isExpanded ? tier.to : C.borderLight}`,
                              borderRadius: 14, padding: 16,
                              background: `linear-gradient(135deg, ${tier.from}0A 0%, ${tier.to}04 100%), ${C.surface}`,
                              cursor: "pointer",
                              transition: "transform .15s ease, box-shadow .2s ease, border-color .2s ease",
                              boxShadow: isExpanded ? `0 12px 28px -16px ${tier.to}77` : "0 1px 2px rgba(15,23,42,0.04)",
                              position: "relative", overflow: "hidden",
                            }}
                            onClick={() => setAppExpandedUser(isExpanded ? null : u.user_id)}
                            onMouseEnter={e => {
                              if (isExpanded) return;
                              e.currentTarget.style.transform = "translateY(-2px)";
                              e.currentTarget.style.boxShadow = `0 10px 24px -16px ${tier.to}88`;
                            }}
                            onMouseLeave={e => {
                              if (isExpanded) return;
                              e.currentTarget.style.transform = "translateY(0)";
                              e.currentTarget.style.boxShadow = "0 1px 2px rgba(15,23,42,0.04)";
                            }}>
                              {/* Left accent stripe (tier colour) */}
                              <span style={{ position: "absolute", left: 0, top: 0, bottom: 0, width: 3, background: `linear-gradient(180deg, ${tier.from}, ${tier.to})` }} />

                              {/* Header */}
                              <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 12 }}>
                                <div style={{
                                  width: 44, height: 44, borderRadius: "50%",
                                  background: `linear-gradient(135deg, ${APP_COLORS[ui % APP_COLORS.length]}, ${APP_COLORS[(ui + 2) % APP_COLORS.length]})`,
                                  display: "flex", alignItems: "center", justifyContent: "center",
                                  color: "#fff", fontWeight: 700, fontSize: 15, flexShrink: 0,
                                  boxShadow: `0 4px 10px -4px ${APP_COLORS[ui % APP_COLORS.length]}aa`,
                                }}>{init}</div>
                                <div style={{ flex: 1, minWidth: 0 }}>
                                  <p style={{ fontWeight: 700, fontSize: 14, color: C.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{u.name}</p>
                                  <p style={{ fontSize: 11, color: C.textSub, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{T.topAppLabel || "Top app:"} <span style={{ fontWeight: 600, color: C.text }}>{u.top_app || "—"}</span></p>
                                </div>
                                {/* Productivity ring */}
                                <div style={{
                                  width: 54, height: 54, borderRadius: "50%", flexShrink: 0,
                                  background: `conic-gradient(${tier.to} ${ringPct * 3.6}deg, ${C.borderLight} 0deg)`,
                                  display: "flex", alignItems: "center", justifyContent: "center",
                                }}>
                                  <div style={{
                                    width: 42, height: 42, borderRadius: "50%", background: C.surface,
                                    display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
                                  }}>
                                    <span style={{
                                      fontSize: 13, fontWeight: 800, lineHeight: 1,
                                      background: `linear-gradient(135deg, ${tier.from}, ${tier.to})`,
                                      WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent", backgroundClip: "text",
                                    }}>{ringPct}%</span>
                                  </div>
                                </div>
                              </div>

                              {/* Mini KPIs */}
                              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr", gap: 8 }}>
                                {[
                                  { l: T.active || "Active", v: fmtAppDur(u.active_seconds),               c: "#0F9D6E" },
                                  { l: T.idle   || "Idle",   v: fmtAppDur(u.idle_seconds),                 c: "#D97706" },
                                  { l: T.clicksCol || "Clicks", v: (u.total_clicks || 0).toLocaleString(), c: "#DB2777" },
                                  { l: T.keysCol   || "Keys",   v: (u.total_keys   || 0).toLocaleString(), c: "#0891B2" },
                                ].map(m => (
                                  <div key={m.l} style={{ textAlign: "center", padding: "8px 4px", borderRadius: 8, background: `${m.c}10` }}>
                                    <p style={{ fontSize: 13, fontWeight: 800, color: m.c, fontVariantNumeric: "tabular-nums", lineHeight: 1.1 }}>{m.v}</p>
                                    <p style={{ fontSize: 9, color: C.textSub, marginTop: 3, textTransform: "uppercase", letterSpacing: 0.4, fontWeight: 700 }}>{m.l}</p>
                                  </div>
                                ))}
                              </div>

                              {/* Expanded: Per-app gradient bars */}
                              {isExpanded && u.apps && u.apps.length > 0 && (
                                <div style={{ marginTop: 14, borderTop: `1px dashed ${C.borderLight}`, paddingTop: 14 }}>
                                  <p style={{ fontSize: 11, fontWeight: 700, color: C.textSub, marginBottom: 10, textTransform: "uppercase", letterSpacing: 0.5 }}>{T.appBreakdown || "App Breakdown"}</p>
                                  {u.apps.map((a, ai) => {
                                    const barColor = APP_COLORS[ai % APP_COLORS.length];
                                    const widthPct = Math.max(2, (a.seconds / (u.apps[0]?.seconds || 1)) * 100);
                                    return (
                                      <div key={a.app} style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
                                        <p style={{ fontSize: 12, fontWeight: 600, color: C.text, width: 100, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flexShrink: 0 }}>{a.app}</p>
                                        <div style={{ flex: 1, height: 8, borderRadius: 4, background: C.borderLight, overflow: "hidden" }}>
                                          <div style={{
                                            height: "100%", borderRadius: 4,
                                            background: `linear-gradient(90deg, ${barColor}77, ${barColor})`,
                                            width: `${widthPct}%`,
                                            transition: "width .35s ease",
                                            boxShadow: `0 0 8px -2px ${barColor}66`,
                                          }} />
                                        </div>
                                        <p style={{ fontSize: 11, fontWeight: 700, color: C.text, minWidth: 50, textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{fmtAppDur(a.seconds)}</p>
                                      </div>
                                    );
                                  })}
                                </div>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  )}

                  {/* ── Detailed App Table ── */}
                  {appsData.length > 0 && (
                    <div style={{ ...card(), padding: 20 }}>
                      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16 }}>
                        <p style={{ fontSize: 14, fontWeight: 700, color: C.text }}>{T.allApplications || "All Applications"}</p>
                        <span style={{ fontSize: 11, color: C.textLight, fontWeight: 600 }}>
                          {appsData.length} {T.appsTotalLabel || "apps"}
                        </span>
                      </div>
                      <div style={{ overflowX: "auto" }}>
                        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                          <thead>
                            <tr style={{ background: C.bgAlt || C.surface }}>
                              <th style={{ ...thS, textAlign: "left" }}>{T.applicationCol || "Application"}</th>
                              <th style={{ ...thS, textAlign: "left", width: "30%" }}>{T.usageCol || "Usage"}</th>
                              <th style={{ ...thS, textAlign: "right" }}>{T.durationCol || "Duration"}</th>
                              <th style={{ ...thS, textAlign: "right" }}>{T.clicksCol || "Clicks"}</th>
                              <th style={{ ...thS, textAlign: "right" }}>{T.keysCol || "Keys"}</th>
                              <th style={{ ...thS, textAlign: "center" }}>{T.usersCol || "Users"}</th>
                              <th style={{ ...thS, textAlign: "center" }}>{T.activityCol || "Activity"}</th>
                            </tr>
                          </thead>
                          <tbody>
                            {appsData.map((row, idx) => {
                              const dotColor = APP_COLORS[idx % APP_COLORS.length];
                              // High-activity row pill = emerald, low = amber.
                              const isHigh = row.active_pct >= 50;
                              const pillFrom = isHigh ? "#22D3A6" : "#FCD34D";
                              const pillTo   = isHigh ? "#0F9D6E" : "#D97706";
                              return (
                                <tr
                                  key={row.app_name}
                                  style={{ borderBottom: `1px solid ${C.borderLight}`, transition: "background .15s ease" }}
                                  onMouseEnter={e => { e.currentTarget.style.background = `${dotColor}0C`; }}
                                  onMouseLeave={e => { e.currentTarget.style.background = "transparent"; }}
                                >
                                  <td style={{ ...tdS, color: C.text, fontWeight: 600 }}>
                                    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                                      <span style={{
                                        width: 10, height: 10, borderRadius: "50%",
                                        background: `linear-gradient(135deg, ${dotColor}, ${dotColor}99)`,
                                        boxShadow: `0 0 6px -1px ${dotColor}`,
                                        flexShrink: 0,
                                      }} />
                                      {row.app_name || (T.unknown || "Unknown")}
                                    </div>
                                  </td>
                                  <td style={{ ...tdS }}>
                                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                                      <div style={{ flex: 1, height: 8, borderRadius: 4, background: C.borderLight, overflow: "hidden" }}>
                                        <div style={{
                                          height: "100%", borderRadius: 4,
                                          background: `linear-gradient(90deg, ${dotColor}55, ${dotColor})`,
                                          width: `${Math.max(2, (row.total_seconds / maxAppSec) * 100)}%`,
                                          transition: "width .35s ease",
                                          boxShadow: `0 0 6px -2px ${dotColor}88`,
                                        }} />
                                      </div>
                                    </div>
                                  </td>
                                  <td style={{ ...tdS, textAlign: "right", color: C.text, fontWeight: 700 }}>{fmtAppDur(row.total_seconds)}</td>
                                  <td style={{ ...tdS, textAlign: "right", color: C.textSub, fontWeight: 600 }}>{(row.clicks || 0).toLocaleString()}</td>
                                  <td style={{ ...tdS, textAlign: "right", color: C.textSub, fontWeight: 600 }}>{(row.keys || 0).toLocaleString()}</td>
                                  <td style={{ ...tdS, textAlign: "center", color: C.textSub, fontWeight: 600 }}>{row.user_count || 0}</td>
                                  <td style={{ ...tdS, textAlign: "center" }}>
                                    <span style={{
                                      fontSize: 10, fontWeight: 800,
                                      padding: "4px 12px", borderRadius: 99,
                                      background: `linear-gradient(135deg, ${pillFrom}, ${pillTo})`,
                                      color: "#fff",
                                      boxShadow: `0 2px 6px -2px ${pillTo}99`,
                                      letterSpacing: 0.3,
                                    }}>{row.active_pct}{T.percentActive || "% active"}</span>
                                  </td>
                                </tr>
                              );
                            })}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  )}
                </>
                )}
              </div>
              );
}

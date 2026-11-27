// Phase: defer Recharts off the admin landing critical path. The 5 Overview
// charts are extracted here and lazy-loaded so vendor-charts (~321 kB) is no
// longer statically imported by AdminDashboard.jsx. Chart bodies are verbatim;
// data/theme/hover-state arrive as props.
import React from "react";
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, PieChart, Pie, Cell, Legend, AreaChart, Area, LineChart, Line, Sector, LabelList, Label } from "recharts";

// Wrap a label into up to `maxLines` lines of at most `maxChars` each, breaking
// on word boundaries. The final line is ellipsised if the text still overflows.
function wrapLabel(text, maxChars = 14, maxLines = 2) {
  const words = String(text || "").trim().split(/\s+/);
  const lines = [];
  let cur = "";
  for (const w of words) {
    const next = cur ? `${cur} ${w}` : w;
    if (next.length > maxChars && cur) {
      lines.push(cur);
      cur = w;
      if (lines.length === maxLines - 1) break;
    } else {
      cur = next;
    }
  }
  if (lines.length < maxLines) lines.push(cur);
  // Anything not yet consumed goes onto (and may truncate) the last line.
  const consumed = lines.join(" ").split(/\s+/).length;
  if (consumed < words.length) {
    let last = lines[lines.length - 1];
    if (last.length > maxChars - 1) last = last.slice(0, maxChars - 1);
    lines[lines.length - 1] = `${last}…`;
  }
  return lines.filter(Boolean);
}

function DailyHoursChartBase({ C, T, daily, dailyHoursHover, dailyHoursSeries, isDark, peakIdx, setDailyHoursHover }) {
  return (
                          <ResponsiveContainer width="100%" height={220}>
                            <BarChart
                              data={daily}
                              margin={{ top: 5, right: 5, left: -10, bottom: 0 }}
                              onMouseLeave={() => setDailyHoursHover(-1)}
                              onMouseMove={(e) => { if (e?.activeTooltipIndex != null) setDailyHoursHover(e.activeTooltipIndex); }}
                            >
                              <defs>
                                {/* Vibrant gradient fills — green→blue for worked, light-blue for break. */}
                                <linearGradient id="ztBarWorkedGrad" x1="0" y1="0" x2="0" y2="1">
                                  <stop offset="0%"   stopColor="#22C55E" stopOpacity={1.0} />
                                  <stop offset="100%" stopColor="#11496A" stopOpacity={0.9} />
                                </linearGradient>
                                <linearGradient id="ztBarBreakGrad" x1="0" y1="0" x2="0" y2="1">
                                  <stop offset="0%"   stopColor="#60A5FA" stopOpacity={0.95} />
                                  <stop offset="100%" stopColor="#1D4ED8" stopOpacity={0.75} />
                                </linearGradient>
                                <linearGradient id="ztBarWorkedPeak" x1="0" y1="0" x2="0" y2="1">
                                  <stop offset="0%"   stopColor="#FBBF24" stopOpacity={1.0} />
                                  <stop offset="100%" stopColor="#F97316" stopOpacity={0.95} />
                                </linearGradient>
                              </defs>
                              <CartesianGrid strokeDasharray="3 3" stroke={isDark ? "#2D3748" : "#F0F0F0"} vertical={false} />
                              <XAxis dataKey="date" tick={{ fontSize: 10, fill: C.textLight }} tickFormatter={v => { const d = new Date(v + "T00:00:00"); return d.toLocaleDateString("en-IN", { day: "numeric", month: "short" }); }} />
                              <YAxis tick={{ fontSize: 10, fill: C.textLight }} tickFormatter={v => `${v}h`} />
                              <Tooltip
                                cursor={{ fill: isDark ? "rgba(34,197,94,0.06)" : "rgba(34,197,94,0.08)" }}
                                contentStyle={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10, fontSize: 12, boxShadow: "0 8px 24px rgba(0,0,0,0.12)" }}
                                labelStyle={{ color: C.text, fontWeight: 600 }}
                                itemStyle={{ color: C.text }}
                                formatter={(v, name) => [`${Number(v).toFixed(1)}h`, name]}
                                labelFormatter={v => new Date(v + "T00:00:00").toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short" })}
                              />
                              {dailyHoursSeries.worked && (
                                <Bar dataKey="worked" name={T.worked || "Worked"} radius={[6, 6, 0, 0]} isAnimationActive animationDuration={500}>
                                  {daily.map((_, i) => (
                                    <Cell key={i}
                                      fill={i === peakIdx ? "url(#ztBarWorkedPeak)" : "url(#ztBarWorkedGrad)"}
                                      stroke={i === dailyHoursHover ? C.text : "none"}
                                      strokeWidth={i === dailyHoursHover ? 1 : 0}
                                      cursor="pointer"
                                    />
                                  ))}
                                </Bar>
                              )}
                              {dailyHoursSeries.break && (
                                <Bar dataKey="break" name={T.breakLabel || "Break"} radius={[6, 6, 0, 0]} isAnimationActive animationDuration={500}>
                                  {daily.map((_, i) => (
                                    <Cell key={`b-${i}`} fill="url(#ztBarBreakGrad)" opacity={i === dailyHoursHover ? 1 : 0.85} />
                                  ))}
                                </Bar>
                              )}
                            </BarChart>
                          </ResponsiveContainer>
  );
}

function DeptDonutBase({ C, analytics, hoveredDeptIdx, setHoveredDeptIdx, setStatusFilter, setTeamFilter, teamFilter }) {
  return (
                          <ResponsiveContainer width="100%" height={170}>
                            <PieChart>
                              <defs>
                                {/* Per-slice gradient pairs — vibrant top→deep bottom. */}
                                {[
                                  ["#FCA5A5","#DC2626"],  // red
                                  ["#FBBF24","#D97706"],  // amber
                                  ["#34D399","#0F9F5C"],  // green
                                  ["#60A5FA","#1D4ED8"],  // blue
                                  ["#C4B5FD","#7C3AED"],  // violet
                                  ["#F9A8D4","#DB2777"],  // pink
                                  ["#5EEAD4","#0D9488"],  // teal
                                  ["#FDBA74","#EA580C"],  // orange
                                ].map(([a, b], i) => (
                                  <linearGradient key={i} id={`ztDeptGrad${i}`} x1="0" y1="0" x2="1" y2="1">
                                    <stop offset="0%"   stopColor={a} />
                                    <stop offset="100%" stopColor={b} />
                                  </linearGradient>
                                ))}
                              </defs>
                              <Pie
                                data={analytics.dept_hours}
                                dataKey="hours" nameKey="name"
                                cx="50%" cy="50%"
                                innerRadius={45} outerRadius={75}
                                paddingAngle={3} strokeWidth={0}
                                activeIndex={hoveredDeptIdx ?? -1}
                                activeShape={(props) => {
                                  const total = analytics.dept_hours.reduce((s, x) => s + (x.hours || 0), 0) || 1;
                                  const pct = ((props.value || 0) / total) * 100;
                                  // Wrap the department name so long names (e.g. "Technology
                                  // Infrastructure") fit inside the ~90px donut hole instead of
                                  // getting clipped. Break into ≤2 lines of ≤14 chars each.
                                  const nameLines = wrapLabel(props.name, 14, 2);
                                  const lineH = 12;
                                  const nameTop = props.cy - 8 - (nameLines.length - 1) * lineH;
                                  return (
                                    <g>
                                      {nameLines.map((ln, li) => (
                                        <text key={li} x={props.cx} y={nameTop + li * lineH}
                                              textAnchor="middle" fontSize={10} fontWeight={700} fill={C.text}>
                                          {ln}
                                        </text>
                                      ))}
                                      <text x={props.cx} y={props.cy + 14} textAnchor="middle" fontSize={12} fontWeight={800} fill={props.fill}>
                                        {props.value.toFixed(1)}h · {pct.toFixed(0)}%
                                      </text>
                                      <Sector {...props} outerRadius={props.outerRadius + 6} />
                                      <Sector cx={props.cx} cy={props.cy}
                                              innerRadius={props.outerRadius + 8}
                                              outerRadius={props.outerRadius + 11}
                                              startAngle={props.startAngle} endAngle={props.endAngle}
                                              fill={props.fill} opacity={0.35} />
                                    </g>
                                  );
                                }}
                                onMouseEnter={(_, i) => setHoveredDeptIdx(i)}
                                onMouseLeave={() => setHoveredDeptIdx(null)}
                                onClick={(d) => {
                                  if (d?.name) {
                                    setTeamFilter(String(d.name));
                                    setStatusFilter("all");
                                  }
                                }}
                                isAnimationActive animationDuration={600}
                                style={{ cursor: "pointer" }}
                              >
                                {analytics.dept_hours.map((d, i) => (
                                  <Cell
                                    key={i}
                                    fill={`url(#ztDeptGrad${i % 8})`}
                                    stroke={teamFilter === d.name ? C.text : "none"}
                                    strokeWidth={teamFilter === d.name ? 2 : 0}
                                  />
                                ))}
                              </Pie>
                              {/* No <Tooltip> here — the hovered slice's name/hours/%
                                  already render in the donut center, and a floating
                                  tooltip would overlap and obscure that label. */}
                            </PieChart>
                          </ResponsiveContainer>
  );
}

function TopEmployeesChartBase({ C, analytics, employees, hoveredEmpIdx, isDark, setEditEmp, setHoveredEmpIdx }) {
  return (
                          <ResponsiveContainer width="100%" height={220}>
                            <BarChart
                              data={analytics.emp_hours} layout="vertical"
                              margin={{ top: 0, right: 40, left: 0, bottom: 0 }}
                              onMouseLeave={() => setHoveredEmpIdx(-1)}
                              onMouseMove={(e) => { if (e?.activeTooltipIndex != null) setHoveredEmpIdx(e.activeTooltipIndex); }}
                            >
                              <defs>
                                {/* Top 3 get medal gradients. The rest rotate through a
                                    vibrant rainbow palette so each bar is visually distinct. */}
                                <linearGradient id="ztEmpGold"   x1="0" y1="0" x2="1" y2="0">
                                  <stop offset="0%" stopColor="#FEF08A" /><stop offset="100%" stopColor="#F59E0B" />
                                </linearGradient>
                                <linearGradient id="ztEmpSilver" x1="0" y1="0" x2="1" y2="0">
                                  <stop offset="0%" stopColor="#E2E8F0" /><stop offset="100%" stopColor="#64748B" />
                                </linearGradient>
                                <linearGradient id="ztEmpBronze" x1="0" y1="0" x2="1" y2="0">
                                  <stop offset="0%" stopColor="#FDBA74" /><stop offset="100%" stopColor="#C2410C" />
                                </linearGradient>
                                {[
                                  ["#6EE7B7","#059669"],  // emerald
                                  ["#7DD3FC","#0284C7"],  // sky
                                  ["#A78BFA","#7C3AED"],  // violet
                                  ["#F9A8D4","#DB2777"],  // pink
                                  ["#FCA5A5","#DC2626"],  // red
                                  ["#5EEAD4","#0D9488"],  // teal
                                  ["#FDE68A","#CA8A04"],  // yellow
                                  ["#C4B5FD","#6D28D9"],  // purple
                                  ["#86EFAC","#15803D"],  // green
                                  ["#FDBA74","#EA580C"],  // orange
                                ].map(([a, b], i) => (
                                  <linearGradient key={i} id={`ztEmpRainbow${i}`} x1="0" y1="0" x2="1" y2="0">
                                    <stop offset="0%"   stopColor={a} />
                                    <stop offset="100%" stopColor={b} />
                                  </linearGradient>
                                ))}
                              </defs>
                              <CartesianGrid strokeDasharray="3 3" stroke={isDark ? "#2D3748" : "#F0F0F0"} horizontal={false} />
                              <XAxis type="number" tick={{ fontSize: 10, fill: C.textLight }} tickFormatter={v => `${v}h`} />
                              <YAxis dataKey="name" type="category" width={80} tick={{ fontSize: 11, fill: C.textSub, fontWeight: 600 }} />
                              <Tooltip
                                cursor={{ fill: isDark ? "rgba(34,197,94,0.08)" : "rgba(34,197,94,0.10)" }}
                                contentStyle={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10, fontSize: 12, boxShadow: "0 8px 24px rgba(0,0,0,0.12)" }}
                                formatter={v => [`${v.toFixed(1)}h`]}
                              />
                              <Bar
                                dataKey="worked" name="Worked"
                                radius={[0, 8, 8, 0]} barSize={20}
                                style={{ cursor: "pointer" }}
                                isAnimationActive animationDuration={520}
                                label={({ x, y, width, height, value, index }) => {
                                  const medals = ["🥇", "🥈", "🥉"];
                                  return (
                                    <g>
                                      <text x={x + width + 8} y={y + height / 2 + 4} fill={C.text} fontSize={11} fontWeight={700}>
                                        {Number(value).toFixed(1)}h
                                      </text>
                                      {index < 3 && (
                                        <text x={x + width + 30} y={y + height / 2 + 4} fontSize={12}>{medals[index]}</text>
                                      )}
                                    </g>
                                  );
                                }}
                                onClick={(d) => {
                                  if (!d?.name) return;
                                  const match = employees.find(e => (e.name || "") === d.name);
                                  if (match) setEditEmp(match);
                                }}
                              >
                                {analytics.emp_hours.map((_, i) => {
                                  const grad =
                                    i === 0 ? "url(#ztEmpGold)" :
                                    i === 1 ? "url(#ztEmpSilver)" :
                                    i === 2 ? "url(#ztEmpBronze)" :
                                              `url(#ztEmpRainbow${(i - 3) % 10})`;
                                  return (
                                    <Cell
                                      key={i}
                                      fill={grad}
                                      opacity={hoveredEmpIdx === -1 || hoveredEmpIdx === i ? 1 : 0.55}
                                      stroke={hoveredEmpIdx === i ? C.text : "none"}
                                      strokeWidth={hoveredEmpIdx === i ? 1.5 : 0}
                                      cursor="pointer"
                                    />
                                  );
                                })}
                              </Bar>
                            </BarChart>
                          </ResponsiveContainer>
  );
}

function AttendanceDonutBase({ C, analytics, hoveredAttIdx, setHoveredAttIdx, setStatusFilter, statusFilter }) {
  return (
                      <ResponsiveContainer width="100%" height={140}>
                        <PieChart>
                          <defs>
                            <linearGradient id="ztAttWorking" x1="0" y1="0" x2="1" y2="1">
                              <stop offset="0%" stopColor="#86EFAC" /><stop offset="100%" stopColor="#15803D" />
                            </linearGradient>
                            <linearGradient id="ztAttBreak" x1="0" y1="0" x2="1" y2="1">
                              <stop offset="0%" stopColor="#FCD34D" /><stop offset="100%" stopColor="#B45309" />
                            </linearGradient>
                            <linearGradient id="ztAttAbsent" x1="0" y1="0" x2="1" y2="1">
                              <stop offset="0%" stopColor="#FCA5A5" /><stop offset="100%" stopColor="#B91C1C" />
                            </linearGradient>
                          </defs>
                          <Pie
                            data={[
                              { name: "Working",  value: Math.max(analytics.attendance.working, 0),  filterKey: "working",  grad: "ztAttWorking", color: "#22C55E" },
                              { name: "On Break", value: Math.max(analytics.attendance.on_break, 0), filterKey: "on_break", grad: "ztAttBreak",   color: "#F59E0B" },
                              { name: "Absent",   value: Math.max(analytics.attendance.absent, 0),   filterKey: "absent",   grad: "ztAttAbsent",  color: "#EF4444" },
                            ].filter(d => d.value > 0)}
                            dataKey="value" nameKey="name"
                            cx="50%" cy="50%" innerRadius={35} outerRadius={58}
                            paddingAngle={4} strokeWidth={0}
                            style={{ cursor: "pointer" }}
                            activeIndex={hoveredAttIdx ?? -1}
                            activeShape={(props) => {
                              const total = analytics.attendance.total || 1;
                              const pct = ((props.value || 0) / total) * 100;
                              return (
                                <g>
                                  <text x={props.cx} y={props.cy - 4} textAnchor="middle" fontSize={10} fontWeight={700} fill={C.text}>
                                    {props.name}
                                  </text>
                                  <text x={props.cx} y={props.cy + 11} textAnchor="middle" fontSize={12} fontWeight={800} fill={props.payload?.color || props.fill}>
                                    {props.value} · {pct.toFixed(0)}%
                                  </text>
                                  <Sector {...props} outerRadius={props.outerRadius + 5} />
                                  <Sector cx={props.cx} cy={props.cy}
                                          innerRadius={props.outerRadius + 7}
                                          outerRadius={props.outerRadius + 10}
                                          startAngle={props.startAngle} endAngle={props.endAngle}
                                          fill={props.fill} opacity={0.35} />
                                </g>
                              );
                            }}
                            onMouseEnter={(_, i) => setHoveredAttIdx(i)}
                            onMouseLeave={() => setHoveredAttIdx(null)}
                            onClick={(d) => {
                              if (d?.filterKey) {
                                setStatusFilter(statusFilter === d.filterKey ? "all" : d.filterKey);
                              }
                            }}
                            isAnimationActive animationDuration={600}
                          >
                            <Cell fill="url(#ztAttWorking)" />
                            <Cell fill="url(#ztAttBreak)" />
                            <Cell fill="url(#ztAttAbsent)" />
                          </Pie>
                          {/* No <Tooltip>: the activeShape above already renders the
                              segment's name + count + % inside the donut. A tooltip
                              on top would stack a third "Working : 30" label over the
                              activeShape text and the static "77% PRESENT" overlay. */}
                        </PieChart>
                      </ResponsiveContainer>
  );
}

function TopAppsDonutBase({ C, hoveredAppIdx, overviewApps, setHoveredAppIdx }) {
  return (
                      <ResponsiveContainer width={200} height={200}>
                        <PieChart>
                          <defs>
                            {[
                              ["#A5B4FC","#4338CA"], // indigo
                              ["#6EE7B7","#047857"], // emerald
                              ["#FCD34D","#B45309"], // amber
                              ["#93C5FD","#1E40AF"], // blue
                              ["#F9A8D4","#BE185D"], // pink
                              ["#C4B5FD","#6D28D9"], // violet
                              ["#FDBA74","#C2410C"], // orange
                              ["#5EEAD4","#0D9488"], // teal
                            ].map(([a, b], i) => (
                              <linearGradient key={i} id={`ztAppGrad${i}`} x1="0" y1="0" x2="1" y2="1">
                                <stop offset="0%"   stopColor={a} />
                                <stop offset="100%" stopColor={b} />
                              </linearGradient>
                            ))}
                          </defs>
                          <Pie
                            data={overviewApps.map((a, i) => ({ name: a.app_name, value: a.total_seconds, idx: i }))}
                            dataKey="value" nameKey="name"
                            cx="50%" cy="50%"
                            innerRadius={55} outerRadius={88}
                            paddingAngle={2} strokeWidth={0}
                            activeIndex={hoveredAppIdx ?? -1}
                            activeShape={(props) => {
                              const total = overviewApps.reduce((s, x) => s + (x.total_seconds || 0), 0) || 1;
                              const pct = ((props.value || 0) / total) * 100;
                              const m = Math.floor((props.value || 0) / 60);
                              return (
                                <g>
                                  <text x={props.cx} y={props.cy - 6} textAnchor="middle" fontSize={11} fontWeight={700} fill={C.text}>
                                    {String(props.name).slice(0, 14)}
                                  </text>
                                  <text x={props.cx} y={props.cy + 10} textAnchor="middle" fontSize={12} fontWeight={800} fill={props.fill}>
                                    {Math.floor(m / 60)}h {m % 60}m · {pct.toFixed(0)}%
                                  </text>
                                  <Sector {...props} outerRadius={props.outerRadius + 5} />
                                  <Sector cx={props.cx} cy={props.cy}
                                          innerRadius={props.outerRadius + 7}
                                          outerRadius={props.outerRadius + 10}
                                          startAngle={props.startAngle} endAngle={props.endAngle}
                                          fill={props.fill} opacity={0.35} />
                                </g>
                              );
                            }}
                            onMouseEnter={(_, i) => setHoveredAppIdx(i)}
                            onMouseLeave={() => setHoveredAppIdx(null)}
                            isAnimationActive animationDuration={620}
                            style={{ cursor: "pointer" }}
                          >
                            {overviewApps.map((_, i) => <Cell key={i} fill={`url(#ztAppGrad${i % 8})`} />)}
                          </Pie>
                          <Tooltip contentStyle={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10, fontSize: 12, boxShadow: "0 8px 24px rgba(0,0,0,0.12)" }} formatter={v => { const m = Math.floor(v / 60); return [`${Math.floor(m / 60)}h ${m % 60}m`]; }} />
                        </PieChart>
                      </ResponsiveContainer>
  );
}

// PERF: memoized so a hover on one chart (which flips only that chart's hover
// state in the parent) no longer re-renders the other four, and so an
// unrelated parent re-render with unchanged data props skips the (expensive)
// recharts SVG recompute entirely. Exported under the original names so the
// lazy() imports in AdminDashboard are unchanged.
export const DailyHoursChart   = React.memo(DailyHoursChartBase);
export const DeptDonut         = React.memo(DeptDonutBase);
export const TopEmployeesChart = React.memo(TopEmployeesChartBase);
export const AttendanceDonut   = React.memo(AttendanceDonutBase);
export const TopAppsDonut      = React.memo(TopAppsDonutBase);

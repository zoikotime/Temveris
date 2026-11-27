import React from "react";

// Extracted verbatim from AdminDashboard.jsx (the "browsercategories" tab) so
// its ~250 lines lazy-load on demand instead of shipping in the main
// AdminDashboard chunk. Shared theme (C)/helpers/state are passed as props —
// AdminDashboard keeps a module-level mutable theme `C`, so threading it in
// (rather than importing) avoids a circular dependency.
export default function BrowserCategoriesTab({
  C, T, isDark, Icon, initials, card, search,
  browserLive, employees, siteUsageData, siteUsageLoading,
  siteUsageEmp, setSiteUsageEmp, siteUsageDate, setSiteUsageDate,
  bcOverrides, bcDefaults, fetchSiteUsage, fetchBrowserLive,
}) {
              const thS = { padding: "11px 16px", textAlign: "left", color: C.textSub, fontWeight: 700, fontSize: 11, textTransform: "uppercase", letterSpacing: "0.5px", borderBottom: `1px solid ${C.border}` };

              const empById = Object.fromEntries(employees.map(e => [String(e.id), e]));

              // Honour the global header search: filter both the live grid and the
              // per-user table by employee name so this tab behaves like the rest.
              const _q = (search || "").trim().toLowerCase();
              const _matchName = (userId, fallback) =>
                !_q || (empById[String(userId)]?.name || fallback || "").toLowerCase().includes(_q);

              // Merge employees with live browser data
              const liveEntries = Object.values(browserLive).filter(bl => _matchName(bl.user_id, bl.name));

              // Today's app usage per user from /api/app-analytics
              const usersToday = (siteUsageData?.users || []).filter(u => _matchName(u.user_id, u.name));

              const fmtSecs2 = s => {
                if (!s || s < 60) return `${s || 0}s`;
                const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
                return h > 0 ? `${h}h ${m}m` : `${m}m`;
              };

              // catStyle accepts both the legacy 3-bucket vocabulary used by
              // the browser-domain table (work / neutral / distraction) AND
              // the App Review 4-bucket vocabulary now emitted by
              // /api/app-analytics (productive / neutral / distracting /
              // blocked). Both get sensible colors so a single style helper
              // works for live tiles (browser) and the table rows (apps).
              const catStyle = (cat) => {
                if (cat === "work" || cat === "productive")
                  return { bg: "#DCFCE7", color: "#2F8F5C", border: "#BBF7D0", label: T.appCatProductive || "Productive" };
                if (cat === "distraction" || cat === "distracting")
                  return { bg: "#FEE2E2", color: "#DC2626", border: "#FCA5A5", label: T.appCatDistracting || "Distracting" };
                if (cat === "blocked")
                  return { bg: "#FEE2E2", color: "#7F1D1D", border: "#FCA5A5", label: T.appCatBlocked || "Blocked" };
                return { bg: isDark ? "#1F2937" : "#F3F4F6", color: C.textSub, border: C.border, label: T.appCatNeutral || "Neutral" };
              };

              // Used ONLY for browser-side live tiles where the value is a
              // website domain (e.g. "github.com"). For desktop apps coming
              // from /api/app-analytics, prefer the `category` field already
              // emitted by the backend — it respects App Review admin
              // overrides automatically.
              const catForDomain = (domain) => {
                if (!domain) return "neutral";
                const ov = bcOverrides.find(o => o.domain === domain);
                if (ov) return ov.category;
                const df = bcDefaults.find(d => d.domain === domain);
                if (df) return df.category;
                return "neutral";
              };

              // Filtered employees for the detail panel
              const selectedEmpData = siteUsageEmp ? usersToday.find(u => String(u.user_id) === String(siteUsageEmp)) : null;
              const selectedEmpObj  = siteUsageEmp ? employees.find(e => String(e.id) === String(siteUsageEmp)) : null;
              return (
                <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>

                  {/* ── Header ── */}
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 10 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                      <div style={{ width: 38, height: 38, borderRadius: 10, background: C.brandBg, border: `1px solid ${C.brandBdr}`, display: "flex", alignItems: "center", justifyContent: "center" }}>
                        <Icon n="monitor" s={17} c={C.brand} />
                      </div>
                      <div>
                        <h2 style={{ fontSize: 18, fontWeight: 800, color: C.text, margin: 0 }}>{T.siteAndAppUsage || "Site & App Usage"}</h2>
                        <p style={{ fontSize: 12, color: C.textSub, margin: 0 }}>{T.siteAppUsageDescription || "See which employee is using which app or website"}</p>
                      </div>
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <input type="date" value={siteUsageDate} max={new Date().toISOString().slice(0, 10)}
                        onChange={e => { setSiteUsageDate(e.target.value); fetchSiteUsage(e.target.value); }}
                        style={{ padding: "7px 10px", borderRadius: 8, border: `1px solid ${C.border}`, background: C.surface, color: C.text, fontSize: 12, fontFamily: "inherit", outline: "none" }} />
                      <button onClick={() => { fetchSiteUsage(siteUsageDate); fetchBrowserLive(); }}
                        style={{ display: "flex", alignItems: "center", gap: 6, padding: "8px 14px", borderRadius: 8, border: `1px solid ${C.border}`, background: C.surface, color: C.text, fontSize: 12, fontWeight: 600, cursor: "pointer", fontFamily: "inherit" }}>
                        <Icon n="refresh" s={13} c={C.textSub} />{T.refreshBtn || "Refresh"}
                      </button>
                    </div>
                  </div>

                  {/* ── Live Now (only meaningful for today — real-time data
                       ignores the date picker, so hide it on past dates or the
                       calendar looks broken when the grid never changes) ── */}
                  {liveEntries.length > 0 && siteUsageDate === new Date().toISOString().slice(0, 10) && (
                    <div style={{ ...card(), padding: 20 }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 14 }}>
                        <span style={{ width: 8, height: 8, borderRadius: "50%", background: "#22C55E", display: "inline-block", boxShadow: "0 0 6px #22C55E", animation: "pulse 2s infinite" }} />
                        <span style={{ fontSize: 13, fontWeight: 700, color: C.text }}>{T.liveNow || "Live Now"}</span>
                        <span style={{ fontSize: 11, color: C.textSub }}>{liveEntries.length} {liveEntries.length !== 1 ? (T.employees || "employees") : (T.employee || "employee")} {T.tracked || "tracked"}</span>
                      </div>
                      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))", gap: 12 }}>
                        {liveEntries.map(bl => {
                          const emp = empById[String(bl.user_id)];
                          const name = emp?.name || bl.name || `User ${bl.user_id}`;
                          const cat = catForDomain(bl.domain);
                          const cs  = catStyle(cat);
                          const statusEmoji = bl.is_idle ? "💤" : "✅";
                          return (
                            <div key={bl.user_id}
                              onClick={() => setSiteUsageEmp(prev => prev === bl.user_id ? null : bl.user_id)}
                              style={{ padding: "12px 14px", background: siteUsageEmp === bl.user_id ? C.brandBg : (isDark ? "#1F2937" : "#F9FAFB"), borderRadius: 10, border: `1.5px solid ${siteUsageEmp === bl.user_id ? C.brand : (bl.is_idle ? C.border : "#22C55E44")}`, cursor: "pointer", transition: "all .15s" }}>
                              <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
                                <div style={{ position: "relative", flexShrink: 0 }}>
                                  <div style={{ width: 34, height: 34, borderRadius: "50%", background: `linear-gradient(135deg,${C.brand},${C.brandDark})`, display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 800, fontSize: 12 }}>{initials(name)}</div>
                                  <div style={{ position: "absolute", bottom: 0, right: 0, width: 9, height: 9, borderRadius: "50%", background: bl.is_idle ? C.amber : "#22C55E", border: `1.5px solid ${C.surface}` }} />
                                </div>
                                <div style={{ minWidth: 0 }}>
                                  <div style={{ fontSize: 13, fontWeight: 700, color: C.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{name}</div>
                                  <div style={{ fontSize: 10, color: bl.is_idle ? C.amber : "#22C55E", fontWeight: 600 }}>{bl.is_idle ? (T.idle || "Idle") : (T.active || "Active")}</div>
                                </div>
                                <span style={{ marginLeft: "auto", fontSize: 14 }}>{statusEmoji}</span>
                              </div>
                              <div style={{ display: "flex", alignItems: "center", gap: 6, padding: "7px 10px", background: cs.bg, borderRadius: 7, border: `1px solid ${cs.border}` }}>
                                {bl.favicon_url
                                  ? <img src={bl.favicon_url} alt="" style={{ width: 14, height: 14, objectFit: "contain", flexShrink: 0 }} onError={e => { e.currentTarget.style.display = "none"; }} />
                                  : <Icon n="monitor" s={12} c={cs.color} />}
                                <span style={{ fontSize: 12, fontWeight: 600, color: cs.color, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}>{bl.domain || "—"}</span>
                                <span style={{ fontSize: 9, fontWeight: 700, color: cs.color, background: isDark ? "rgba(255,255,255,.08)" : "rgba(0,0,0,.06)", padding: "2px 6px", borderRadius: 4, flexShrink: 0, textTransform: "uppercase" }}>{cs.label}</span>
                              </div>
                              {bl.tab_title && (
                                <div style={{ fontSize: 10, color: C.textSub, marginTop: 5, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={bl.tab_title}>{bl.tab_title}</div>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  )}

                  {/* ── Employee detail panel (click to expand) ── */}
                  {selectedEmpData && selectedEmpObj && (
                    <div style={{ ...card(), padding: 20 }}>
                      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16 }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                          <div style={{ width: 36, height: 36, borderRadius: "50%", background: `linear-gradient(135deg,${C.brand},${C.brandDark})`, display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 800, fontSize: 13 }}>{initials(selectedEmpObj.name)}</div>
                          <div>
                            <div style={{ fontSize: 14, fontWeight: 800, color: C.text }}>{selectedEmpObj.name}</div>
                            <div style={{ fontSize: 11, color: C.textSub }}>{fmtSecs2(selectedEmpData.active_seconds)} {T.active || "active"} · {selectedEmpData.productivity}% {T.productivity || "productivity"}</div>
                          </div>
                        </div>
                        <button onClick={() => setSiteUsageEmp(null)} style={{ background: "none", border: "none", fontSize: 18, color: C.textSub, cursor: "pointer" }}>×</button>
                      </div>
                      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))", gap: 10 }}>
                        {(selectedEmpData.apps || []).slice(0, 12).map((a, i) => {
                          const cat = a.category || catForDomain(a.app);
                          const cs  = catStyle(cat);
                          // Per-app `seconds` is total time on that app
                          // (active+idle). Divide by the user's TOTAL clocked
                          // time (active+idle) so the bar never exceeds 100%.
                          const denom = (selectedEmpData.active_seconds || 0) + (selectedEmpData.idle_seconds || 0);
                          const pct = denom ? Math.round((a.seconds / denom) * 100) : 0;
                          const barColor = (cat === "distraction" || cat === "distracting" || cat === "blocked") ? "#EF4444"
                                         : (cat === "work" || cat === "productive") ? "#2F8F5C"
                                         : C.brand;
                          return (
                            <div key={i} style={{ padding: "10px 12px", background: isDark ? "#1F2937" : "#F9FAFB", borderRadius: 8, border: `1px solid ${cs.border}` }}>
                              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 6 }}>
                                <span style={{ fontSize: 12, fontWeight: 700, color: C.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 110 }}>{a.app}</span>
                                <span style={{ fontSize: 9, fontWeight: 700, color: cs.color, background: cs.bg, padding: "2px 6px", borderRadius: 4, textTransform: "uppercase" }}>{cs.label}</span>
                              </div>
                              <div style={{ fontSize: 12, color: C.brand, fontWeight: 700 }}>{fmtSecs2(a.seconds)}</div>
                              <div style={{ marginTop: 5, height: 4, borderRadius: 2, background: C.border, overflow: "hidden" }}>
                                <div style={{ height: "100%", width: `${Math.min(pct, 100)}%`, background: barColor, borderRadius: 2 }} />
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  )}

                  {/* ── Today's Usage Table ── */}
                  <div style={{ ...card(), overflow: "hidden" }}>
                    <div style={{ padding: "14px 18px", borderBottom: `1px solid ${C.border}`, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                      <span style={{ fontSize: 14, fontWeight: 800, color: C.text }}>{T.appSiteBreakdown || "App & Site Breakdown"} — {siteUsageDate === new Date().toISOString().slice(0, 10) ? (T.today || "Today") : siteUsageDate}</span>
                      <span style={{ fontSize: 11, color: C.textSub }}>{usersToday.length} {usersToday.length !== 1 ? (T.employees || "employees") : (T.employee || "employee")}</span>
                    </div>
                    {siteUsageLoading ? (
                      <div style={{ padding: 48, textAlign: "center" }}>
                        <div style={{ width: 28, height: 28, borderRadius: "50%", border: `3px solid ${C.brand}`, borderTopColor: "transparent", animation: "spin .8s linear infinite", margin: "0 auto 10px" }} />
                        <p style={{ color: C.textSub, fontSize: 12 }}>{T.loadingUsageData || "Loading usage data…"}</p>
                      </div>
                    ) : usersToday.length === 0 ? (
                      <div style={{ padding: 48, textAlign: "center" }}>
                        <p style={{ color: C.textSub, fontSize: 13 }}>{T.noAppSiteActivity || "No app/site activity recorded for this date."}</p>
                        <p style={{ color: C.textLight, fontSize: 11, marginTop: 4 }}>{T.activityCapturedNote || "Activity is captured when employees use the desktop tracker."}</p>
                      </div>
                    ) : (
                      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                        <thead>
                          <tr style={{ background: isDark ? "#1F2937" : "#F8FAFC" }}>
                            <th style={thS}>{T.employee || "Employee"}</th>
                            <th style={thS}>{T.topAppSite || "Top App / Site"}</th>
                            <th style={thS}>{T.activeTimeLabel || "Active Time"}</th>
                            <th style={thS}>{T.productivity || "Productivity"}</th>
                            <th style={thS}>{T.allAppsUsed || "All Apps Used"}</th>
                          </tr>
                        </thead>
                        <tbody>
                          {usersToday.map((u) => {
                            const emp   = empById[String(u.user_id)];
                            const name  = emp?.name || u.name || `User ${u.user_id}`;
                            // Backend now returns top_app_category from the unified
                            // App Review categoriser (respects admin overrides).
                            // Falls back to the domain map only if missing.
                            const topCat = u.top_app_category || catForDomain(u.top_app);
                            const topCs  = catStyle(topCat);
                            const prodColor = u.productivity >= 70 ? "#22C55E" : u.productivity >= 40 ? C.amber : "#EF4444";
                            return (
                              <tr key={u.user_id}
                                onClick={() => setSiteUsageEmp(prev => prev === u.user_id ? null : u.user_id)}
                                style={{ borderBottom: `1px solid ${C.borderLight}`, cursor: "pointer", background: siteUsageEmp === u.user_id ? C.brandBg : "transparent", transition: "background .1s" }}
                                onMouseEnter={e => { if (siteUsageEmp !== u.user_id) e.currentTarget.style.background = isDark ? "#1F2937" : "#F9FAFB"; }}
                                onMouseLeave={e => { if (siteUsageEmp !== u.user_id) e.currentTarget.style.background = "transparent"; }}>
                                <td style={{ padding: "13px 16px" }}>
                                  <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                                    <div style={{ width: 30, height: 30, borderRadius: "50%", background: `linear-gradient(135deg,${C.brand},${C.brandDark})`, display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 800, fontSize: 11, flexShrink: 0 }}>{initials(name)}</div>
                                    <div>
                                      <div style={{ fontWeight: 700, color: C.text }}>{name}</div>
                                      <div style={{ fontSize: 10, color: C.textSub }}>{emp?.department || emp?.designation || ""}</div>
                                    </div>
                                  </div>
                                </td>
                                <td style={{ padding: "13px 16px" }}>
                                  <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                                    <span style={{ fontSize: 12, fontWeight: 700, color: topCs.color, background: topCs.bg, border: `1px solid ${topCs.border}`, padding: "3px 10px", borderRadius: 6 }}>{u.top_app || "—"}</span>
                                    {u.top_app_seconds > 0 && <span style={{ fontSize: 11, color: C.textSub }}>{fmtSecs2(u.top_app_seconds)}</span>}
                                  </div>
                                </td>
                                <td style={{ padding: "13px 16px", fontWeight: 700, color: C.brand, fontVariantNumeric: "tabular-nums" }}>{fmtSecs2(u.active_seconds)}</td>
                                <td style={{ padding: "13px 16px" }}>
                                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                                    <div style={{ flex: 1, height: 6, background: C.border, borderRadius: 3, overflow: "hidden", maxWidth: 80 }}>
                                      <div style={{ height: "100%", width: `${u.productivity}%`, background: prodColor, borderRadius: 3, transition: "width .4s" }} />
                                    </div>
                                    <span style={{ fontSize: 12, fontWeight: 700, color: prodColor, minWidth: 34 }}>{u.productivity}%</span>
                                  </div>
                                </td>
                                <td style={{ padding: "13px 16px" }}>
                                  <div style={{ display: "flex", gap: 4, flexWrap: "wrap", maxWidth: 320 }}>
                                    {(u.apps || []).slice(0, 6).map((a, i) => {
                                      const ac  = a.category || catForDomain(a.app);
                                      const acs = catStyle(ac);
                                      return (
                                        <span key={i} title={`${a.app} — ${fmtSecs2(a.seconds)}`}
                                          style={{ fontSize: 10, fontWeight: 600, color: acs.color, background: acs.bg, border: `1px solid ${acs.border}`, padding: "2px 7px", borderRadius: 4, whiteSpace: "nowrap" }}>
                                          {a.app}
                                        </span>
                                      );
                                    })}
                                    {u.apps?.length > 6 && <span style={{ fontSize: 10, color: C.textSub, alignSelf: "center" }}>+{u.apps.length - 6} more</span>}
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

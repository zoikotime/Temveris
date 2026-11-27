import React, { useMemo } from "react";
import { API_BASE } from "../../config";
import AuthImage from "./AuthImage";
// Extracted from AdminDashboard.jsx ("screenshots" tab) for lazy-loading.
// Shared theme/helpers/state/callbacks passed as props. `effectiveStatus` and
// `employees` are passed read-only (used to label a worker's status on a
// screenshot card) — the shared status engine itself is unchanged.
export default function ScreenshotsTab({
  C, T, isDark, Icon, AppIcon, initials, fmtTimeIST, getAppMeta, effectiveStatus, employees,
  token, showToast, fetchScreenshots, screenshotData, ssLoading, ssBlurImages,
  ssShowIdle, setSsShowIdle, ssSort, setSsSort, ssActivityFilter, setSsActivityFilter,
  ssDateFilter, setSsDateFilter, ssEmpSearch, setSsEmpSearch, ssAppSearch, setSsAppSearch,
  ssSelectedUser, setSsSelectedUser, ssSelectedIds, setSsSelectedIds, setLightboxUrl,
}) {
              // Memoized so the per-keystroke member search (ssEmpSearch updates
              // state on every key) no longer re-runs the localeCompare sort or
              // re-buckets up to ~500 screenshots on every render.
              const visible = useMemo(
                () => (ssShowIdle ? screenshotData : screenshotData.filter(s => s.is_active)),
                [screenshotData, ssShowIdle],
              );
              const sortedEmps = useMemo(
                () => [...employees].sort((a, b) => (a.name || "").localeCompare(b.name || "")),
                [employees],
              );
              const visibleEmps = useMemo(() => {
                const q = ssEmpSearch.toLowerCase();
                return sortedEmps.filter(e => (e.name || "").toLowerCase().includes(q));
              }, [sortedEmps, ssEmpSearch]);
              const ssGroups = useMemo(() => {
                const g = {};
                visible.forEach(s => { const name = s.employee_name || "Unknown"; if (!g[name]) g[name] = []; g[name].push(s); });
                return g;
              }, [visible]);
              const allSelected = visible.length > 0 && ssSelectedIds.size === visible.length;

              const handleToggleId = (id) => setSsSelectedIds(prev => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n; });
              const handleDeleteSelected = async () => {
                for (const id of ssSelectedIds) {
                  await fetch(`${API_BASE}/api/screenshots/${id}`, { method: "DELETE", headers: { Authorization: `Bearer ${token}` } });
                }
                const deletedCount = ssSelectedIds.size;
                setSsSelectedIds(new Set());
                // Background refresh: drops the deleted cards without wiping the
                // grid, so the admin stays at their current scroll position.
                fetchScreenshots({ background: true });
                showToast(`${deletedCount} screenshot(s) deleted`);
              };

              return (
                <div style={{ display: "flex", gap: 0, margin: "-28px -28px -40px", height: "calc(100vh - 64px)", overflow: "hidden" }}>
                  <div style={{ width: 210, flexShrink: 0, borderRight: `1px solid ${C.border}`, display: "flex", flexDirection: "column", background: C.surface, overflowY: "auto" }}>
                    <div style={{ padding: "12px 10px 8px" }}>
                      <div style={{ position: "relative" }}>
                        <div style={{ position: "absolute", left: 8, top: "50%", transform: "translateY(-50%)" }}><Icon n="search" s={12} c={C.textLight} /></div>
                        <input placeholder={T.searchMembers||"Search Members"} value={ssEmpSearch} onChange={e => setSsEmpSearch(e.target.value)}
                          style={{ width: "100%", padding: "7px 8px 7px 26px", borderRadius: C.rSm, border: `1px solid ${C.border}`, background: C.bg, color: C.text, fontSize: 12, fontFamily: "inherit", outline: "none", boxSizing: "border-box" }} />
                      </div>
                    </div>
                    <div onClick={() => setSsSelectedUser(null)}
                      style={{ padding: "9px 14px", cursor: "pointer", display: "flex", alignItems: "center", gap: 9, borderLeft: `3px solid ${ssSelectedUser === null ? C.brand : "transparent"}`, background: ssSelectedUser === null ? C.brandBg : "transparent" }}>
                      <div style={{ width: 30, height: 30, borderRadius: "50%", background: C.brand, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}><Icon n="users" s={13} c="#fff" /></div>
                      <span style={{ fontSize: 13, fontWeight: 700, color: ssSelectedUser === null ? C.brand : C.text }}>Team View</span>
                    </div>
                    {visibleEmps.map(e => {
                      const empStatus = (effectiveStatus(e) || "").toLowerCase();
                      const isOnline = ["working", "on break", "break"].includes(empStatus);
                      return (
                        <div key={e.id} onClick={() => setSsSelectedUser(e.id)}
                          style={{ padding: "8px 14px", cursor: "pointer", display: "flex", alignItems: "center", gap: 9, borderLeft: `3px solid ${ssSelectedUser === e.id ? C.brand : "transparent"}`, background: ssSelectedUser === e.id ? C.brandBg : "transparent" }}>
                          <div style={{ position: "relative", flexShrink: 0 }}>
                            <div style={{ width: 30, height: 30, borderRadius: "50%", background: `hsl(${(e.id || 0) * 37},55%,55%)`, display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 700, fontSize: 11 }}>{initials(e.name)}</div>
                            <div title={isOnline ? "Online" : "Offline"} style={{ position: "absolute", bottom: 0, right: 0, width: 9, height: 9, borderRadius: "50%", background: isOnline ? "#22C55E" : "#9CA3AF", border: "1.5px solid #fff" }} />
                          </div>
                          <span style={{ fontSize: 13, color: ssSelectedUser === e.id ? C.brand : C.text, fontWeight: ssSelectedUser === e.id ? 700 : 400, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}>{e.name}</span>
                        </div>
                      );
                    })}
                  </div>

                  <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden", background: C.bg }}>
                    <div style={{ padding: "8px 18px", background: isDark ? "#3D1F1A" : "#FFF5F5", borderBottom: `1px solid ${isDark ? "#991B1B" : C.border}`, fontSize: 12, color: isDark ? "#FECACA" : C.red, fontWeight: 600 }}>
                      {T.inactiveScreenshotNote || "Note: If the user is inactive, screenshots taken during that time will have a red border."}
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 16px", borderBottom: `1px solid ${C.border}`, background: C.surface, flexWrap: "wrap" }}>
                      <div style={{ position: "relative", flex: 1, minWidth: 160, maxWidth: 280 }}>
                        <div style={{ position: "absolute", left: 9, top: "50%", transform: "translateY(-50%)" }}><Icon n="search" s={12} c={C.textLight} /></div>
                        <input placeholder={T.searchByApp||"Search by app or site name"} value={ssAppSearch} onChange={e => setSsAppSearch(e.target.value)}
                          style={{ width: "100%", padding: "7px 10px 7px 28px", borderRadius: C.rSm, border: `1px solid ${C.border}`, background: C.bg, color: C.text, fontSize: 12, fontFamily: "inherit", outline: "none", boxSizing: "border-box" }} />
                      </div>
                      <div style={{ flex: 1 }} />
                      <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: C.textSub, cursor: "pointer", userSelect: "none" }}>
                        <input type="checkbox" checked={allSelected} onChange={() => setSsSelectedIds(allSelected ? new Set() : new Set(visible.map(s => s.id)))} />
                        {T.selectAll || "Select All"}
                      </label>
                      {ssSelectedIds.size > 0 && (
                        <button onClick={handleDeleteSelected} style={{ padding: "6px 13px", borderRadius: C.rSm, border: "none", background: C.red, color: "#fff", fontSize: 12, cursor: "pointer", fontFamily: "inherit", fontWeight: 700 }}>
                          {T.delete || "Delete"} ({ssSelectedIds.size})
                        </button>
                      )}
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "9px 16px", borderBottom: `1px solid ${C.border}`, background: C.surface, flexWrap: "wrap" }}>
                      {/* ── Date navigator (‹ date ›) ── */}
                      {(() => {
                        const today = new Date().toISOString().split("T")[0];
                        const isToday = ssDateFilter === today;
                        const prevDay = () => { const d = new Date(ssDateFilter); d.setDate(d.getDate() - 1); setSsDateFilter(d.toISOString().split("T")[0]); };
                        const nextDay = () => { const d = new Date(ssDateFilter); d.setDate(d.getDate() + 1); const nxt = d.toISOString().split("T")[0]; if (nxt <= today) setSsDateFilter(nxt); };
                        const label = (() => {
                          if (ssDateFilter === today) return T.today || "Today";
                          const yest = new Date(); yest.setDate(yest.getDate() - 1);
                          if (ssDateFilter === yest.toISOString().split("T")[0]) return T.yesterday || "Yesterday";
                          return new Date(ssDateFilter + "T00:00:00").toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
                        })();
                        return (
                          <div style={{ display: "flex", alignItems: "center", gap: 4, background: C.bg, border: `1px solid ${C.border}`, borderRadius: C.rSm, padding: "2px 4px" }}>
                            <button onClick={prevDay} style={{ background: "none", border: "none", cursor: "pointer", padding: "3px 7px", borderRadius: 4, color: C.brand, fontWeight: 700, fontSize: 16, lineHeight: 1, display: "flex", alignItems: "center" }}>‹</button>
                            <input type="date" value={ssDateFilter} onChange={e => setSsDateFilter(e.target.value)}
                              style={{ border: "none", background: "transparent", color: C.text, fontSize: 12, fontFamily: "inherit", outline: "none", cursor: "pointer", width: 0, opacity: 0, position: "absolute" }} />
                            <span onClick={e => e.currentTarget.previousSibling.showPicker?.()} style={{ fontSize: 12, fontWeight: 700, color: C.text, minWidth: 90, textAlign: "center", cursor: "pointer" }}>
                              {label}
                            </span>
                            <button onClick={nextDay} style={{ background: "none", border: "none", cursor: isToday ? "default" : "pointer", padding: "3px 7px", borderRadius: 4, color: isToday ? C.textLight : C.brand, fontWeight: 700, fontSize: 16, lineHeight: 1, display: "flex", alignItems: "center" }}>›</button>
                          </div>
                        );
                      })()}
                      {ssDateFilter !== new Date().toISOString().split("T")[0] && (
                        <button onClick={() => setSsDateFilter(new Date().toISOString().split("T")[0])}
                          style={{ padding: "4px 10px", borderRadius: C.rSm, border: `1px solid ${C.brandBdr}`, background: C.brandBg, color: C.brand, fontSize: 11, fontWeight: 700, cursor: "pointer", fontFamily: "inherit" }}>
                          {T.today || "Today"}
                        </button>
                      )}
                      {/* Activity filter */}
                      <div style={{ position: "relative" }}>
                        <select value={ssActivityFilter} onChange={e => setSsActivityFilter(e.target.value)}
                          style={{ padding: "5px 24px 5px 10px", borderRadius: C.rSm, border: `1px solid ${C.border}`, background: C.bg, color: C.text, fontSize: 12, fontFamily: "inherit", outline: "none", appearance: "none", cursor: "pointer" }}>
                          <option value="all">{T.allActivity || "All Activity"}</option>
                          <option value="active">{T.activeOnly || "Active Only"}</option>
                          <option value="idle">{T.stoppedWorkingOnly || "Stopped Working Only"}</option>
                        </select>
                        <div style={{ position: "absolute", right: 7, top: "50%", transform: "translateY(-50%)", pointerEvents: "none" }}><Icon n="chevron_down" s={11} c={C.textSub} /></div>
                      </div>
                      <div style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 12, fontWeight: 700, color: C.text }}>
                        <Icon n="camera" s={13} c={C.textSub} />
                        <span style={{ fontSize: 11, color: C.textLight, fontWeight: 400 }}>({visible.length})</span>
                      </div>
                      <div style={{ flex: 1 }} />
                      <label style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 12, color: C.textSub, cursor: "pointer", userSelect: "none" }}>
                        {T.idle || "Idle"}
                        <div onClick={() => setSsShowIdle(v => !v)}
                          style={{ width: 34, height: 18, borderRadius: 99, background: ssShowIdle ? C.brand : C.border, position: "relative", cursor: "pointer", transition: "background .2s", flexShrink: 0 }}>
                          <div style={{ position: "absolute", top: 2, left: ssShowIdle ? 18 : 2, width: 14, height: 14, borderRadius: "50%", background: "#fff", transition: "left .2s", boxShadow: "0 1px 3px rgba(0,0,0,.2)" }} />
                        </div>
                      </label>
                      <button onClick={() => setSsSort(s => s === "newest" ? "oldest" : "newest")}
                        style={{ display: "flex", alignItems: "center", gap: 5, padding: "5px 11px", borderRadius: C.rSm, border: `1px solid ${C.border}`, background: C.bg, color: C.brand, fontSize: 12, fontWeight: 700, cursor: "pointer", fontFamily: "inherit" }}>
                        {ssSort === "newest" ? (T.newest || "Newest") : (T.oldest || "Oldest")}
                        <Icon n={ssSort === "newest" ? "arrowdown" : "arrowup"} s={11} c={C.brand} />
                      </button>
                    </div>
                    <div style={{ flex: 1, overflowY: "auto", padding: 16 }}>
                      {ssLoading ? (
                        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: 200, gap: 10 }}>
                          <div style={{ width: 22, height: 22, borderRadius: "50%", border: `3px solid ${C.brand}`, borderTopColor: "transparent", animation: "spin .8s linear infinite" }} />
                          <span style={{ color: C.textSub, fontSize: 13 }}>{T.loadingScreenshots || "Loading screenshots..."}</span>
                        </div>
                      ) : visible.length === 0 ? (
                        <div style={{ textAlign: "center", padding: "60px 0" }}>
                          <Icon n="camera" s={38} c={C.textLight} />
                          <p style={{ marginTop: 14, fontSize: 15, color: C.textLight, fontWeight: 600 }}>{T.noScreenshotsDate || "No screenshots for this date"}</p>
                          {ssSelectedUser !== null && (() => {
                            const emp = employees.find(e => e.id === ssSelectedUser);
                            const trackerLive = emp?.tracker_last_seen
                              ? (Date.now() - new Date(emp.tracker_last_seen).getTime()) < 5 * 60 * 1000
                              : false;
                            return !trackerLive ? (
                              <div style={{ marginTop: 10, display: "inline-flex", alignItems: "center", gap: 7, background: isDark ? "#3D2A0A" : "#FEF3C7", border: `1px solid ${isDark ? "#92400E" : "#FCD34D"}`, borderRadius: 8, padding: "8px 14px" }}>
                                <span style={{ fontSize: 16 }}>⚠️</span>
                                <span style={{ fontSize: 12, color: isDark ? "#FCD34D" : "#92400E", fontWeight: 600 }}>{T.trackerNotRunning || "Desktop tracker is not running for this employee. Screenshots cannot be captured without the tracker."}</span>
                              </div>
                            ) : (
                              <p style={{ fontSize: 12, color: C.textLight, marginTop: 4 }}>Try navigating to a different day</p>
                            );
                          })()}
                          {ssSelectedUser === null && (
                            <p style={{ fontSize: 12, color: C.textLight, marginTop: 4 }}>Try navigating to a different day or check that the desktop tracker is running</p>
                          )}
                        </div>
                      ) : ssSelectedUser === null ? (
                        /* ── Team View: group by employee name ── */
                        (() => {
                          const groups = ssGroups;
                          return (
                            <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
                              {Object.keys(groups).sort((a, b) => a.localeCompare(b)).map(empName => (
                                <div key={empName}>
                                  <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
                                    <div style={{ width: 32, height: 32, borderRadius: "50%", background: `hsl(${(empName.charCodeAt(0) * 37) % 360},55%,55%)`, display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 700, fontSize: 12, flexShrink: 0 }}>
                                      {initials(empName)}
                                    </div>
                                    <span style={{ fontSize: 14, fontWeight: 700, color: C.text }}>{empName}</span>
                                    <span style={{ fontSize: 11, color: C.textLight, fontWeight: 400 }}>({groups[empName].length} screenshot{groups[empName].length !== 1 ? "s" : ""})</span>
                                    <div style={{ flex: 1, height: 1, background: C.border }} />
                                  </div>
                                  <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))", gap: 12 }}>
                                    {groups[empName].map(s => {
                                      const sel = ssSelectedIds.has(s.id);
                                      const inactive = !s.is_active;
                                      // Prefer DB thumbnail (base64, always accessible), then Supabase signed URL,
                                      // then the API endpoint (which falls back to DB thumbnail internally).
                                      // Avoid using local file URLs (/uploads/...) — they are instance-specific on Cloud Run.
                                      const imgUrl = s.thumbnail ? `data:image/jpeg;base64,${s.thumbnail}` : (s.file_url && s.file_url.startsWith("http")) ? s.file_url : `${API_BASE}/api/screenshots/${s.id}/image`;
                                      return (
                                        <div key={s.id} onClick={() => handleToggleId(s.id)}
                                          style={{ background: C.surface, borderRadius: 10, border: `2px solid ${inactive ? "#EF4444" : sel ? C.brand : C.border}`, overflow: "hidden", cursor: "pointer", transition: "border .15s, box-shadow .15s", boxShadow: sel ? `0 0 0 3px ${C.brand}33` : "none" }}
                                          onMouseEnter={e => { if (!sel) e.currentTarget.style.boxShadow = "0 4px 14px rgba(0,0,0,.1)"; }}
                                          onMouseLeave={e => { if (!sel) e.currentTarget.style.boxShadow = "none"; }}>
                                          <div onClick={e => { e.stopPropagation(); setLightboxUrl({ url: imgUrl, name: s.employee_name || "Employee", appName: s.app_name, timestamp: s.timestamp, mouseClicks: s.mouse_clicks, keyPresses: s.key_presses }); }}
                                            style={{ position: "relative", height: 118, background: C.bg, overflow: "hidden", cursor: "zoom-in" }}>
                                            {imgUrl ? <AuthImage url={imgUrl} token={token} alt="screenshot" style={{ width: "100%", height: "100%", objectFit: "cover", display: "block", filter: ssBlurImages ? "blur(6px)" : "none" }} /> : <div style={{ height: "100%", display: "flex", alignItems: "center", justifyContent: "center" }}><Icon n="camera" s={24} c={C.textLight} /></div>}
                                            {inactive && <div style={{ position: "absolute", inset: 0, background: "rgba(239,68,68,0.12)", pointerEvents: "none" }} />}
                                            {inactive && <div style={{ position: "absolute", top: 6, right: 6, background: "#EF4444", color: "#fff", fontSize: 9, fontWeight: 800, padding: "2px 6px", borderRadius: 4 }}>IDLE</div>}
                                            <div title={sel ? (T.deselect || "Deselect") : (T.select || "Select")}
                                              onClick={e => { e.stopPropagation(); handleToggleId(s.id); }}
                                              style={{ position: "absolute", top: 6, left: 6, width: 16, height: 16, borderRadius: 4, border: `2px solid ${sel ? C.brand : "rgba(255,255,255,.85)"}`, background: sel ? C.brand : "rgba(0,0,0,.25)", display: "flex", alignItems: "center", justifyContent: "center", cursor: "pointer", zIndex: 2 }}>{sel && <Icon n="check" s={9} c="#fff" />}</div>
                                            {s.app_name && (() => { const ssApp = getAppMeta(s.app_name); return <div style={{ position: "absolute", bottom: 6, left: 6, display: "flex", alignItems: "center", gap: 4, background: "rgba(0,0,0,.55)", backdropFilter: "blur(4px)", padding: "2px 7px", borderRadius: 5 }}><AppIcon app={ssApp} size={13} /><span style={{ fontSize: 9, color: "#fff", fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 90 }}>{ssApp.label}</span></div>; })()}
                                          </div>
                                          <div style={{ padding: "8px 10px 9px" }}>
                                            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 3 }}>
                                              <span style={{ fontSize: 10, fontWeight: 700, color: C.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 110 }}>{s.employee_name || "Unknown"}</span>
                                              <span style={{ fontSize: 9, color: C.textLight, whiteSpace: "nowrap" }}>{fmtTimeIST(s.timestamp)}</span>
                                            </div>
                                            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                                              <div style={{ display: "flex", gap: 10 }}>
                                                <span style={{ display: "flex", alignItems: "center", gap: 3, fontSize: 10, color: C.textSub }}><span>🖱</span>{s.mouse_clicks}</span>
                                                <span style={{ display: "flex", alignItems: "center", gap: 3, fontSize: 10, color: C.textSub }}><span>⌨</span>{s.key_presses}</span>
                                              </div>
                                              <button onClick={e => { e.stopPropagation(); setLightboxUrl({ url: imgUrl, name: s.employee_name || "Employee", appName: s.app_name, timestamp: s.timestamp, mouseClicks: s.mouse_clicks, keyPresses: s.key_presses }); }} style={{ fontSize: 9, color: C.brand, background: "none", border: `1px solid ${C.brandBdr}`, borderRadius: 4, padding: "2px 6px", cursor: "pointer", fontFamily: "inherit", fontWeight: 600 }}>Open</button>
                                            </div>
                                          </div>
                                        </div>
                                      );
                                    })}
                                  </div>
                                </div>
                              ))}
                            </div>
                          );
                        })()
                      ) : (
                        /* ── Individual employee: flat grid ── */
                        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", gap: 14 }}>
                          {visible.map(s => {
                            const sel = ssSelectedIds.has(s.id);
                            const inactive = !s.is_active;
                            // Prefer DB thumbnail (base64, always accessible), then Supabase signed URL,
                            // then the API endpoint (which falls back to DB thumbnail internally).
                            const imgUrl = s.thumbnail ? `data:image/jpeg;base64,${s.thumbnail}` : (s.file_url && s.file_url.startsWith("http")) ? s.file_url : `${API_BASE}/api/screenshots/${s.id}/image`;
                            return (
                              <div key={s.id} onClick={() => handleToggleId(s.id)}
                                style={{ background: C.surface, borderRadius: 10, border: `2px solid ${inactive ? "#EF4444" : sel ? C.brand : C.border}`, overflow: "hidden", cursor: "pointer", transition: "border .15s, box-shadow .15s", boxShadow: sel ? `0 0 0 3px ${C.brand}33` : "none" }}
                                onMouseEnter={e => { if (!sel) e.currentTarget.style.boxShadow = "0 4px 14px rgba(0,0,0,.1)"; }}
                                onMouseLeave={e => { if (!sel) e.currentTarget.style.boxShadow = "none"; }}>
                                <div onClick={e => { e.stopPropagation(); setLightboxUrl({ url: imgUrl, name: s.employee_name || "Employee", appName: s.app_name, timestamp: s.timestamp, mouseClicks: s.mouse_clicks, keyPresses: s.key_presses }); }}
                                  style={{ position: "relative", height: 128, background: C.bg, overflow: "hidden", cursor: "zoom-in" }}>
                                  {imgUrl ? <AuthImage url={imgUrl} token={token} alt="screenshot" style={{ width: "100%", height: "100%", objectFit: "cover", display: "block", filter: ssBlurImages ? "blur(6px)" : "none" }} /> : <div style={{ height: "100%", display: "flex", alignItems: "center", justifyContent: "center" }}><Icon n="camera" s={26} c={C.textLight} /></div>}
                                  {inactive && <div style={{ position: "absolute", inset: 0, background: "rgba(239,68,68,0.12)", pointerEvents: "none" }} />}
                                  {inactive && <div style={{ position: "absolute", top: 7, right: 7, background: "#EF4444", color: "#fff", fontSize: 9, fontWeight: 800, padding: "2px 7px", borderRadius: 4, letterSpacing: ".5px" }}>IDLE</div>}
                                  <div title={sel ? (T.deselect || "Deselect") : (T.select || "Select")}
                                    onClick={e => { e.stopPropagation(); handleToggleId(s.id); }}
                                    style={{ position: "absolute", top: 7, left: 7, width: 17, height: 17, borderRadius: 4, border: `2px solid ${sel ? C.brand : "rgba(255,255,255,.85)"}`, background: sel ? C.brand : "rgba(0,0,0,.25)", display: "flex", alignItems: "center", justifyContent: "center", cursor: "pointer", zIndex: 2 }}>{sel && <Icon n="check" s={10} c="#fff" />}</div>
                                  {s.app_name && (() => { const ssApp = getAppMeta(s.app_name); return <div style={{ position: "absolute", bottom: 7, left: 7, display: "flex", alignItems: "center", gap: 5, background: "rgba(0,0,0,.55)", backdropFilter: "blur(4px)", padding: "3px 8px", borderRadius: 5 }}><AppIcon app={ssApp} size={14} /><span style={{ fontSize: 10, color: "#fff", fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 100 }}>{ssApp.label}</span></div>; })()}
                                </div>
                                <div style={{ padding: "9px 11px 10px" }}>
                                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
                                    <span style={{ fontSize: 11, fontWeight: 700, color: C.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 130 }}>{s.employee_name || "Unknown"}</span>
                                    <span style={{ fontSize: 10, color: C.textLight, whiteSpace: "nowrap" }}>{fmtTimeIST(s.timestamp)}</span>
                                  </div>
                                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                                    <div style={{ display: "flex", gap: 12 }}>
                                      <span style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 11, color: C.textSub }}><span style={{ fontSize: 12 }}>🖱</span>{s.mouse_clicks}</span>
                                      <span style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 11, color: C.textSub }}><span style={{ fontSize: 12 }}>⌨</span>{s.key_presses}</span>
                                    </div>
                                    <button onClick={e => { e.stopPropagation(); setLightboxUrl({ url: imgUrl, name: s.employee_name || "Employee", appName: s.app_name, timestamp: s.timestamp, mouseClicks: s.mouse_clicks, keyPresses: s.key_presses }); }} style={{ fontSize: 10, color: C.brand, background: "none", border: `1px solid ${C.brandBdr}`, borderRadius: 4, padding: "2px 8px", cursor: "pointer", fontFamily: "inherit", fontWeight: 600 }}>Open</button>
                                  </div>
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              );
}

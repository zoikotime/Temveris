import React from "react";
import { getOrgTimeZone } from "../../lib/orgTime";
import { API_BASE } from "../../config";
import AuthImage from "./AuthImage";
import { readPlaybackDeepLink } from "../leave/WorkedDateChips";
// Extracted from AdminDashboard.jsx ("timelapse" tab) for lazy-loading.
// Shared theme (C)/state/setters passed as props (AdminDashboard keeps a
// module-level mutable theme `C`); API_BASE comes from the shared config module.
export default function TimelapseTab({
  C, T, isDark, Icon, card, employeesAlpha, token,
  tlDate, setTlDate, tlEmpId, setTlEmpId,
  tlShots, setTlShots, tlLoading, setTlLoading,
  tlLightbox, setTlLightbox, tlDelShot, setTlDelShot, tlDelVideo, setTlDelVideo,
  tlVideoUrl, setTlVideoUrl, tlVideoMeta, setTlVideoMeta,
  tlVideoLoading, setTlVideoLoading, tlVideoError, setTlVideoError,
}) {
              // Holds the server filename for the current video so Delete still
              // works after we swap tlVideoUrl to a blob: URL (blob URLs carry no
              // filename). See generateVideo/deleteVideo below.
              const tlFilenameRef = React.useRef(null);
              const loadShots = async () => {
                if (!token) return;
                setTlLoading(true);
                setTlVideoUrl(null);
                setTlVideoError("");
                try {
                  const params = new URLSearchParams({ date: tlDate, sort: "oldest", limit: "500" });
                  if (tlEmpId) params.append("user_id", tlEmpId);
                  const r = await fetch(`${API_BASE}/api/screenshots?${params}`, { headers: { Authorization: `Bearer ${token}` } });
                  const d = r.ok ? await r.json() : [];
                  setTlShots(Array.isArray(d) ? d : []);
                } catch { setTlShots([]); } finally { setTlLoading(false); }
              };
              // Opened from a deep link (?user=&date=, e.g. a comp-leave worked
              // date): the employee and day are already selected, so load once
              // instead of making the reviewer press Load.
              React.useEffect(() => {
                if (readPlaybackDeepLink(window.location.search)) loadShots();
                // eslint-disable-next-line react-hooks/exhaustive-deps
              }, []);

              const generateVideo = async () => {
                if (!tlEmpId) { setTlVideoError("Select a specific employee to generate timelapse"); return; }
                setTlVideoLoading(true);
                setTlVideoError("");
                setTlVideoMeta(null);
                try {
                  const params = new URLSearchParams({ user_id: tlEmpId, date: tlDate });
                  const r = await fetch(`${API_BASE}/api/screenshots/timelapse?${params}`, {
                    method: "POST",
                    headers: { Authorization: `Bearer ${token}` },
                  });
                  if (!r.ok) {
                    const err = await r.json().catch(() => ({}));
                    throw new Error(err.detail || `Generation failed (${r.status})`);
                  }
                  const d = await r.json();
                  // A native <video src> tag cannot send the Authorization header,
                  // but the stream endpoint requires auth. So fetch the MP4 WITH the
                  // Bearer token and play it as a blob (same approach as AuthImage).
                  const vres = await fetch(`${API_BASE}${d.video_url}`, {
                    headers: { Authorization: `Bearer ${token}` },
                  });
                  if (!vres.ok) throw new Error(`Video fetch failed (${vres.status})`);
                  const blob = await vres.blob();
                  if (tlVideoUrl && tlVideoUrl.startsWith("blob:")) URL.revokeObjectURL(tlVideoUrl);
                  tlFilenameRef.current = d.filename;
                  setTlVideoUrl(URL.createObjectURL(blob));
                  setTlVideoMeta({ frame_count: d.frame_count, duration_seconds: d.duration_seconds, size_bytes: d.size_bytes, fps: d.fps, skipped: d.skipped });
                } catch (e) { setTlVideoError(e.message); } finally { setTlVideoLoading(false); }
              };

              const deleteVideo = async () => {
                if (!tlVideoUrl) return;
                // tlVideoUrl is now a blob: URL, so use the stored server filename.
                const filename = tlFilenameRef.current || tlVideoUrl.split("/").pop();
                try {
                  await fetch(`${API_BASE}/api/screenshots/timelapse/${filename}`, {
                    method: "DELETE", headers: { Authorization: `Bearer ${token}` },
                  });
                } catch {}
                if (tlVideoUrl.startsWith("blob:")) URL.revokeObjectURL(tlVideoUrl);
                tlFilenameRef.current = null;
                setTlVideoUrl(null);
                setTlVideoMeta(null);
                setTlDelVideo(false);
              };

              const deleteShot = async (id) => {
                try {
                  await fetch(`${API_BASE}/api/screenshots/${id}`, {
                    method: "DELETE", headers: { Authorization: `Bearer ${token}` },
                  });
                  setTlShots(prev => prev.filter(s => s.id !== id));
                } catch {}
                setTlDelShot(null);
              };

              const advanceLightbox = (dir) => {
                if (tlLightbox === null) return;
                const next = tlLightbox + dir;
                if (next >= 0 && next < tlShots.length) setTlLightbox(next);
              };

              return (
                <div style={{ display: "flex", flexDirection: "column", gap: 16, maxWidth: 1200, animation: "fadeIn 0.2s" }}>

                  {/* ── Filter bar ── */}
                  <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                    <h2 style={{ fontSize: 18, fontWeight: 800, color: C.text }}>{T.timePlayback || "Time Playback"}</h2>
                    <div style={{ marginLeft: "auto", display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
                      <select value={tlEmpId} onChange={e => { setTlEmpId(e.target.value); setTlVideoUrl(null); setTlVideoError(""); }}
                        style={{ padding: "8px 12px", borderRadius: 8, border: `1px solid ${C.border}`, background: C.surface, color: C.text, fontSize: 13, outline: "none", fontFamily: "inherit", colorScheme: isDark ? "dark" : "light" }}>
                        <option value="">All employees</option>
                        {employeesAlpha.map(e => <option key={e.id} value={e.id}>{e.name}</option>)}
                      </select>
                      <input type="date" value={tlDate} onChange={e => { setTlDate(e.target.value); setTlVideoUrl(null); setTlVideoError(""); }}
                        style={{ padding: "8px 12px", borderRadius: 8, border: `1px solid ${C.border}`, background: C.surface, color: C.text, fontSize: 13, outline: "none", fontFamily: "inherit", colorScheme: isDark ? "dark" : "light" }} />
                      <button onClick={loadShots} disabled={tlLoading}
                        style={{ padding: "8px 18px", borderRadius: 8, border: "none", background: C.brand, color: "#fff", fontSize: 13, fontWeight: 700, cursor: tlLoading ? "default" : "pointer", opacity: tlLoading ? 0.7 : 1, fontFamily: "inherit" }}>
                        {tlLoading ? "Loading…" : "Load"}
                      </button>
                    </div>
                  </div>

                  {/* ── Loading ── */}
                  {tlLoading && (
                    <div style={{ ...card(), padding: 40, textAlign: "center" }}>
                      <p style={{ color: C.textSub, fontSize: 13 }}>Loading screenshots…</p>
                    </div>
                  )}

                  {/* ── Empty state ── */}
                  {!tlLoading && tlShots.length === 0 && (
                    <div style={{ ...card(), padding: "60px 40px", textAlign: "center" }}>
                      <Icon n="camera" s={28} c={C.textSub} />
                      <p style={{ fontSize: 14, fontWeight: 700, color: C.text, marginTop: 14, marginBottom: 6 }}>No screenshots found</p>
                      <p style={{ fontSize: 13, color: C.textSub }}>Select an employee and date, then click Load to view the timeline.</p>
                    </div>
                  )}

                  {/* ── Loaded content ── */}
                  {!tlLoading && tlShots.length > 0 && (
                    <>
                      {/* Stats + timelapse action bar */}
                      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
                        <div>
                          <p style={{ fontSize: 13, color: C.textSub, margin: 0 }}>
                            {tlShots.length} screenshot{tlShots.length !== 1 ? "s" : ""} · {tlDate}
                          </p>
                          {/* estimated duration hint shown before generation */}
                          {!tlVideoUrl && !tlVideoLoading && tlEmpId && (() => {
                            const estFps = tlShots.length <= 15 ? 0.5 : Math.max(1, Math.min(15, tlShots.length / 25));
                            const estDur = Math.round(tlShots.length / estFps);
                            return <p style={{ fontSize: 11, color: C.textSub, margin: "2px 0 0", fontStyle: "italic" }}>Est. video length ≈ {estDur}s at {estFps.toFixed(1)} fps</p>;
                          })()}
                        </div>
                        <div style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                          {tlVideoError && (
                            <span style={{ fontSize: 12, color: "#DC2626" }}>{tlVideoError}</span>
                          )}
                          {!tlVideoUrl && !tlVideoLoading && tlEmpId && (
                            <button onClick={generateVideo}
                              style={{ display: "flex", alignItems: "center", gap: 6, padding: "8px 18px", borderRadius: 8, border: "none", background: "#7C3AED", color: "#fff", fontSize: 13, fontWeight: 700, cursor: "pointer", fontFamily: "inherit" }}>
                              <Icon n="timelapse" s={14} c="#fff" /> Generate Timelapse
                            </button>
                          )}
                          {!tlVideoUrl && !tlVideoLoading && !tlEmpId && (
                            <span style={{ fontSize: 12, color: C.textSub, fontStyle: "italic" }}>Select an employee to generate timelapse</span>
                          )}
                          {tlVideoLoading && (
                            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                              <span style={{ fontSize: 12, color: "#7C3AED", fontWeight: 600 }}>Encoding {tlShots.length} frames…</span>
                              <span style={{ fontSize: 11, color: C.textSub }}>this may take 10–30 s</span>
                            </div>
                          )}
                          {tlVideoUrl && !tlVideoLoading && (
                            <button onClick={generateVideo}
                              style={{ padding: "7px 14px", borderRadius: 8, border: `1px solid ${C.border}`, background: "transparent", color: C.text, fontSize: 12, fontWeight: 600, cursor: "pointer", fontFamily: "inherit" }}>
                              ↺ Regenerate
                            </button>
                          )}
                          {tlVideoUrl && !tlVideoLoading && (
                            <button onClick={() => setTlDelVideo(true)}
                              style={{ padding: "7px 14px", borderRadius: 8, border: `1px solid #DC2626`, background: "transparent", color: "#DC2626", fontSize: 12, fontWeight: 600, cursor: "pointer", fontFamily: "inherit" }}>
                              Delete Video
                            </button>
                          )}
                        </div>
                      </div>

                      {/* Video player */}
                      {tlVideoUrl && (
                        <div style={{ ...card(), padding: 16, background: isDark ? "#0f0f17" : "#f0f4ff" }}>
                          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10, flexWrap: "wrap", gap: 6 }}>
                            <p style={{ fontSize: 11, fontWeight: 700, color: C.textSub, textTransform: "uppercase", letterSpacing: 1, margin: 0 }}>Timelapse Preview</p>
                            {tlVideoMeta && (
                              <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
                                {[
                                  `${tlVideoMeta.frame_count} frames`,
                                  `${tlVideoMeta.duration_seconds}s`,
                                  `${tlVideoMeta.fps} fps`,
                                  `${(tlVideoMeta.size_bytes / 1024).toFixed(0)} KB`,
                                  tlVideoMeta.skipped > 0 ? `${tlVideoMeta.skipped} skipped` : null,
                                ].filter(Boolean).map(label => (
                                  <span key={label} style={{ fontSize: 11, color: C.textSub, background: isDark ? "rgba(255,255,255,0.07)" : "rgba(0,0,0,0.06)", padding: "2px 8px", borderRadius: 20 }}>{label}</span>
                                ))}
                              </div>
                            )}
                          </div>
                          <video controls muted loop src={tlVideoUrl}
                            disablePictureInPicture controlsList="nodownload nofullscreen noremoteplayback"
                            onError={() => setTlVideoError("Video failed to load — try Regenerate")}
                            style={{ width: "100%", maxHeight: 440, borderRadius: 8, background: "#000", display: "block", outline: "none" }}
                          />
                        </div>
                      )}

                      {/* Screenshot grid */}
                      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(200px,1fr))", gap: 12 }}>
                        {tlShots.map((ss, i) => {
                          const imgSrc = ss.file_url ? `${API_BASE}${ss.file_url}` : `${API_BASE}/api/screenshots/${ss.id}/image`;
                          const tsDate = ss.timestamp ? new Date(ss.timestamp) : null;
                          const ts = tsDate
                            ? tsDate.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: true, timeZone: getOrgTimeZone() })
                            : "—";
                          return (
                            <div key={ss.id} style={{ ...card(), overflow: "hidden", position: "relative", transition: "transform .15s, box-shadow .15s" }}
                              onMouseEnter={e => { e.currentTarget.style.transform = "translateY(-2px)"; e.currentTarget.style.boxShadow = "0 8px 24px rgba(0,0,0,0.12)"; }}
                              onMouseLeave={e => { e.currentTarget.style.transform = ""; e.currentTarget.style.boxShadow = ""; }}>
                              <div style={{ cursor: "pointer", position: "relative" }} onClick={() => setTlLightbox(i)}>
                                <AuthImage url={imgSrc} token={token} alt="" style={{ width: "100%", height: 120, objectFit: "cover", display: "block" }} />
                                {/* frame index badge */}
                                <span style={{ position: "absolute", top: 6, left: 6, background: "rgba(0,0,0,0.55)", color: "#fff", fontSize: 9, fontWeight: 700, padding: "2px 6px", borderRadius: 4 }}>
                                  #{i + 1}
                                </span>
                              </div>
                              <div style={{ padding: "8px 10px", display: "flex", alignItems: "center", justifyContent: "space-between", gap: 4 }}>
                                <div style={{ minWidth: 0 }}>
                                  <p style={{ fontSize: 12, fontWeight: 700, color: C.text, margin: 0, letterSpacing: "-0.2px" }}>{ts}</p>
                                  <p style={{ fontSize: 10, color: C.textSub, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", margin: 0 }}>{ss.employee_name || ss.app_name || "—"}</p>
                                </div>
                                <button onClick={() => setTlDelShot(ss.id)}
                                  style={{ flexShrink: 0, padding: "3px 8px", borderRadius: 5, border: `1px solid ${C.border}`, background: "transparent", color: "#DC2626", fontSize: 10, fontWeight: 700, cursor: "pointer", fontFamily: "inherit" }}>
                                  ✕
                                </button>
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    </>
                  )}

                  {/* ── Lightbox ── */}
                  {tlLightbox !== null && tlShots[tlLightbox] && (
                    <div onClick={() => setTlLightbox(null)} style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.88)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 9999, padding: 20 }}>
                      <button onClick={e => { e.stopPropagation(); advanceLightbox(-1); }} disabled={tlLightbox === 0}
                        style={{ position: "absolute", left: 24, top: "50%", transform: "translateY(-50%)", width: 44, height: 44, borderRadius: "50%", border: "1px solid rgba(255,255,255,0.3)", background: "rgba(0,0,0,0.5)", color: "#fff", cursor: "pointer", fontSize: 18, display: "flex", alignItems: "center", justifyContent: "center", opacity: tlLightbox === 0 ? 0.3 : 1 }}>‹</button>
                      <div onClick={e => e.stopPropagation()} style={{ maxWidth: "80vw", maxHeight: "80vh", borderRadius: 12, overflow: "hidden" }}>
                        <AuthImage url={tlShots[tlLightbox].file_url ? `${API_BASE}${tlShots[tlLightbox].file_url}` : `${API_BASE}/api/screenshots/${tlShots[tlLightbox].id}/image`} token={token} eager alt=""
                          style={{ maxWidth: "100%", maxHeight: "70vh", objectFit: "contain", display: "block" }} />
                        <div style={{ background: "#1a1a2e", padding: "10px 16px", color: "#fff" }}>
                          <p style={{ fontSize: 13, fontWeight: 600 }}>
                            {tlShots[tlLightbox].employee_name || "Employee"} · {tlShots[tlLightbox].timestamp ? new Date(tlShots[tlLightbox].timestamp).toLocaleString("en-IN", { timeZone: getOrgTimeZone(), hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: true, day: "numeric", month: "short" }) : "—"}
                          </p>
                          <p style={{ fontSize: 11, opacity: 0.6, marginTop: 2 }}>Frame {tlLightbox + 1} of {tlShots.length} · {tlShots[tlLightbox].app_name || ""}</p>
                        </div>
                      </div>
                      <button onClick={e => { e.stopPropagation(); advanceLightbox(1); }} disabled={tlLightbox === tlShots.length - 1}
                        style={{ position: "absolute", right: 24, top: "50%", transform: "translateY(-50%)", width: 44, height: 44, borderRadius: "50%", border: "1px solid rgba(255,255,255,0.3)", background: "rgba(0,0,0,0.5)", color: "#fff", cursor: "pointer", fontSize: 18, display: "flex", alignItems: "center", justifyContent: "center", opacity: tlLightbox === tlShots.length - 1 ? 0.3 : 1 }}>›</button>
                    </div>
                  )}

                  {/* ── Delete screenshot confirm ── */}
                  {tlDelShot !== null && (
                    <div onClick={() => setTlDelShot(null)} style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.7)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 9999 }}>
                      <div onClick={e => e.stopPropagation()} style={{ ...card(), padding: 28, maxWidth: 380, width: "90%", borderRadius: 14 }}>
                        <p style={{ fontSize: 15, fontWeight: 700, color: C.text, marginBottom: 8 }}>Delete Screenshot?</p>
                        <p style={{ fontSize: 13, color: C.textSub, marginBottom: 20 }}>This action cannot be undone. The screenshot will be permanently removed.</p>
                        <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
                          <button onClick={() => setTlDelShot(null)} style={{ padding: "9px 18px", borderRadius: 8, border: `1px solid ${C.border}`, background: "transparent", color: C.text, fontSize: 13, cursor: "pointer", fontFamily: "inherit" }}>Cancel</button>
                          <button onClick={() => deleteShot(tlDelShot)} style={{ padding: "9px 18px", borderRadius: 8, border: "none", background: "#DC2626", color: "#fff", fontSize: 13, fontWeight: 700, cursor: "pointer", fontFamily: "inherit" }}>Delete</button>
                        </div>
                      </div>
                    </div>
                  )}

                  {/* ── Delete video confirm ── */}
                  {tlDelVideo && (
                    <div onClick={() => setTlDelVideo(false)} style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.7)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 9999 }}>
                      <div onClick={e => e.stopPropagation()} style={{ ...card(), padding: 28, maxWidth: 380, width: "90%", borderRadius: 14 }}>
                        <p style={{ fontSize: 15, fontWeight: 700, color: C.text, marginBottom: 8 }}>Delete Timelapse Video?</p>
                        <p style={{ fontSize: 13, color: C.textSub, marginBottom: 20 }}>The generated MP4 will be permanently removed. You can regenerate it at any time.</p>
                        <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
                          <button onClick={() => setTlDelVideo(false)} style={{ padding: "9px 18px", borderRadius: 8, border: `1px solid ${C.border}`, background: "transparent", color: C.text, fontSize: 13, cursor: "pointer", fontFamily: "inherit" }}>Cancel</button>
                          <button onClick={deleteVideo} style={{ padding: "9px 18px", borderRadius: 8, border: "none", background: "#DC2626", color: "#fff", fontSize: 13, fontWeight: 700, cursor: "pointer", fontFamily: "inherit" }}>Delete</button>
                        </div>
                      </div>
                    </div>
                  )}

                </div>
              );
}

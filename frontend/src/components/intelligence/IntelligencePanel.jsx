/**
 * IntelligencePanel.jsx — Workforce Intelligence cinematic view.
 *
 * Mounts inside the admin EmpDetailPanel as the "Intelligence" tab and
 * consumes /api/intelligence/* endpoints to render:
 *
 *   • AISummaryCard       — Claude-generated daily narrative + headline
 *   • LiveActivityCard    — current live browser tab (glass card)
 *   • WorkflowRibbon      — reconstructed workflow sequences
 *   • FocusTimeline       — deep-focus blocks + idle gaps along a 24h axis
 *   • BurnoutMeter        — explainable burnout signals + 0..1 gauge
 *   • ContextSwitchPulse  — hourly tab-switch heatmap
 *
 * All visuals are theme-aware — they read `C` + `isDark` from useTheme()
 * so navy/emerald + DM Sans match the rest of the dashboard. Glassmorphism
 * adapts: light = white/65% + navy hairline; dark = midnight/65% + brand glow.
 */
import React, { useEffect, useMemo, useState } from "react";
import { API_BASE } from "../../config";
import { useTheme } from "../../context/ThemeContext";

// ─── shared glass + chip primitives ──────────────────────────────────
function useGlassStyle() {
  const { C, isDark } = useTheme();
  return useMemo(() => ({
    base: {
      position: "relative",
      borderRadius: 16,
      padding: "18px 20px",
      background: isDark
        ? "linear-gradient(135deg, rgba(26,31,46,0.78) 0%, rgba(17,21,32,0.78) 100%)"
        : "linear-gradient(135deg, rgba(255,255,255,0.86) 0%, rgba(240,247,253,0.82) 100%)",
      backdropFilter: "blur(20px) saturate(140%)",
      WebkitBackdropFilter: "blur(20px) saturate(140%)",
      border: isDark
        ? "1px solid rgba(68,185,123,0.18)"
        : "1px solid rgba(17,73,106,0.10)",
      boxShadow: isDark
        ? "0 1px 0 rgba(255,255,255,0.04) inset, 0 14px 40px rgba(0,0,0,0.32)"
        : "0 1px 0 rgba(255,255,255,0.9) inset, 0 14px 36px rgba(17,73,106,0.10)",
      overflow: "hidden",
    },
    accent: {
      base: C.brandAccent || "#44B97B",
      ring: isDark
        ? "0 0 0 1px rgba(68,185,123,0.35), 0 0 24px rgba(68,185,123,0.18)"
        : "0 0 0 1px rgba(68,185,123,0.30), 0 0 18px rgba(68,185,123,0.15)",
    },
    headerLabel: {
      fontSize: 10,
      letterSpacing: "1.4px",
      textTransform: "uppercase",
      color: isDark ? "rgba(255,255,255,0.55)" : "rgba(17,73,106,0.7)",
      fontWeight: 700,
    },
    gradientText: {
      backgroundImage: "linear-gradient(135deg,#11496A 0%,#44B97B 100%)",
      WebkitBackgroundClip: "text",
      backgroundClip: "text",
      color: "transparent",
    },
  }), [C, isDark]);
}

const fmtHM = (totalMin) => {
  totalMin = Math.max(0, Math.round(totalMin || 0));
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return h ? `${h}h ${m}m` : `${m}m`;
};
const fmtSecs = (s) => fmtHM(Math.round((s || 0) / 60));
const fmtTime = (iso) => {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch { return "—"; }
};
const catColor = (cat, C) => {
  if (cat === "work")        return { fg: "#1f8a4c", bg: "rgba(68,185,123,0.14)", glow: "rgba(68,185,123,0.35)" };
  if (cat === "neutral")     return { fg: "#7c5cd6", bg: "rgba(124,92,214,0.12)", glow: "rgba(124,92,214,0.30)" };
  if (cat === "distraction") return { fg: "#d6675c", bg: "rgba(214,103,92,0.14)",  glow: "rgba(214,103,92,0.30)" };
  return                          { fg: C.textSub, bg: C.bg, glow: "rgba(0,0,0,0.10)" };
};
const burnoutColor = (band) => {
  if (band === "high")     return { fg: "#d6675c", bg: "rgba(214,103,92,0.14)", glow: "rgba(214,103,92,0.4)", label: "High" };
  if (band === "moderate") return { fg: "#d6a55c", bg: "rgba(214,165,92,0.14)", glow: "rgba(214,165,92,0.4)", label: "Moderate" };
  return                          { fg: "#44B97B", bg: "rgba(68,185,123,0.14)", glow: "rgba(68,185,123,0.4)", label: "Low" };
};

// ─── AI Summary ──────────────────────────────────────────────────────
function AISummaryCard({ data, loading, onRefresh, isDark }) {
  const glass = useGlassStyle();
  const { C } = useTheme();
  if (loading) {
    return (
      <div style={glass.base}>
        <div style={glass.headerLabel}>AI Daily Summary</div>
        <div style={{ marginTop: 10, fontSize: 13, color: C.textSub }}>Composing the day's narrative…</div>
      </div>
    );
  }
  if (!data || !data.narrative) {
    return (
      <div style={glass.base}>
        <div style={glass.headerLabel}>AI Daily Summary</div>
        <div style={{ marginTop: 10, fontSize: 13, color: C.textSub }}>
          No activity recorded for this date yet.
        </div>
      </div>
    );
  }
  return (
    <div style={glass.base}>
      <div style={{ position: "absolute", inset: 0, opacity: 0.5, background:
        "radial-gradient(80% 60% at 100% 0%, rgba(68,185,123,0.16) 0%, rgba(68,185,123,0) 60%)" }} />
      <div style={{ position: "relative" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
          <div>
            <div style={glass.headerLabel}>AI Daily Summary</div>
            <div style={{ ...glass.gradientText, fontWeight: 800, fontSize: 22, marginTop: 6, lineHeight: 1.2 }}>
              {data.headline || "Today's work shape"}
            </div>
          </div>
          <button onClick={onRefresh} title="Recompute"
            style={{
              border: "1px solid rgba(68,185,123,0.4)",
              background: "rgba(68,185,123,0.08)",
              color: "#44B97B", padding: "5px 10px", borderRadius: 8,
              fontSize: 10, fontWeight: 800, letterSpacing: "0.6px",
              cursor: "pointer", textTransform: "uppercase",
            }}>{data.generator === "claude" ? "AI · Claude" : "AI · Rules"}</button>
        </div>
        <p style={{
          marginTop: 14, fontSize: 14, lineHeight: 1.55,
          color: isDark ? "rgba(255,255,255,0.88)" : "rgba(17,73,106,0.92)",
        }}>{data.narrative}</p>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: 10, marginTop: 16 }}>
          <Metric label="Productive" value={`${Math.round(data.productivity_pct || 0)}%`} />
          <Metric label="Deep focus" value={fmtHM(data.deep_work_min || 0)} />
          <Metric label="Switches"  value={(data.context_switches || 0).toLocaleString()} />
          <Metric label="Burnout"   value={burnoutColor(data.burnout_risk).label}
                  color={burnoutColor(data.burnout_risk).fg} />
        </div>
      </div>
    </div>
  );
}
function Metric({ label, value, color }) {
  const { C, isDark } = useTheme();
  return (
    <div style={{
      borderRadius: 10, padding: "10px 12px",
      background: isDark ? "rgba(255,255,255,0.04)" : "rgba(17,73,106,0.04)",
      border: isDark ? "1px solid rgba(255,255,255,0.06)" : "1px solid rgba(17,73,106,0.07)",
    }}>
      <div style={{ fontSize: 9, fontWeight: 700, letterSpacing: "0.8px",
                    color: isDark ? "rgba(255,255,255,0.50)" : "rgba(17,73,106,0.55)", textTransform: "uppercase" }}>{label}</div>
      <div style={{ marginTop: 4, fontSize: 18, fontWeight: 800, color: color || C.text, fontVariantNumeric: "tabular-nums" }}>{value}</div>
    </div>
  );
}

// ─── Live Activity Card ──────────────────────────────────────────────
function LiveActivityCard({ live, isDark }) {
  const glass = useGlassStyle();
  const { C } = useTheme();
  if (!live) {
    return (
      <div style={glass.base}>
        <div style={glass.headerLabel}>Live Activity</div>
        <div style={{ marginTop: 10, fontSize: 13, color: C.textSub }}>
          No active browser tab right now.
        </div>
      </div>
    );
  }
  const cc = catColor(live.category, C);
  return (
    <div style={{ ...glass.base, boxShadow: `${glass.base.boxShadow}, 0 0 0 1px ${cc.glow}` }}>
      <div style={glass.headerLabel}>
        <span style={{ display: "inline-block", width: 8, height: 8, borderRadius: "50%",
          background: cc.fg, boxShadow: `0 0 10px ${cc.glow}`, marginRight: 8,
          animation: "ztPulse 1.6s ease-in-out infinite" }} />
        Live · on tab for {fmtSecs(live.on_tab_sec || 0)}
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 10 }}>
        {live.favicon_url
          ? <img src={live.favicon_url} alt="" width={28} height={28}
                 style={{ borderRadius: 6, background: isDark ? "rgba(255,255,255,0.06)" : "rgba(17,73,106,0.04)" }}
                 onError={(e) => { e.currentTarget.style.display = "none"; }} />
          : <div style={{ width: 28, height: 28, borderRadius: 6, background: cc.bg }} />}
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ fontSize: 14, fontWeight: 700, color: C.text, overflow: "hidden",
                        textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {live.tab_title || live.domain || "Unknown tab"}
          </div>
          <div style={{ fontSize: 11, color: C.textSub, marginTop: 2 }}>
            {live.domain || "—"} · {live.browser || "browser"}
          </div>
        </div>
        <span style={{
          fontSize: 9, fontWeight: 800, letterSpacing: "0.6px", textTransform: "uppercase",
          padding: "4px 9px", borderRadius: 99, color: cc.fg, background: cc.bg,
          boxShadow: `0 0 12px ${cc.glow}`,
        }}>{(live.category || "neutral")}</span>
      </div>
    </div>
  );
}

// ─── Workflow Ribbon ─────────────────────────────────────────────────
function WorkflowRibbon({ data, isDark }) {
  const glass = useGlassStyle();
  const { C } = useTheme();
  const workflows = data?.workflows || [];
  return (
    <div style={glass.base}>
      <div style={glass.headerLabel}>Reconstructed Workflows · {workflows.length}</div>
      {workflows.length === 0 && (
        <div style={{ marginTop: 10, fontSize: 13, color: C.textSub }}>
          No multi-tab workflows detected for this date — either light activity, or all single-domain.
        </div>
      )}
      <div style={{ display: "flex", flexDirection: "column", gap: 12, marginTop: 12 }}>
        {workflows.slice(0, 5).map((w, i) => (
          <WorkflowItem key={i} w={w} isDark={isDark} />
        ))}
        {workflows.length > 5 && (
          <div style={{ fontSize: 11, color: C.textSub, textAlign: "center" }}>
            +{workflows.length - 5} more workflow{workflows.length - 5 === 1 ? "" : "s"}
          </div>
        )}
      </div>
    </div>
  );
}
function WorkflowItem({ w, isDark }) {
  const { C } = useTheme();
  const total = w.duration_sec || 1;
  return (
    <div style={{
      borderRadius: 12, padding: "12px 14px",
      background: isDark ? "rgba(255,255,255,0.025)" : "rgba(17,73,106,0.03)",
      border: isDark ? "1px solid rgba(255,255,255,0.06)" : "1px solid rgba(17,73,106,0.07)",
    }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{
            fontSize: 10, fontWeight: 800, letterSpacing: "0.5px", textTransform: "uppercase",
            padding: "3px 8px", borderRadius: 99,
            backgroundImage: "linear-gradient(135deg,#11496A,#44B97B)",
            color: "#fff",
          }}>{w.label}</span>
          <span style={{ fontSize: 11, color: C.textSub }}>{w.hops} hops · {fmtHM(total / 60)}</span>
        </div>
        <span style={{ fontSize: 10, color: C.textSub, fontVariantNumeric: "tabular-nums" }}>
          {fmtTime(w.started_at)} → {fmtTime(w.ended_at)}
        </span>
      </div>
      <div style={{ display: "flex", height: 10, borderRadius: 6, overflow: "hidden", marginBottom: 8 }}>
        {w.segments.map((s, i) => {
          const cc = catColor(s.category, C);
          const pct = Math.max(2, ((s.duration_sec || 0) / total) * 100);
          return <div key={i} title={`${s.domain} · ${fmtSecs(s.duration_sec)}`}
            style={{ width: `${pct}%`, background: cc.fg, opacity: 0.85,
                     borderRight: i < w.segments.length - 1
                       ? "2px solid rgba(255,255,255,0.18)" : "none" }} />;
        })}
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center", fontSize: 11, color: C.text }}>
        {w.segments.slice(0, 8).map((s, i) => (
          <span key={i} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
            {s.favicon_url && <img src={s.favicon_url} width={12} height={12} alt="" onError={(e) => { e.currentTarget.style.display = "none"; }} />}
            <span style={{ fontWeight: 600 }}>{s.domain}</span>
            {i < Math.min(w.segments.length, 8) - 1 && (
              <span style={{ color: C.textSub, margin: "0 4px" }}>→</span>
            )}
          </span>
        ))}
        {w.segments.length > 8 && (
          <span style={{ color: C.textSub }}>+{w.segments.length - 8}</span>
        )}
      </div>
    </div>
  );
}

// ─── Focus Timeline (24-hour band) ───────────────────────────────────
function FocusTimeline({ data, switches, isDark }) {
  const glass = useGlassStyle();
  const { C } = useTheme();
  const blocks = data?.blocks || [];

  // Build a 24h band: each minute → category color of any block covering it.
  const minutesInDay = 24 * 60;
  const lane = useMemo(() => {
    const arr = new Array(minutesInDay).fill(null);
    blocks.forEach((b) => {
      const start = new Date(b.started_at);
      const end = new Date(b.ended_at);
      const sm = start.getHours() * 60 + start.getMinutes();
      const em = Math.min(minutesInDay, end.getHours() * 60 + end.getMinutes());
      for (let m = sm; m < em; m++) {
        arr[m] = { category: b.category, depth: b.depth_score, is_deep: b.is_deep };
      }
    });
    return arr;
  }, [blocks]);

  const hourly = switches?.hourly || new Array(24).fill(0);
  const maxHourly = Math.max(1, ...hourly);

  return (
    <div style={glass.base}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
        <div style={glass.headerLabel}>Focus Timeline</div>
        <div style={{ display: "flex", gap: 12, fontSize: 11, color: C.textSub }}>
          <span>Deep · {fmtHM(data?.deep_focus_min || 0)}</span>
          <span>Total · {fmtHM(data?.total_focus_min || 0)}</span>
        </div>
      </div>

      {/* Focus band */}
      <div style={{ position: "relative", height: 22, borderRadius: 6, overflow: "hidden",
                    background: isDark ? "rgba(255,255,255,0.04)" : "rgba(17,73,106,0.05)" }}>
        {lane.map((cell, m) => {
          if (!cell) return null;
          const cc = catColor(cell.category, C);
          const left = (m / minutesInDay) * 100;
          const w = (1 / minutesInDay) * 100;
          const opacity = cell.is_deep ? 1 : 0.55;
          return <div key={m} title={cell.category}
            style={{ position: "absolute", left: `${left}%`, top: 0, bottom: 0,
                     width: `${w + 0.05}%`, background: cc.fg, opacity }} />;
        })}
      </div>

      {/* Context-switch pulse beneath */}
      <div style={{ marginTop: 10 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 4 }}>
          <span style={glass.headerLabel}>Context-switch pulse</span>
          {switches?.overload && (
            <span style={{
              fontSize: 9, fontWeight: 800, letterSpacing: "0.6px", textTransform: "uppercase",
              padding: "2px 7px", borderRadius: 99, color: "#d6675c",
              background: "rgba(214,103,92,0.14)",
              boxShadow: "0 0 10px rgba(214,103,92,0.30)",
            }}>Overload</span>
          )}
        </div>
        <div style={{ display: "flex", alignItems: "flex-end", height: 36, gap: 1 }}>
          {hourly.map((v, h) => (
            <div key={h} title={`${h}:00 — ${v} switches`}
              style={{
                flex: 1,
                height: `${Math.max(4, (v / maxHourly) * 100)}%`,
                background: v > 0
                  ? "linear-gradient(to top, #11496A, #44B97B)"
                  : (isDark ? "rgba(255,255,255,0.04)" : "rgba(17,73,106,0.05)"),
                borderRadius: "3px 3px 0 0",
                opacity: v > 0 ? 0.9 : 1,
              }} />
          ))}
        </div>
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 9, color: C.textSub, marginTop: 4 }}>
          <span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>23:59</span>
        </div>
      </div>
    </div>
  );
}

// ─── Burnout Meter ───────────────────────────────────────────────────
function BurnoutMeter({ data, isDark }) {
  const glass = useGlassStyle();
  const { C } = useTheme();
  if (!data) {
    return (
      <div style={glass.base}>
        <div style={glass.headerLabel}>Burnout Signals</div>
        <div style={{ marginTop: 10, fontSize: 13, color: C.textSub }}>Computing…</div>
      </div>
    );
  }
  const band = burnoutColor(data.risk_band || "low");
  const pct = Math.round((data.risk_score || 0) * 100);

  return (
    <div style={glass.base}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
        <div style={glass.headerLabel}>Burnout Signals · last {data.period_days || 7}d</div>
        <span style={{
          fontSize: 10, fontWeight: 800, letterSpacing: "0.6px", textTransform: "uppercase",
          padding: "3px 9px", borderRadius: 99, color: band.fg, background: band.bg,
          boxShadow: `0 0 14px ${band.glow}`,
        }}>{band.label} risk</span>
      </div>

      {/* Gauge */}
      <div style={{ position: "relative", height: 10, borderRadius: 99,
                    background: isDark ? "rgba(255,255,255,0.06)" : "rgba(17,73,106,0.06)", overflow: "hidden" }}>
        <div style={{
          position: "absolute", left: 0, top: 0, bottom: 0,
          width: `${pct}%`,
          background: `linear-gradient(90deg, #44B97B 0%, #d6a55c 60%, #d6675c 100%)`,
          boxShadow: `0 0 14px ${band.glow}`,
          transition: "width .6s ease",
        }} />
      </div>
      <div style={{ marginTop: 6, fontSize: 11, color: C.textSub, fontVariantNumeric: "tabular-nums",
                    display: "flex", justifyContent: "space-between" }}>
        <span>0</span><span style={{ fontWeight: 800, color: band.fg }}>{pct}</span><span>100</span>
      </div>

      {/* Signal list */}
      <div style={{ marginTop: 14, display: "flex", flexDirection: "column", gap: 8 }}>
        {(data.signals || []).length === 0 && (
          <div style={{ fontSize: 12, color: C.textSub }}>
            No elevated burnout signals in this window — healthy pace.
          </div>
        )}
        {(data.signals || []).map((s, i) => (
          <div key={i} style={{
            display: "flex", alignItems: "center", gap: 10,
            padding: "10px 12px", borderRadius: 10,
            background: isDark ? "rgba(255,255,255,0.03)" : "rgba(17,73,106,0.03)",
            border: isDark ? "1px solid rgba(255,255,255,0.05)" : "1px solid rgba(17,73,106,0.07)",
          }}>
            <span style={{ width: 32, height: 32, borderRadius: 8, flexShrink: 0,
              display: "grid", placeItems: "center", fontWeight: 800, fontSize: 11,
              color: band.fg, background: band.bg, boxShadow: `0 0 10px ${band.glow}` }}>
              {Math.round((s.severity || 0) * 100)}
            </span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: C.text }}>
                {s.kind.replace(/_/g, " ")}
              </div>
              <div style={{ fontSize: 11, color: C.textSub, marginTop: 2 }}>{s.reason}</div>
            </div>
          </div>
        ))}
      </div>

      {/* Metric strip */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 8, marginTop: 14 }}>
        <Metric label="Clocked"      value={`${data.metrics?.total_clocked_hours ?? 0}h`} />
        <Metric label="Late-night"   value={`${Math.round((data.metrics?.late_night_minutes || 0) / 60 * 10) / 10}h`} />
        <Metric label="Weekend"      value={`${data.metrics?.weekend_hours ?? 0}h`} />
      </div>
    </div>
  );
}

// ─── Main Panel ──────────────────────────────────────────────────────
export default function IntelligencePanel({ emp, date }) {
  const { C, isDark } = useTheme();
  const [summary,  setSummary]  = useState(null);
  const [workflows, setWorkflows] = useState(null);
  const [focus,    setFocus]    = useState(null);
  const [burnout,  setBurnout]  = useState(null);
  const [switches, setSwitches] = useState(null);
  const [live,     setLive]     = useState(null);
  const [loading,  setLoading]  = useState(false);

  const userId = emp?.id;
  const dateStr = date;

  async function fetchAll(force = false) {
    if (!userId) return;
    const tok = localStorage.getItem("token") || localStorage.getItem("zt_token");
    if (!tok) return;
    const headers = { Authorization: `Bearer ${tok}` };
    const qs = `user_id=${userId}&date=${encodeURIComponent(dateStr)}`;
    setLoading(true);
    try {
      const [s, w, f, b, sw, lv] = await Promise.all([
        fetch(`${API_BASE}/api/intelligence/daily-summary?${qs}${force ? "&force=true" : ""}`, { headers }).then(r => r.ok ? r.json() : null),
        fetch(`${API_BASE}/api/intelligence/workflows?${qs}`, { headers }).then(r => r.ok ? r.json() : null),
        fetch(`${API_BASE}/api/intelligence/focus-sessions?${qs}`, { headers }).then(r => r.ok ? r.json() : null),
        fetch(`${API_BASE}/api/intelligence/burnout?user_id=${userId}&period_days=7`, { headers }).then(r => r.ok ? r.json() : null),
        fetch(`${API_BASE}/api/intelligence/context-switches?${qs}`, { headers }).then(r => r.ok ? r.json() : null),
        fetch(`${API_BASE}/browser/live`, { headers }).then(r => r.ok ? r.json() : []),
      ]);
      setSummary(s);
      setWorkflows(w);
      setFocus(f);
      setBurnout(b);
      setSwitches(sw);
      setLive(Array.isArray(lv) ? lv.find(x => x.user_id === userId) || null : null);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { fetchAll(false); /* eslint-disable-next-line */ }, [userId, dateStr]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {/* keyframes for the live pulse dot */}
      <style>{`
        @keyframes ztPulse {
          0%, 100% { transform: scale(1);   box-shadow: 0 0 0 0 rgba(68,185,123,0.55); }
          50%      { transform: scale(1.4); box-shadow: 0 0 0 6px rgba(68,185,123,0); }
        }
      `}</style>

      <LiveActivityCard live={live} isDark={isDark} />

      <WorkflowRibbon data={workflows} isDark={isDark} />

      <BurnoutMeter data={burnout} isDark={isDark} />
    </div>
  );
}

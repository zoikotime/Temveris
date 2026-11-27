/**
 * Access Governance Center — Workspace Settings → Roles & Permissions.
 *
 * Implements the Roles & Permissions design as specced. Every section is
 * live-wired to the backend; nothing in this component is static.
 *
 *   • GET    /api/workspace/access-governance/overview        — top stat cards
 *   • GET    /api/workspace/roles                             — role library + permission catalog
 *   • GET    /api/workspace/access-governance/assignments     — user × role list (Assignments tab)
 *   • POST   /api/workspace/access-governance/assignments/bulk — bulk reassign
 *   • POST   /api/workspace/roles                             — create custom role
 *   • DELETE /api/workspace/roles/{code}                      — remove custom role
 *   • POST   /api/workspace/roles/{role}/users                — single user assignment
 *   • GET    /api/workspace/access-governance/mfa-controls    — MFA enforcement tab
 *   • PUT    /api/workspace/access-governance/mfa-controls    — save MFA enforcement
 *   • GET    /api/workspace/access-governance/audit-trail     — audit trail tab
 */
import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { API_BASE } from "../../config";
import { useTheme } from "../../context/ThemeContext";
import { wsGet, wsSend } from "./_api";

const token = () => localStorage.getItem("zt_token") || localStorage.getItem("token");

// ── Visual tokens ─────────────────────────────────────────────────────────────
// All palette swatches derive from the active theme via useTheme().C — so the
// whole module looks correct in both light and dark mode without dual paths.

const TIER_STYLE = (C, isDark) => ({
  owner:      { bg: isDark ? "#0D2A1A" : "#ECFDF5", fg: isDark ? "#5FCC93" : "#047857", bd: isDark ? "#1F5A3D" : "#A7F3D0" },
  high:       { bg: isDark ? "#3D2A0A" : "#FFEDD5", fg: isDark ? "#FBBF24" : "#9A3412", bd: isDark ? "#92400E" : "#FDBA74" },
  moderate:   { bg: isDark ? "#1F2937" : "#F1F5F9", fg: isDark ? "#CBD5E1" : "#334155", bd: isDark ? "#374151" : "#CBD5E1" },
  scoped:     { bg: isDark ? "#0E2B3D" : "#EFF6FF", fg: isDark ? "#7DD3FC" : "#1D4ED8", bd: isDark ? "#1F3D55" : "#BFDBFE" },
  read_heavy: { bg: isDark ? "#2D1B69" : "#F4F0FF", fg: isDark ? "#C4B5FD" : "#6B21A8", bd: isDark ? "#4C1D95" : "#DDD6FE" },
});

const MFA_STYLE = (C, isDark) => ({
  required:    { fg: isDark ? "#FCA5A5" : "#B42318", icon: "🔒" },
  enforced:    { fg: isDark ? "#FBBF24" : "#B54708", icon: "🛡" },
  recommended: { fg: isDark ? "#5FCC93" : "#047857", icon: "🛡" },
  optional:    { fg: isDark ? "#A0ADB9" : "#475467", icon: "○" },
});

const STAT_ACCENT = (C, isDark) => ({
  privileged_access: isDark ? "#5FCC93" : "#10B981",
  mfa_coverage:      isDark ? "#FBBF24" : "#F59E0B",
  custom_roles:      isDark ? "#86EFAC" : "#10B981",
});

const card = (C) => ({
  background: C.surface,
  border: `1px solid ${C.border}`,
  borderRadius: 12,
  boxSizing: "border-box",
});

const btnPrimary = (C) => ({
  padding: "10px 18px",
  borderRadius: 8,
  border: "none",
  background: C.brand,
  color: "#fff",
  fontWeight: 700,
  fontSize: 13,
  cursor: "pointer",
  fontFamily: "inherit",
});

const btnGhost = (C) => ({
  padding: "10px 18px",
  borderRadius: 8,
  border: `1px solid ${C.border}`,
  background: C.surface,
  color: C.text,
  fontWeight: 600,
  fontSize: 13,
  cursor: "pointer",
  fontFamily: "inherit",
});

// ── Tier + MFA badges ─────────────────────────────────────────────────────────
function TierBadge({ tier, label, C, isDark }) {
  const palette = TIER_STYLE(C, isDark)[tier] || TIER_STYLE(C, isDark).moderate;
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", padding: "4px 10px",
      borderRadius: 999, fontSize: 11, fontWeight: 700, letterSpacing: 0.2,
      background: palette.bg, color: palette.fg, border: `1px solid ${palette.bd}`,
      whiteSpace: "nowrap",
    }}>{label}</span>
  );
}

function MfaTag({ tier, label, C, isDark }) {
  const palette = MFA_STYLE(C, isDark)[tier] || MFA_STYLE(C, isDark).optional;
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 4,
      fontSize: 11, fontWeight: 700, color: palette.fg, whiteSpace: "nowrap",
    }}>
      <span aria-hidden="true">{palette.icon}</span>
      {label}
    </span>
  );
}

// ── Stat card ─────────────────────────────────────────────────────────────────
function StatCard({ accent, label, value, caption, suffix, C }) {
  return (
    <div style={{
      ...card(C),
      padding: "16px 18px",
      borderTop: `3px solid ${accent}`,
      flex: 1, minWidth: 0,
    }}>
      <p style={{
        margin: 0, fontSize: 11, color: C.textSub, fontWeight: 700,
        textTransform: "uppercase", letterSpacing: 0.5,
      }}>{label}</p>
      <p style={{
        margin: "6px 0 4px",
        fontSize: 30, fontWeight: 900, color: C.text,
        letterSpacing: "-0.5px",
        display: "flex", alignItems: "baseline", gap: 4,
      }}>
        {value}
        {suffix && <span style={{ fontSize: 18, color: C.textSub, fontWeight: 700 }}>{suffix}</span>}
      </p>
      <p style={{ margin: 0, fontSize: 12, color: C.textSub, lineHeight: 1.4 }}>{caption}</p>
    </div>
  );
}

// ── Tab strip ─────────────────────────────────────────────────────────────────
const TABS = [
  { k: "role_library",      label: "Role library" },
  { k: "assignments",       label: "Assignments" },
  { k: "permission_matrix", label: "Permission matrix" },
  { k: "mfa_controls",      label: "MFA & access controls" },
  { k: "audit_trail",       label: "Audit trail" },
];

function TabStrip({ active, onChange, C }) {
  return (
    <div style={{
      display: "flex", gap: 4, borderBottom: `1px solid ${C.border}`,
      marginBottom: 18, overflowX: "auto",
    }}>
      {TABS.map(t => {
        const on = active === t.k;
        return (
          <button key={t.k} onClick={() => onChange(t.k)}
            style={{
              padding: "12px 18px", border: "none", background: "transparent",
              color: on ? C.brand : C.textSub,
              fontWeight: on ? 700 : 600, fontSize: 13.5,
              cursor: "pointer", fontFamily: "inherit",
              borderBottom: on ? `2px solid ${C.brand}` : "2px solid transparent",
              marginBottom: -1,
              whiteSpace: "nowrap",
            }}>
            {t.label}
          </button>
        );
      })}
    </div>
  );
}

// ── Role library card ─────────────────────────────────────────────────────────
function RoleCard({ role, C, isDark, isAdmin, onAssign, onDelete }) {
  const userCount = role.user_count || 0;
  return (
    <div style={{
      ...card(C), padding: "18px 20px",
      display: "flex", flexDirection: "column", gap: 10,
      transition: "transform .15s ease, box-shadow .15s ease",
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 10 }}>
        <h3 style={{ margin: 0, color: C.text, fontSize: 15, fontWeight: 700, lineHeight: 1.3 }}>
          {role.label}
        </h3>
        <TierBadge tier={role.tier} label={role.tier_label} C={C} isDark={isDark} />
      </div>
      <p style={{
        margin: 0, color: C.textSub, fontSize: 12.5, lineHeight: 1.5,
        minHeight: 56,
      }}>{role.description}</p>

      <div style={{
        display: "flex", justifyContent: "space-between", alignItems: "center",
        paddingTop: 12, borderTop: `1px solid ${C.borderLight}`, gap: 8,
      }}>
        <button
          onClick={() => onAssign(role)}
          disabled={!isAdmin}
          style={{
            background: "transparent", border: "none", padding: 0,
            color: C.textSub, fontSize: 12, fontWeight: 600,
            cursor: isAdmin ? "pointer" : "default", fontFamily: "inherit",
            textAlign: "left",
          }}
          title={isAdmin ? "Manage users on this role" : ""}>
          {userCount} {userCount === 1 ? "user" : "users"}
        </button>
        <MfaTag tier={role.mfa_tier} label={role.mfa_tier_label} C={C} isDark={isDark} />
      </div>

      {!role.is_system && isAdmin && (
        <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 2 }}>
          <button onClick={() => onDelete(role)}
            style={{
              background: "transparent", border: "none", padding: 0,
              color: C.danger || "#B42318", fontSize: 11, fontWeight: 600,
              cursor: "pointer", fontFamily: "inherit",
            }}>
            Delete custom role
          </button>
        </div>
      )}
    </div>
  );
}

// ── Modal shell ───────────────────────────────────────────────────────────────
function Modal({ title, onClose, children, width = 560, C }) {
  return (
    <div onClick={onClose} style={{
      position: "fixed", inset: 0, zIndex: 9000,
      background: "rgba(15,23,42,0.55)",
      display: "flex", alignItems: "center", justifyContent: "center",
      padding: 16,
    }}>
      <div onClick={e => e.stopPropagation()} style={{
        ...card(C), width, maxWidth: "100%", maxHeight: "90vh", overflowY: "auto",
        boxShadow: "0 22px 60px rgba(0,0,0,0.35)", padding: 24,
      }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
          <h3 style={{ margin: 0, color: C.text, fontSize: 17, fontWeight: 700 }}>{title}</h3>
          <button onClick={onClose} style={{
            background: "transparent", border: "none", color: C.textSub,
            fontSize: 22, cursor: "pointer", lineHeight: 1, fontFamily: "inherit",
          }}>×</button>
        </div>
        {children}
      </div>
    </div>
  );
}

// ── Top-level component ───────────────────────────────────────────────────────
export default function AccessGovernanceCenter({ isAdmin, showToast }) {
  const { C, isDark } = useTheme();

  const [overview, setOverview] = useState(null);
  const [roles, setRoles]       = useState([]);
  const [permissions, setPermissions] = useState([]);
  const [users, setUsers]       = useState([]);
  const [tab, setTab]           = useState("role_library");
  const [loading, setLoading]   = useState(true);

  // ── Permission-matrix top scrollbar ─────────────────────────────────────
  // The matrix is much wider than the viewport (one column per role + the
  // sticky Permission column). The native scrollbar lives at the BOTTOM of
  // the table, which is invisible when the matrix has many rows — the admin
  // has to scroll the whole page down just to reach the horizontal scroll.
  //
  // To fix this we render a synced scroll proxy ABOVE the table and hide the
  // native bottom scrollbar. The proxy's inner spacer is sized to match the
  // table's scrollWidth, kept in sync via ResizeObserver, and bidirectional
  // scrollLeft syncing lets the user drag from either end without feedback
  // loops (browsers don't fire scroll if scrollLeft is set to its current
  // value).
  const matrixTopScrollRef    = useRef(null);
  const matrixBottomScrollRef = useRef(null);
  const matrixTableRef        = useRef(null);
  const [matrixInnerWidth, setMatrixInnerWidth] = useState(0);

  useLayoutEffect(() => {
    if (tab !== "permission_matrix") return;
    const el = matrixTableRef.current;
    if (!el) return;
    const update = () => setMatrixInnerWidth(el.scrollWidth);
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, [tab, roles.length, permissions.length]);

  const onMatrixTopScroll = () => {
    const t = matrixTopScrollRef.current, b = matrixBottomScrollRef.current;
    if (t && b && b.scrollLeft !== t.scrollLeft) b.scrollLeft = t.scrollLeft;
  };
  const onMatrixBottomScroll = () => {
    const t = matrixTopScrollRef.current, b = matrixBottomScrollRef.current;
    if (t && b && t.scrollLeft !== b.scrollLeft) t.scrollLeft = b.scrollLeft;
  };

  // Modals
  const [showCreate, setShowCreate]   = useState(false);
  const [createDraft, setCreateDraft] = useState({ label: "", description: "", permissions: [], tier: "moderate", mfa_tier: "recommended" });
  const [assignTarget, setAssignTarget] = useState(null); // role being assigned, or null
  const [assignUserId, setAssignUserId] = useState("");
  const [showInvite,  setShowInvite]  = useState(false);

  // Assignments tab
  const [assignments, setAssignments] = useState([]);
  const [assignmentsLoading, setAssignmentsLoading] = useState(false);
  const [assignmentsSearch, setAssignmentsSearch]   = useState("");
  const [assignmentsRoleFilter, setAssignmentsRoleFilter] = useState("");
  const [bulkSelected, setBulkSelected] = useState(() => new Set());
  const [bulkRole, setBulkRole]         = useState("");

  // MFA controls tab
  const [mfaCfg, setMfaCfg] = useState(null);
  const [mfaSaving, setMfaSaving] = useState(false);
  // Monotonic sequence so a slow response from an earlier save can't clobber
  // newer optimistic state when the admin clicks several pills in quick succession.
  const mfaSaveSeqRef = useRef(0);

  // Audit trail tab
  const [auditEvents, setAuditEvents] = useState([]);
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditWindow, setAuditWindow]   = useState(720); // 30 days

  // ── Loaders ───────────────────────────────────────────────────────────────
  const loadCore = async () => {
    setLoading(true);
    try {
      const [ov, rolesData, userRes] = await Promise.all([
        wsGet("/api/workspace/access-governance/overview").catch(() => null),
        wsGet("/api/workspace/roles"),
        fetch(`${API_BASE}/employees`, { headers: { Authorization: `Bearer ${token()}` } }).then(r => r.ok ? r.json() : []),
      ]);
      setOverview(ov);
      setRoles(rolesData.roles || []);
      setPermissions(rolesData.permissions || []);
      setUsers(Array.isArray(userRes) ? userRes : []);
    } catch (e) {
      showToast?.(e?.message || "Failed to load access governance data", "error");
    } finally {
      setLoading(false);
    }
  };

  const loadAssignments = async () => {
    setAssignmentsLoading(true);
    try {
      const params = new URLSearchParams();
      if (assignmentsSearch.trim()) params.set("search", assignmentsSearch.trim());
      if (assignmentsRoleFilter)    params.set("role_filter", assignmentsRoleFilter);
      const data = await wsGet(`/api/workspace/access-governance/assignments?${params.toString()}`);
      setAssignments(data.items || []);
    } catch (e) {
      showToast?.(e?.message || "Failed to load assignments", "error");
    } finally {
      setAssignmentsLoading(false);
    }
  };

  const loadMfa = async () => {
    try {
      const data = await wsGet("/api/workspace/access-governance/mfa-controls");
      setMfaCfg(data);
    } catch (e) {
      showToast?.(e?.message || "Failed to load MFA controls", "error");
    }
  };

  const loadAudit = async () => {
    setAuditLoading(true);
    try {
      const data = await wsGet(`/api/workspace/access-governance/audit-trail?since_hours=${auditWindow}`);
      setAuditEvents(data.events || []);
    } catch (e) {
      showToast?.(e?.message || "Failed to load audit trail", "error");
    } finally {
      setAuditLoading(false);
    }
  };

  useEffect(() => { loadCore(); }, []);
  useEffect(() => {
    if (tab === "assignments") loadAssignments();
    if (tab === "mfa_controls" && !mfaCfg) loadMfa();
    if (tab === "audit_trail") loadAudit();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab]);

  // Debounced refresh when the assignment filters change.
  useEffect(() => {
    if (tab !== "assignments") return;
    const id = setTimeout(loadAssignments, 250);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [assignmentsSearch, assignmentsRoleFilter]);

  // ── Actions ───────────────────────────────────────────────────────────────
  const createRole = async () => {
    const label = createDraft.label.trim();
    if (!label) return showToast?.("Role name is required", "error");
    try {
      await wsSend("POST", "/api/workspace/roles", createDraft);
      setShowCreate(false);
      setCreateDraft({ label: "", description: "", permissions: [], tier: "moderate", mfa_tier: "recommended" });
      showToast?.("Custom role created");
      loadCore();
    } catch (e) {
      showToast?.(e?.message || "Failed to create role", "error");
    }
  };

  const deleteRole = async (role) => {
    if (!window.confirm(`Delete the "${role.label}" custom role? Users on this role must be reassigned first.`)) return;
    try {
      await fetch(`${API_BASE}/api/workspace/roles/${encodeURIComponent(role.code)}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token()}` },
      }).then(async r => {
        if (!r.ok) {
          const body = await r.json().catch(() => ({}));
          throw new Error(body.detail || `Delete failed (${r.status})`);
        }
      });
      showToast?.("Custom role deleted");
      loadCore();
    } catch (e) {
      showToast?.(e?.message || "Failed to delete role", "error");
    }
  };

  const assignSingle = async () => {
    if (!assignTarget || !assignUserId) return showToast?.("Pick a user to assign", "error");
    try {
      await wsSend("POST", `/api/workspace/roles/${encodeURIComponent(assignTarget.code)}/users`, { user_id: Number(assignUserId) });
      showToast?.(`Assigned to ${assignTarget.label}`);
      setAssignTarget(null);
      setAssignUserId("");
      loadCore();
      if (tab === "assignments") loadAssignments();
    } catch (e) {
      showToast?.(e?.message || "Failed to assign role", "error");
    }
  };

  const bulkAssign = async () => {
    if (bulkSelected.size === 0) return showToast?.("Select users to reassign", "error");
    if (!bulkRole) return showToast?.("Choose a destination role", "error");
    try {
      const r = await wsSend("POST", "/api/workspace/access-governance/assignments/bulk", {
        user_ids: Array.from(bulkSelected), role: bulkRole,
      });
      showToast?.(`${r.count || bulkSelected.size} users reassigned`);
      setBulkSelected(new Set());
      setBulkRole("");
      loadCore();
      loadAssignments();
    } catch (e) {
      showToast?.(e?.message || "Bulk reassign failed", "error");
    }
  };

  const saveMfa = async (patch) => {
    // Optimistic merge — flip the UI immediately so rapid clicks feel instant
    // and don't get blocked waiting on a 1-2s round-trip.
    setMfaCfg(prev => (prev ? { ...prev, ...patch } : prev));
    const mySeq = ++mfaSaveSeqRef.current;
    setMfaSaving(true);
    try {
      const next = await wsSend("PUT", "/api/workspace/access-governance/mfa-controls", patch);
      // Only reconcile with the server if no newer save has started; otherwise
      // the in-flight response would overwrite the user's later choice.
      if (mySeq === mfaSaveSeqRef.current) setMfaCfg(next);
    } catch (e) {
      showToast?.(e?.message || "Failed to save MFA controls", "error");
      // Roll back optimistic state to whatever the server actually has.
      if (mySeq === mfaSaveSeqRef.current) loadMfa();
    } finally {
      if (mySeq === mfaSaveSeqRef.current) setMfaSaving(false);
    }
  };

  // ── Derived ───────────────────────────────────────────────────────────────
  const builtinRoles = useMemo(() => roles.filter(r => r.is_system), [roles]);
  const customRoles  = useMemo(() => roles.filter(r => !r.is_system), [roles]);
  const stat         = overview || {};
  const accent       = STAT_ACCENT(C, isDark);

  // ── Render ────────────────────────────────────────────────────────────────
  if (loading) {
    return (
      <div style={{ padding: "48px 0", textAlign: "center", color: C.textSub }}>
        Loading Access Governance Center…
      </div>
    );
  }

  return (
    <div style={{ padding: "28px 36px 60px", maxWidth: 1240 }}>

      {/* ─── Header ─────────────────────────────────────────────────── */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16, marginBottom: 22, flexWrap: "wrap" }}>
        <div style={{ display: "flex", gap: 14, alignItems: "flex-start" }}>
          <div style={{
            width: 44, height: 44, borderRadius: 12,
            background: isDark ? "#0D2A1A" : "#E8F7EE",
            display: "flex", alignItems: "center", justifyContent: "center",
            color: isDark ? "#5FCC93" : "#10B981",
            fontSize: 22, fontWeight: 800, flexShrink: 0,
          }}>🛡</div>
          <div style={{ minWidth: 0 }}>
            <h2 style={{ margin: 0, fontSize: 22, fontWeight: 800, color: C.text }}>Access Governance Center</h2>
            <p style={{ margin: "4px 0 0", fontSize: 13, color: C.textSub, lineHeight: 1.5, maxWidth: 620 }}>
              Control role-based access, scoped authority, MFA requirements, and
              audit-ready administration across your workspace.
            </p>
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button onClick={() => setTab("audit_trail")} style={btnGhost(C)}>View Audit Log</button>
          <button onClick={() => setShowInvite(true)} style={btnGhost(C)} disabled={!isAdmin}>Invite user</button>
          <button onClick={() => setShowCreate(true)} style={btnGhost(C)} disabled={!isAdmin}>Create custom role</button>
          <button onClick={() => setTab("assignments")} style={btnPrimary(C)} disabled={!isAdmin}>Assign role</button>
        </div>
      </div>

      {/* ─── Stat cards ─────────────────────────────────────────────── */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 14, marginBottom: 22 }}>
        <StatCard C={C}
          accent={accent.privileged_access}
          label={stat.privileged_access?.label || "Privileged access"}
          value={stat.privileged_access?.value ?? "—"}
          caption={stat.privileged_access?.caption || ""} />
        <StatCard C={C}
          accent={accent.mfa_coverage}
          label={stat.mfa_coverage?.label || "MFA coverage"}
          value={stat.mfa_coverage?.value ?? "—"}
          suffix={stat.mfa_coverage ? "%" : ""}
          caption={stat.mfa_coverage?.caption || ""} />
        <StatCard C={C}
          accent={accent.custom_roles}
          label={stat.custom_roles?.label || "Custom roles"}
          value={stat.custom_roles?.value ?? customRoles.length}
          caption={stat.custom_roles?.caption || (customRoles.length ? "active" : "none configured")} />
      </div>

      {/* ─── Tabs ───────────────────────────────────────────────────── */}
      <TabStrip active={tab} onChange={setTab} C={C} />

      {/* ─── Role library ───────────────────────────────────────────── */}
      {tab === "role_library" && (
        <>
          <p style={{
            margin: "0 0 12px", fontSize: 11, color: C.textLight, fontWeight: 700,
            textTransform: "uppercase", letterSpacing: 0.6,
          }}>Built-in roles</p>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))", gap: 16, marginBottom: 26 }}>
            {builtinRoles.map(r => (
              <RoleCard key={r.code} role={r} C={C} isDark={isDark} isAdmin={isAdmin}
                onAssign={(role) => setAssignTarget(role)}
                onDelete={deleteRole} />
            ))}
          </div>

          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
            <p style={{
              margin: 0, fontSize: 11, color: C.textLight, fontWeight: 700,
              textTransform: "uppercase", letterSpacing: 0.6,
            }}>Custom roles</p>
            {isAdmin && (
              <button onClick={() => setShowCreate(true)} style={btnGhost(C)}>+ New custom role</button>
            )}
          </div>
          {customRoles.length === 0 ? (
            <div style={{
              ...card(C), padding: 28, textAlign: "center", color: C.textSub, fontSize: 13,
            }}>
              No custom roles yet. Click <strong>Create custom role</strong> to define one with a
              tailored permission set and MFA stance.
            </div>
          ) : (
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))", gap: 16 }}>
              {customRoles.map(r => (
                <RoleCard key={r.code} role={r} C={C} isDark={isDark} isAdmin={isAdmin}
                  onAssign={(role) => setAssignTarget(role)}
                  onDelete={deleteRole} />
              ))}
            </div>
          )}
        </>
      )}

      {/* ─── Assignments ────────────────────────────────────────────── */}
      {tab === "assignments" && (
        <div style={{ ...card(C), padding: 0, overflow: "hidden" }}>
          <div style={{ padding: "14px 18px", borderBottom: `1px solid ${C.border}`, display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
            <input
              placeholder="Search by name or email…"
              value={assignmentsSearch}
              onChange={e => setAssignmentsSearch(e.target.value)}
              style={{
                flex: 1, minWidth: 220,
                padding: "9px 12px", borderRadius: 8,
                border: `1px solid ${C.border}`, background: C.surface, color: C.text,
                fontSize: 13, fontFamily: "inherit", outline: "none",
              }} />
            <select value={assignmentsRoleFilter}
              onChange={e => setAssignmentsRoleFilter(e.target.value)}
              style={{
                padding: "9px 12px", borderRadius: 8,
                border: `1px solid ${C.border}`, background: C.surface, color: C.text,
                fontSize: 13, fontFamily: "inherit", minWidth: 200,
              }}>
              <option value="">All roles</option>
              {roles.map(r => <option key={r.code} value={r.code}>{r.label}</option>)}
            </select>
            {bulkSelected.size > 0 && isAdmin && (
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <select value={bulkRole} onChange={e => setBulkRole(e.target.value)}
                  style={{
                    padding: "9px 12px", borderRadius: 8,
                    border: `1px solid ${C.border}`, background: C.surface, color: C.text,
                    fontSize: 13, fontFamily: "inherit", minWidth: 200,
                  }}>
                  <option value="">Reassign {bulkSelected.size} selected to…</option>
                  {roles.map(r => <option key={r.code} value={r.code}>{r.label}</option>)}
                </select>
                <button onClick={bulkAssign} style={btnPrimary(C)} disabled={!bulkRole}>Apply</button>
                <button onClick={() => setBulkSelected(new Set())} style={btnGhost(C)}>Clear</button>
              </div>
            )}
          </div>
          {assignmentsLoading ? (
            <div style={{ padding: 28, textAlign: "center", color: C.textSub }}>Loading…</div>
          ) : assignments.length === 0 ? (
            <div style={{ padding: 28, textAlign: "center", color: C.textSub }}>No users match this filter.</div>
          ) : (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr style={{ background: isDark ? C.surfaceAlt : "#F9FAFB" }}>
                  <th style={th(C)} width="36">
                    <input type="checkbox"
                      checked={bulkSelected.size > 0 && bulkSelected.size === assignments.length}
                      onChange={e => {
                        if (e.target.checked) setBulkSelected(new Set(assignments.map(a => a.user_id)));
                        else setBulkSelected(new Set());
                      }}
                      disabled={!isAdmin} />
                  </th>
                  <th style={th(C)}>User</th>
                  <th style={th(C)}>Role</th>
                  <th style={th(C)}>Tier</th>
                  <th style={th(C)}>MFA</th>
                  <th style={th(C)}>Last login</th>
                </tr>
              </thead>
              <tbody>
                {assignments.map(u => (
                  <tr key={u.user_id} style={{ borderTop: `1px solid ${C.borderLight}` }}>
                    <td style={td(C)}>
                      <input type="checkbox"
                        checked={bulkSelected.has(u.user_id)}
                        onChange={e => {
                          const next = new Set(bulkSelected);
                          if (e.target.checked) next.add(u.user_id);
                          else next.delete(u.user_id);
                          setBulkSelected(next);
                        }}
                        disabled={!isAdmin} />
                    </td>
                    <td style={td(C)}>
                      <p style={{ margin: 0, color: C.text, fontWeight: 700 }}>{u.name}</p>
                      <p style={{ margin: "2px 0 0", color: C.textSub, fontSize: 12 }}>{u.email}</p>
                    </td>
                    <td style={td(C)}><span style={{ color: C.text, fontWeight: 600 }}>{u.role_label}</span></td>
                    <td style={td(C)}>
                      <TierBadge tier={u.tier} label={(TIER_STYLE(C, isDark)[u.tier] ? (u.tier === "owner" ? "Owner" : u.tier === "high" ? "High" : u.tier === "scoped" ? "Scoped" : u.tier === "read_heavy" ? "Read-heavy" : "Moderate") : u.tier)} C={C} isDark={isDark} />
                    </td>
                    <td style={td(C)}>
                      {u.mfa_enrolled ? (
                        <span style={{ color: isDark ? "#5FCC93" : "#047857", fontWeight: 700, fontSize: 12 }}>🔒 Enrolled</span>
                      ) : u.mfa_required ? (
                        <span style={{ color: isDark ? "#FCA5A5" : "#B42318", fontWeight: 700, fontSize: 12 }}>⚠ Missing</span>
                      ) : (
                        <span style={{ color: C.textSub, fontWeight: 600, fontSize: 12 }}>—</span>
                      )}
                    </td>
                    <td style={{ ...td(C), color: C.textSub }}>
                      {u.last_login_at ? new Date(u.last_login_at).toLocaleString() : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* ─── Permission matrix ──────────────────────────────────────── */}
      {tab === "permission_matrix" && (
        <div style={{ ...card(C), padding: 0, overflow: "hidden" }}>
          {/* Scoped CSS: hide the native scrollbar on the table container so
              the ONLY visible horizontal scrollbar is the one we render at
              the top. The bottom container stays scrollable via wheel/touch
              and is still kept in sync with the top scrollbar via JS. */}
          <style>{`
            .perm-matrix-scroll::-webkit-scrollbar { height: 0; width: 0; display: none; }
            .perm-matrix-scroll { -ms-overflow-style: none; scrollbar-width: none; }
          `}</style>
          {/* Top scrollbar — proxy with a spacer sized to the table's
              scrollWidth so the thumb covers the full virtual range. */}
          <div
            ref={matrixTopScrollRef}
            onScroll={onMatrixTopScroll}
            style={{
              overflowX: "auto", overflowY: "hidden",
              borderBottom: `1px solid ${C.borderLight}`,
            }}
          >
            <div style={{ width: matrixInnerWidth || "100%", height: 1 }} />
          </div>
          {/* Table — native scrollbar hidden, scroll is driven by the
              top proxy (or by wheel/touch on the table itself). */}
          <div
            ref={matrixBottomScrollRef}
            onScroll={onMatrixBottomScroll}
            className="perm-matrix-scroll"
            style={{ overflowX: "auto", overflowY: "hidden" }}
          >
            <table ref={matrixTableRef} style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
              <thead>
                <tr style={{ background: isDark ? C.surfaceAlt : "#F9FAFB" }}>
                  <th style={{ ...th(C), position: "sticky", left: 0, background: isDark ? C.surfaceAlt : "#F9FAFB", minWidth: 220 }}>Permission</th>
                  {roles.map(r => (
                    <th key={r.code} style={{ ...th(C), textAlign: "center", minWidth: 110 }}>
                      <div style={{ fontWeight: 700, color: C.text, fontSize: 12 }}>{r.label}</div>
                      <div style={{ marginTop: 4 }}><TierBadge tier={r.tier} label={r.tier_label} C={C} isDark={isDark} /></div>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {permissions.map(p => (
                  <tr key={p.key} style={{ borderTop: `1px solid ${C.borderLight}` }}>
                    <td style={{ ...td(C), position: "sticky", left: 0, background: C.surface, borderRight: `1px solid ${C.borderLight}` }}>
                      <p style={{ margin: 0, color: C.text, fontWeight: 700, fontSize: 12.5 }}>{p.key}</p>
                      <p style={{ margin: "2px 0 0", color: C.textSub, fontSize: 11.5, lineHeight: 1.4 }}>{p.description}</p>
                    </td>
                    {roles.map(r => {
                      const has = (r.permissions || []).includes(p.key);
                      return (
                        <td key={r.code} style={{ ...td(C), textAlign: "center" }}>
                          {has ? (
                            <span style={{ color: isDark ? "#5FCC93" : "#10B981", fontSize: 16, fontWeight: 800 }}>✓</span>
                          ) : (
                            <span style={{ color: C.textLight, fontSize: 14 }}>—</span>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* ─── MFA & access controls ──────────────────────────────────── */}
      {tab === "mfa_controls" && mfaCfg && (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <div style={{ ...card(C), padding: "18px 20px" }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
              <h3 style={{ margin: 0, fontSize: 15, fontWeight: 700, color: C.text }}>MFA enforcement by tier</h3>
              {mfaSaving && (
                <span style={{ fontSize: 11.5, fontWeight: 600, color: C.textSub, letterSpacing: 0.2 }}>
                  Saving…
                </span>
              )}
            </div>
            <p style={{ margin: "4px 0 14px", fontSize: 12.5, color: C.textSub }}>
              Users on roles classified into any of the selected tiers must enroll a
              verified MFA factor before logging in.
            </p>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
              {mfaCfg.tiers.map(t => {
                const active = (mfaCfg.required_tiers || []).includes(t.code);
                return (
                  <button key={t.code}
                    onClick={() => {
                      if (!isAdmin) return;
                      const cur = new Set(mfaCfg.required_tiers || []);
                      if (cur.has(t.code)) cur.delete(t.code); else cur.add(t.code);
                      // saveMfa applies the optimistic merge itself.
                      saveMfa({ required_tiers: Array.from(cur) });
                    }}
                    disabled={!isAdmin}
                    style={{
                      padding: "8px 14px", borderRadius: 999,
                      border: `1px solid ${active ? C.brand : C.border}`,
                      background: active ? C.brandBg : C.surface,
                      color: active ? C.brand : C.text,
                      fontWeight: 700, fontSize: 12.5, cursor: isAdmin ? "pointer" : "default",
                      fontFamily: "inherit",
                      transition: "background .12s ease, border-color .12s ease, color .12s ease",
                    }}>
                    {t.label}
                  </button>
                );
              })}
            </div>
          </div>

          <div style={{ ...card(C), padding: "18px 20px", display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
            <div>
              <label style={{ fontSize: 12.5, fontWeight: 700, color: C.textSub, display: "block", marginBottom: 6 }}>
                Enrollment grace period (days)
              </label>
              <input type="number" min={0} max={90}
                value={mfaCfg.enforce_grace_days ?? ""}
                onChange={e => {
                  // Keep "" as "" so the field can be cleared mid-edit without
                  // snapping back to 0; clamp/parse happens on blur.
                  const raw = e.target.value;
                  setMfaCfg({ ...mfaCfg, enforce_grace_days: raw === "" ? "" : parseInt(raw, 10) });
                }}
                onBlur={e => {
                  // Read from the event target so we save what the user actually
                  // typed even if React hasn't flushed the latest onChange yet.
                  const raw = e.target.value;
                  const days = Math.max(0, Math.min(90, parseInt(raw || "0", 10) || 0));
                  setMfaCfg(prev => prev ? { ...prev, enforce_grace_days: days } : prev);
                  // Skip the round-trip when nothing actually changed.
                  if (days === Number(mfaCfg.enforce_grace_days)) return;
                  saveMfa({ enforce_grace_days: days });
                }}
                disabled={!isAdmin}
                style={{
                  width: "100%", padding: "9px 12px", borderRadius: 8,
                  border: `1px solid ${C.border}`, background: C.surface, color: C.text,
                  fontSize: 13, fontFamily: "inherit",
                }} />
              <p style={{ margin: "4px 0 0", fontSize: 11, color: C.textLight }}>
                Days a new user has to enroll before access is blocked.
              </p>
            </div>
            <div>
              <label style={{ fontSize: 12.5, fontWeight: 700, color: C.textSub, display: "block", marginBottom: 6 }}>
                Allow admin-granted MFA exceptions
              </label>
              <button
                onClick={() => isAdmin && saveMfa({ allow_exceptions: !mfaCfg.allow_exceptions })}
                disabled={!isAdmin}
                style={{
                  display: "inline-flex", alignItems: "center", gap: 10,
                  padding: "8px 14px", borderRadius: 8,
                  border: `1px solid ${C.border}`, background: C.surface,
                  color: C.text, fontWeight: 600, fontSize: 13,
                  cursor: isAdmin ? "pointer" : "default", fontFamily: "inherit",
                }}>
                <span style={{
                  width: 36, height: 20, borderRadius: 999,
                  background: mfaCfg.allow_exceptions ? C.brand : C.textLight,
                  position: "relative", flexShrink: 0,
                }}>
                  <span style={{
                    position: "absolute", top: 2,
                    left: mfaCfg.allow_exceptions ? 18 : 2,
                    width: 16, height: 16, borderRadius: "50%", background: "#fff",
                    transition: "left .16s ease",
                  }} />
                </span>
                {mfaCfg.allow_exceptions ? "Exceptions allowed" : "Strict — no exceptions"}
              </button>
              <p style={{ margin: "4px 0 0", fontSize: 11, color: C.textLight }}>
                Every exception is recorded in the audit trail.
              </p>
            </div>
          </div>
        </div>
      )}

      {/* ─── Audit trail ────────────────────────────────────────────── */}
      {tab === "audit_trail" && (
        <div style={{ ...card(C), padding: 0, overflow: "hidden" }}>
          <div style={{ padding: "14px 18px", borderBottom: `1px solid ${C.border}`, display: "flex", justifyContent: "space-between", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
            <div style={{ color: C.text, fontWeight: 700, fontSize: 14 }}>Role & MFA audit events</div>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <select value={auditWindow} onChange={e => { setAuditWindow(Number(e.target.value)); setTimeout(loadAudit, 0); }}
                style={{
                  padding: "8px 12px", borderRadius: 8,
                  border: `1px solid ${C.border}`, background: C.surface, color: C.text,
                  fontSize: 13, fontFamily: "inherit",
                }}>
                <option value={24}>Last 24 hours</option>
                <option value={168}>Last 7 days</option>
                <option value={720}>Last 30 days</option>
                <option value={2160}>Last 90 days</option>
              </select>
              <button onClick={loadAudit} style={btnGhost(C)}>Refresh</button>
            </div>
          </div>
          {auditLoading ? (
            <div style={{ padding: 28, textAlign: "center", color: C.textSub }}>Loading…</div>
          ) : auditEvents.length === 0 ? (
            <div style={{ padding: 28, textAlign: "center", color: C.textSub }}>No role or MFA events in this window.</div>
          ) : (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr style={{ background: isDark ? C.surfaceAlt : "#F9FAFB" }}>
                  <th style={th(C)}>When</th>
                  <th style={th(C)}>Actor</th>
                  <th style={th(C)}>Entity</th>
                  <th style={th(C)}>Action</th>
                  <th style={th(C)}>Details</th>
                </tr>
              </thead>
              <tbody>
                {auditEvents.map(ev => (
                  <tr key={ev.id} style={{ borderTop: `1px solid ${C.borderLight}` }}>
                    <td style={{ ...td(C), color: C.textSub, whiteSpace: "nowrap" }}>
                      {ev.occurred_at ? new Date(ev.occurred_at).toLocaleString() : "—"}
                    </td>
                    <td style={td(C)}>
                      <p style={{ margin: 0, color: C.text, fontWeight: 700 }}>{ev.actor_name || ev.actor_email || "system"}</p>
                      {ev.actor_email && <p style={{ margin: "2px 0 0", color: C.textSub, fontSize: 11.5 }}>{ev.actor_email}</p>}
                    </td>
                    <td style={td(C)}><span style={{ color: C.text, fontWeight: 600 }}>{ev.entity}</span></td>
                    <td style={td(C)}><span style={{ color: C.brand, fontWeight: 700 }}>{ev.action}</span></td>
                    <td style={{ ...td(C), color: C.textSub, maxWidth: 360, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {ev.payload ? JSON.stringify(ev.payload) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* ─── Create custom role modal ───────────────────────────────── */}
      {showCreate && (
        <Modal title="Create custom role" onClose={() => setShowCreate(false)} width={620} C={C}>
          <div style={{ display: "grid", gap: 12 }}>
            <div>
              <label style={lbl(C)}>Role name</label>
              <input value={createDraft.label}
                onChange={e => setCreateDraft({ ...createDraft, label: e.target.value })}
                placeholder="e.g. Regional Operations Lead"
                style={inp(C)} />
            </div>
            <div>
              <label style={lbl(C)}>Description</label>
              <textarea value={createDraft.description}
                onChange={e => setCreateDraft({ ...createDraft, description: e.target.value })}
                placeholder="What this role can do and who should hold it."
                rows={2}
                style={{ ...inp(C), resize: "vertical", fontFamily: "inherit" }} />
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
              <div>
                <label style={lbl(C)}>Privilege tier</label>
                <select value={createDraft.tier}
                  onChange={e => setCreateDraft({ ...createDraft, tier: e.target.value })}
                  style={inp(C)}>
                  <option value="moderate">Moderate</option>
                  <option value="scoped">Scoped</option>
                  <option value="read_heavy">Read-heavy</option>
                  <option value="high">High privilege</option>
                </select>
              </div>
              <div>
                <label style={lbl(C)}>MFA stance</label>
                <select value={createDraft.mfa_tier}
                  onChange={e => setCreateDraft({ ...createDraft, mfa_tier: e.target.value })}
                  style={inp(C)}>
                  <option value="optional">Optional</option>
                  <option value="recommended">Recommended</option>
                  <option value="enforced">Enforced</option>
                  <option value="required">Required</option>
                </select>
              </div>
            </div>
            <div>
              <label style={lbl(C)}>Permissions ({createDraft.permissions.length} selected)</label>
              <div style={{
                border: `1px solid ${C.border}`, borderRadius: 8, padding: 10,
                maxHeight: 240, overflowY: "auto", display: "grid", gap: 6,
              }}>
                {permissions.map(p => {
                  const on = createDraft.permissions.includes(p.key);
                  return (
                    <label key={p.key} style={{
                      display: "flex", gap: 10, alignItems: "flex-start",
                      padding: 8, borderRadius: 6,
                      background: on ? (isDark ? "#0D2A1A" : "#ECFDF5") : "transparent",
                      cursor: "pointer",
                    }}>
                      <input type="checkbox" checked={on} onChange={() => {
                        const next = new Set(createDraft.permissions);
                        if (on) next.delete(p.key); else next.add(p.key);
                        setCreateDraft({ ...createDraft, permissions: Array.from(next) });
                      }} />
                      <div>
                        <p style={{ margin: 0, color: C.text, fontWeight: 700, fontSize: 12.5 }}>{p.key}</p>
                        <p style={{ margin: "2px 0 0", color: C.textSub, fontSize: 11.5, lineHeight: 1.5 }}>{p.description}</p>
                      </div>
                    </label>
                  );
                })}
              </div>
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 6 }}>
              <button onClick={() => setShowCreate(false)} style={btnGhost(C)}>Cancel</button>
              <button onClick={createRole} style={btnPrimary(C)} disabled={!createDraft.label.trim()}>Create role</button>
            </div>
          </div>
        </Modal>
      )}

      {/* ─── Single-user assign modal ───────────────────────────────── */}
      {assignTarget && (
        <Modal title={`Assign user to ${assignTarget.label}`} onClose={() => setAssignTarget(null)} width={480} C={C}>
          <div style={{ display: "grid", gap: 12 }}>
            <p style={{ margin: 0, color: C.textSub, fontSize: 13, lineHeight: 1.5 }}>
              The user will inherit all permissions on this role and their previous role assignment will be replaced.
            </p>
            <div>
              <label style={lbl(C)}>User</label>
              <select value={assignUserId} onChange={e => setAssignUserId(e.target.value)} style={inp(C)}>
                <option value="">Select a workspace user…</option>
                {users.map(u => (
                  <option key={u.id} value={u.id}>{u.name} — {u.email} ({u.role || "employee"})</option>
                ))}
              </select>
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
              <button onClick={() => setAssignTarget(null)} style={btnGhost(C)}>Cancel</button>
              <button onClick={assignSingle} style={btnPrimary(C)} disabled={!assignUserId}>Assign role</button>
            </div>
          </div>
        </Modal>
      )}

      {/* ─── Invite redirect modal ───────────────────────────────────
          Invite is handled by Workers & Users — the Access Governance
          Center just routes the user there with a one-tap link instead
          of duplicating the invite form.                                */}
      {showInvite && (
        <Modal title="Invite a workspace user" onClose={() => setShowInvite(false)} width={460} C={C}>
          <p style={{ margin: "0 0 14px", color: C.textSub, fontSize: 13.5, lineHeight: 1.5 }}>
            Inviting a user opens the <strong>Workers & Users</strong> tab where you can
            specify their email, default role, team, and onboarding email template. Once
            invited, return here to assign role-specific permissions or MFA stance.
          </p>
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
            <button onClick={() => setShowInvite(false)} style={btnGhost(C)}>Close</button>
            <button onClick={() => { setShowInvite(false); window.location.assign("/settings/workers-users"); }}
              style={btnPrimary(C)}>Open Workers & Users</button>
          </div>
        </Modal>
      )}

    </div>
  );
}

// ── Reusable table cell styles ────────────────────────────────────────────────
const th = (C) => ({
  padding: "10px 14px", textAlign: "left",
  fontSize: 11, fontWeight: 700, color: C.textSub,
  textTransform: "uppercase", letterSpacing: 0.4,
  borderBottom: `1px solid ${C.border}`,
});
const td = (C) => ({
  padding: "12px 14px", fontSize: 13, color: C.text, verticalAlign: "middle",
});
const lbl = (C) => ({
  display: "block", fontSize: 12, fontWeight: 700, color: C.textSub, marginBottom: 5,
});
const inp = (C) => ({
  width: "100%", padding: "9px 12px", borderRadius: 8,
  border: `1px solid ${C.border}`, background: C.surface, color: C.text,
  fontSize: 13, fontFamily: "inherit", outline: "none", boxSizing: "border-box",
});

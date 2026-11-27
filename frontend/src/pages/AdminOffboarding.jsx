/**
 * Admin Offboarding — the People → Offboarding section root.
 *
 * Internal navigation (no full page reload between sub-views):
 *   • Active Cases · Scheduled Exits · Blocked Cases · Completed Cases ·
 *     Former Worker Access · Legal Holds · Retention Review · Security Queues
 *
 * The visible-list view always renders <CaseRegister>, with the "saved filter"
 * pre-applied to match the section. The Case Detail view stacks on top when
 * a row is clicked. Security Queues, Former Access, Legal Holds and
 * Retention Review live as their own panels.
 *
 * Visual chrome (background, sidebar, header band, content padding) deliberately
 * mirrors `AdminDashboard.jsx` so navigation between Team Directory →
 * Screenshots → Attendance → Offboarding feels like one app.
 */
import React, { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Activity, Scale, ShieldCheck, UserCheck, FileSearch, Layers,
  AlertCircle, Search, Mail, Clock, RefreshCw, Bell, Sun, Moon,
} from "lucide-react";

import AdminSidebar from "../components/AdminSidebar";
import LangSwitcher from "../components/LangSwitcher";
import CaseRegister from "../components/offboarding/CaseRegister";
import CaseDetail from "../components/offboarding/CaseDetail";
import CreateCaseWizard from "../components/offboarding/CreateCaseWizard";
import SecurityDashboards from "../components/offboarding/SecurityDashboards";
import useDirectory from "../components/offboarding/useDirectory";
import {
  ActionButton, BlockerChip, EmptyState, SectionHeading, StatusChip, Surface,
} from "../components/offboarding/primitives";
import {
  listCases, listDeletionReviews, listPolicies, revokeFormerAccess,
} from "../api/offboarding";
import { useTheme } from "../context/ThemeContext";
import { useAuth } from "../context/AuthContext";
import toast from "react-hot-toast";

/** Initials for the profile-avatar fallback — same algorithm as the dashboard. */
const initials = (name) =>
  (name || "").split(" ").map((w) => w[0]).join("").toUpperCase().slice(0, 2) || "U";

const SECTIONS = [
  { key: "active",     label: "Active Cases",          icon: <Activity size={14} />,    filter: { status: "in_progress" } },
  { key: "scheduled",  label: "Scheduled Exits",       icon: <FileSearch size={14} />,  filter: { status: "scheduled" } },
  { key: "blocked",    label: "Blocked Cases",         icon: <AlertCircle size={14} />, filter: { status: "blocked" } },
  { key: "completed",  label: "Completed Cases",       icon: <ShieldCheck size={14} />, filter: { status: "completed" } },
  { key: "former",     label: "Former Worker Access",  icon: <UserCheck size={14} />,   filter: { status: "former_access_pending" } },
  { key: "legal",      label: "Legal Holds",           icon: <Scale size={14} />,       filter: { legal_hold_status: "active" } },
  { key: "retention",  label: "Retention Review",      icon: <Layers size={14} />,      filter: {} },
  { key: "security",   label: "Security Queues",       icon: <ShieldCheck size={14} />, filter: {} },
];

export default function AdminOffboarding() {
  const { C, isDark, toggleDark } = useTheme();
  const { user: authUser } = useAuth();
  const [section, setSection] = useState("active");
  const [activeCaseId, setActiveCaseId] = useState(null);
  const [showWizard, setShowWizard] = useState(false);
  const [globalSearch, setGlobalSearch] = useState("");
  const [registerReloadKey, setRegisterReloadKey] = useState(0);
  const navigate = useNavigate();

  // Keyboard escape closes detail / wizard, matching the rest of the admin UX.
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") {
        if (showWizard) setShowWizard(false);
        else if (activeCaseId) setActiveCaseId(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [showWizard, activeCaseId]);

  return (
    <div style={{ display: "flex", height: "100vh", background: C.bg, overflow: "hidden" }}>
      <AdminSidebar activeKey="offboarding" />

      <main style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
        {/* ═══ Top nav — identical to AdminDashboard so the two pages share
              the same chrome. Search filters the register, refresh re-runs the
              register query, dashboard-only buttons (Invite, Today, All Types)
              navigate back to /admin where those state owners live. ═══ */}
        <AdminTopNav
          search={globalSearch}
          onSearch={setGlobalSearch}
          authUser={authUser}
          onInvite={() => navigate("/admin")}
          onToday={() => navigate("/admin")}
          onTypes={() => navigate("/admin")}
          onRefresh={() => setRegisterReloadKey((k) => k + 1)}
          onBell={() => navigate("/admin")}
          onProfile={() => navigate("/profile")}
          isDark={isDark}
          toggleDark={toggleDark}
        />

        {/* ── Page heading band — kept under the top nav so the section context
              ("People / Offboarding") is still visible without crowding the nav. */}
        <div
          style={{
            flexShrink: 0,
            background: C.surface,
            borderBottom: `1px solid ${C.border}`,
            padding: "14px 24px",
          }}
        >
          <nav aria-label="Breadcrumb" style={{ fontSize: 11, color: C.textLight, fontWeight: 600, letterSpacing: 0.4, textTransform: "uppercase" }}>
            <span>People</span> <span style={{ padding: "0 6px" }}>/</span>
            <span style={{ color: C.textSub }}>Offboarding</span>
          </nav>
          <h1 style={{ margin: "4px 0 0", fontSize: 18, fontWeight: 800, color: C.text }}>
            Offboarding & Former-Worker Lifecycle
          </h1>
        </div>

        {/* Scrollable content. Matches the dashboard's main content padding. */}
        <div style={{ flex: 1, overflowY: "auto", padding: "20px 24px 32px" }}>
          {/* Section tabs (mirror the spec's tree). Pill-style, brand colour
              when active — same chip vocabulary as the rest of the admin. */}
          <div
            role="tablist"
            aria-label="Offboarding sections"
            style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 20 }}
          >
            {SECTIONS.map((s) => (
              <SectionTab
                key={s.key}
                active={section === s.key}
                onClick={() => { setSection(s.key); setActiveCaseId(null); }}
                icon={s.icon}
                label={s.label}
              />
            ))}
          </div>

          {/* Body */}
          {activeCaseId ? (
            <CaseDetail caseId={activeCaseId} onBack={() => setActiveCaseId(null)} />
          ) : section === "security" ? (
            <SecurityDashboards />
          ) : section === "retention" ? (
            <RetentionReview />
          ) : section === "former" ? (
            <FormerAccessList onOpen={setActiveCaseId} />
          ) : section === "legal" ? (
            <LegalHoldsList onOpen={setActiveCaseId} />
          ) : (
            <FilteredRegister
              section={section}
              onCreate={() => setShowWizard(true)}
              onOpen={setActiveCaseId}
              externalSearch={globalSearch}
              reloadKey={registerReloadKey}
            />
          )}
        </div>

        {showWizard && (
          <CreateCaseWizard
            onClose={() => setShowWizard(false)}
            onCreated={(created) => { setShowWizard(false); setActiveCaseId(created.id); }}
          />
        )}
      </main>
    </div>
  );
}

/* ════════════════════════════════════════════════════════════════════════
   AdminTopNav — visual twin of the AdminDashboard header band.

   We deliberately re-implement (rather than share) the header so the
   dashboard's tightly-coupled state (employee filter, date picker,
   notifications) keeps its current owner. The buttons that depend on
   dashboard state (Invite Worker, Today, All Types, Bell) navigate back
   to /admin where they are functional; the rest (search, refresh, theme,
   language, profile) operate directly on the offboarding page.

   Both light and dark themes are driven by `useTheme()`, so the band
   inherits the same surface / border / brand tokens as the dashboard.
   ════════════════════════════════════════════════════════════════════════ */
function AdminTopNav({
  search, onSearch, authUser,
  onInvite, onToday, onTypes, onRefresh, onBell, onProfile,
  isDark, toggleDark,
}) {
  const { C } = useTheme();
  const name = authUser?.name || "Admin";
  const email = authUser?.email || "";
  const picture = authUser?.profile_picture || null;

  // Match the dashboard's 64px header band exactly so navigation between
  // /admin and /admin/offboarding doesn't shift the page vertically.
  return (
    <header
      style={{
        height: 64, flexShrink: 0,
        background: C.surface, borderBottom: `1px solid ${C.border}`,
        display: "flex", alignItems: "center", gap: 12,
        padding: "0 24px",
        boxShadow: isDark ? "0 1px 2px rgba(0,0,0,0.4)" : "0 1px 2px rgba(16,24,40,0.04)",
      }}
    >
      {/* ── Search ────────────────────────────────────────────────── */}
      <div style={{ flex: 1, position: "relative", maxWidth: 360 }}>
        <span style={{ position: "absolute", left: 12, top: "50%", transform: "translateY(-50%)", display: "inline-flex" }}>
          <Search size={15} color={C.textLight} />
        </span>
        <input
          placeholder="Search employees..."
          value={search}
          onChange={(e) => onSearch(e.target.value)}
          style={{
            width: "100%", padding: "9px 14px 9px 36px",
            borderRadius: C.rSm,
            border: `1.5px solid ${C.border}`, background: C.bg,
            color: C.text, fontSize: 13, fontFamily: "inherit", outline: "none",
            transition: "border-color .2s, box-shadow .2s",
          }}
          onFocus={(e) => { e.target.style.borderColor = C.brand; e.target.style.boxShadow = `0 0 0 3px ${C.brandBg}`; }}
          onBlur={(e) => { e.target.style.borderColor = C.border; e.target.style.boxShadow = "none"; }}
        />
      </div>

      <div style={{ flex: 1 }} />

      {/* ── Admin Portal badge (live status dot) ──────────────────── */}
      <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 2, marginRight: 4 }}>
        <p style={{ margin: 0, fontSize: 11, color: C.textSub, fontWeight: 600, lineHeight: 1 }}>
          Admin Portal
        </p>
        <div style={{ display: "flex", alignItems: "center", gap: 4 }} title="Live">
          <span
            style={{
              width: 6, height: 6, borderRadius: "50%",
              background: "#22C55E", boxShadow: "0 0 6px #22C55E",
              display: "inline-block",
            }}
          />
          <span style={{ fontSize: 9, color: C.textLight, fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.5 }}>
            Live
          </span>
        </div>
      </div>

      {/* ── Invite Worker (brand gradient CTA) ────────────────────── */}
      <button
        onClick={onInvite}
        style={{
          display: "flex", alignItems: "center", gap: 7,
          padding: "9px 20px", borderRadius: C.rSm, border: "none",
          background: `linear-gradient(135deg, ${C.brand}, ${C.brandDark})`,
          color: "#fff", fontWeight: 700, fontSize: 13,
          cursor: "pointer", fontFamily: "inherit",
          boxShadow: `0 2px 8px ${C.brandAccent}55`,
          transition: "all .2s",
        }}
        onMouseEnter={(e) => { e.currentTarget.style.transform = "translateY(-1px)"; e.currentTarget.style.boxShadow = `0 6px 16px ${C.brandAccent}80`; }}
        onMouseLeave={(e) => { e.currentTarget.style.transform = "none"; e.currentTarget.style.boxShadow = `0 2px 8px ${C.brandAccent}55`; }}
      >
        <Mail size={14} color="#fff" /> Invite Worker
      </button>

      {/* ── Today (date filter passthrough) ───────────────────────── */}
      <button
        onClick={onToday}
        style={{
          display: "flex", alignItems: "center", gap: 6,
          padding: "8px 16px", borderRadius: C.rSm,
          border: `1.5px solid ${C.border}`,
          background: C.surface, color: C.text,
          fontWeight: 500, fontSize: 13,
          cursor: "pointer", fontFamily: "inherit",
          transition: "all .2s",
        }}
      >
        <Clock size={13} color={C.textSub} /> Today
      </button>

      {/* ── All Types ─────────────────────────────────────────────── */}
      <button
        onClick={onTypes}
        style={{
          display: "flex", alignItems: "center", gap: 6,
          padding: "8px 14px", borderRadius: C.rSm,
          border: `1.5px solid ${C.textSub}`,
          background: C.surface, color: C.text,
          fontWeight: 500, fontSize: 13,
          cursor: "pointer", fontFamily: "inherit",
          outline: "none", transition: "all .2s",
          boxShadow: "0 1px 3px rgba(0,0,0,0.12)",
        }}
      >
        All Types
        <span style={{ marginLeft: 4, color: C.textSub, fontSize: 11 }}>▾</span>
      </button>

      {/* ── Refresh ───────────────────────────────────────────────── */}
      <button
        onClick={onRefresh}
        title="Refresh"
        style={{
          width: 38, height: 38, borderRadius: "50%",
          border: `1.5px solid ${C.border}`, background: C.surface,
          display: "flex", alignItems: "center", justifyContent: "center",
          cursor: "pointer", flexShrink: 0, transition: "all .2s",
        }}
        onMouseEnter={(e) => { e.currentTarget.style.borderColor = C.brandAccent; e.currentTarget.style.background = C.brandAccentBg; }}
        onMouseLeave={(e) => { e.currentTarget.style.borderColor = C.border; e.currentTarget.style.background = C.surface; }}
      >
        <RefreshCw size={16} color={C.textSub} />
      </button>

      {/* ── Bell (notifications) ──────────────────────────────────── */}
      <button
        onClick={onBell}
        title="Notifications"
        style={{
          width: 38, height: 38, borderRadius: "50%",
          border: `1.5px solid ${C.border}`, background: C.surface,
          display: "flex", alignItems: "center", justifyContent: "center",
          cursor: "pointer", flexShrink: 0, transition: "all .2s",
        }}
        onMouseEnter={(e) => { e.currentTarget.style.borderColor = C.brandAccent; e.currentTarget.style.background = C.brandAccentBg; }}
        onMouseLeave={(e) => { e.currentTarget.style.borderColor = C.border; e.currentTarget.style.background = C.surface; }}
      >
        <Bell size={17} color={C.textSub} />
      </button>

      {/* ── Theme toggle ──────────────────────────────────────────── */}
      <button
        onClick={toggleDark}
        title={isDark ? "Switch to light" : "Switch to dark"}
        style={{
          width: 38, height: 38, borderRadius: "50%",
          border: `1.5px solid ${C.border}`, background: C.surface,
          display: "flex", alignItems: "center", justifyContent: "center",
          cursor: "pointer", flexShrink: 0, transition: "all .2s",
        }}
      >
        {isDark ? <Sun size={16} color={C.textSub} /> : <Moon size={16} color={C.textSub} />}
      </button>

      {/* ── Language switcher (shared component) ──────────────────── */}
      <LangSwitcher isDark={isDark} />

      {/* ── Profile avatar ────────────────────────────────────────── */}
      <button
        type="button"
        onClick={onProfile}
        aria-label="Open profile"
        style={{
          width: 38, height: 38, borderRadius: "50%",
          background: picture
            ? `url(${picture}) center/cover`
            : `linear-gradient(135deg, ${C.brand}, ${C.brandDark})`,
          display: "flex", alignItems: "center", justifyContent: "center",
          color: "#fff", fontWeight: 800, fontSize: 14,
          cursor: "pointer",
          boxShadow: "0 2px 8px rgba(17,73,106,0.35)",
          border: "2px solid transparent",
          padding: 0, outline: "none",
          transition: "box-shadow .2s, transform .2s",
        }}
        title={`${name}${email ? ` · ${email}` : ""}`}
      >
        {!picture && initials(name)}
      </button>
    </header>
  );
}

/** Section tab pill — brand colour when active, neutral chip otherwise. */
function SectionTab({ active, onClick, icon, label }) {
  const { C } = useTheme();
  const base = {
    display: "inline-flex",
    alignItems: "center",
    gap: 6,
    padding: "8px 14px",
    borderRadius: 999,
    fontSize: 12,
    fontWeight: 700,
    cursor: "pointer",
    fontFamily: "inherit",
    transition: "all .18s ease",
  };
  const activeStyle = {
    background: C.brand,
    color: "#fff",
    border: `1.5px solid ${C.brand}`,
    boxShadow: `0 2px 8px ${C.brandAccent}33`,
  };
  const restStyle = {
    background: C.surface,
    color: C.textSub,
    border: `1.5px solid ${C.border}`,
  };
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      style={{ ...base, ...(active ? activeStyle : restStyle) }}
      onMouseEnter={(e) => {
        if (!active) {
          e.currentTarget.style.borderColor = C.brandBdr;
          e.currentTarget.style.color = C.brand;
        }
      }}
      onMouseLeave={(e) => {
        if (!active) {
          e.currentTarget.style.borderColor = C.border;
          e.currentTarget.style.color = C.textSub;
        }
      }}
    >
      <span style={{ display: "inline-flex" }} aria-hidden="true">{icon}</span>
      {label}
    </button>
  );
}

/**
 * FilteredRegister — pre-applies a section's saved filter against the
 * existing CaseRegister. The chip-row inside the register still works on top.
 *
 * Each FilteredRegister section key matches a REGISTER_FILTERS chip key 1:1
 * (active / scheduled / blocked / completed), so we pre-select that chip via
 * the `initialChip` prop on first mount. `key={section}` forces a fresh
 * mount when the user switches tabs so the initial chip re-applies.
 */
function FilteredRegister({ section, onCreate, onOpen, externalSearch, reloadKey }) {
  const { C } = useTheme();
  const sec = SECTIONS.find((s) => s.key === section);
  return (
    <div>
      <CaseRegister
        onCreate={onCreate}
        onOpen={onOpen}
        initialChip={section}
        externalSearch={externalSearch}
        externalReloadKey={reloadKey}
        key={section}
      />
      {sec?.filter && Object.keys(sec.filter).length > 0 && (
        <p style={{ marginTop: 12, fontSize: 11, color: C.textLight }}>
          Default filter for this section: {JSON.stringify(sec.filter)} — clear chips to see everything.
        </p>
      )}
    </div>
  );
}

function FormerAccessList({ onOpen }) {
  const { C } = useTheme();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const { labelFor } = useDirectory();
  useEffect(() => {
    listCases({ status: "former_access_pending", limit: 200 })
      .then((r) => setRows(r.items || []))
      .finally(() => setLoading(false));
  }, []);
  return (
    <Surface>
      <SectionHeading
        title="Former Worker Access"
        description="Cases at the former-access stage. Click into a case to grant or revoke."
      />
      {loading ? (
        <div style={{ fontSize: 13, color: C.textSub }} aria-live="polite">Loading…</div>
      ) : rows.length === 0 ? (
        <EmptyState title="No former-access cases" />
      ) : (
        <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column" }}>
          {rows.map((c, i) => (
            <li
              key={c.id}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: 12,
                padding: "12px 0",
                borderTop: i === 0 ? "none" : `1px solid ${C.borderLight}`,
              }}
            >
              <button
                onClick={() => onOpen(c.id)}
                style={{
                  flex: 1,
                  textAlign: "left",
                  background: "transparent",
                  border: "none",
                  cursor: "pointer",
                  padding: 0,
                  fontFamily: "inherit",
                }}
              >
                <span style={{ fontSize: 13, fontWeight: 700, color: C.text }}>{c.engagement_id}</span>
                <span style={{ marginLeft: 8, fontSize: 12, color: C.textSub }}>
                  {c.worker_id != null ? `${labelFor(c.worker_id)} · #${c.worker_id}` : "no worker linked"}
                </span>
              </button>
              <StatusChip status={c.status} />
              <ActionButton variant="subtle" size="sm" onClick={async () => {
                if (!confirm("Revoke former-worker access?")) return;
                try { await revokeFormerAccess(c.id); toast.success("Revoked"); }
                catch (e) { toast.error(e?.response?.data?.detail?.message || "Failed"); }
              }}>Revoke</ActionButton>
            </li>
          ))}
        </ul>
      )}
    </Surface>
  );
}

function LegalHoldsList({ onOpen }) {
  const { C } = useTheme();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const { labelFor } = useDirectory();
  useEffect(() => {
    listCases({ legal_hold_status: "active", limit: 200 })
      .then((r) => setRows(r.items || []))
      .finally(() => setLoading(false));
  }, []);
  return (
    <Surface>
      <SectionHeading
        title="Legal Holds"
        description="Cases under an active legal hold. Click in to manage holds, retention, and the deletion review."
      />
      {loading ? (
        <div style={{ fontSize: 13, color: C.textSub }} aria-live="polite">Loading…</div>
      ) : rows.length === 0 ? (
        <EmptyState title="No active legal holds" />
      ) : (
        <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
          {rows.map((c, i) => (
            <li
              key={c.id}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: 12,
                padding: "12px 0",
                borderTop: i === 0 ? "none" : `1px solid ${C.borderLight}`,
              }}
            >
              <button
                onClick={() => onOpen(c.id)}
                style={{
                  flex: 1, textAlign: "left", background: "transparent",
                  border: "none", cursor: "pointer", padding: 0, fontFamily: "inherit",
                }}
              >
                <span style={{ fontSize: 13, fontWeight: 700, color: C.text }}>{c.engagement_id}</span>
                <span style={{ marginLeft: 8, fontSize: 12, color: C.textSub }}>
                  {c.worker_id != null ? `${labelFor(c.worker_id)} · #${c.worker_id}` : "no worker linked"}
                </span>
              </button>
              <StatusChip status={c.status} />
              <BlockerChip count={0 /* detail view surfaces real counts */} />
            </li>
          ))}
        </ul>
      )}
    </Surface>
  );
}

function RetentionReview() {
  const { C } = useTheme();
  const [reviews, setReviews] = useState([]);
  const [policies, setPolicies] = useState([]);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    Promise.all([listDeletionReviews({ limit: 100 }), listPolicies()])
      .then(([r, p]) => { setReviews(r); setPolicies(p); })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <Surface>
        <SectionHeading
          title="Deletion eligibility reviews"
          description="Pending dual-approval reviews. Both Legal and Compliance must sign off, and eligibility is re-checked at decision time."
        />
        {loading ? (
          <div style={{ fontSize: 13, color: C.textSub }} aria-live="polite">Loading…</div>
        ) : reviews.length === 0 ? (
          <EmptyState title="No open deletion reviews" />
        ) : (
          <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {reviews.map((r, i) => (
              <li
                key={r.id}
                style={{
                  display: "flex", flexWrap: "wrap", alignItems: "center",
                  justifyContent: "space-between", gap: 8,
                  padding: "10px 0",
                  borderTop: i === 0 ? "none" : `1px solid ${C.borderLight}`,
                  fontSize: 13,
                }}
              >
                <div>
                  <span style={{ fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 11, color: C.textLight }}>
                    {r.id.slice(0, 12)}…
                  </span>{" "}
                  <span style={{ fontWeight: 700, color: C.text }}>{r.status}</span>{" "}
                  <span style={{ color: C.textSub }}>
                    missing: {r.missing_approvers?.join(", ") || "—"}
                  </span>
                </div>
                <div style={{ fontSize: 11, color: C.textSub }}>{r.case_id?.slice(0, 12)}…</div>
              </li>
            ))}
          </ul>
        )}
      </Surface>

      <Surface>
        <SectionHeading
          title="Retention policies"
          description="Versioned, jurisdiction-aware. Supersede a code to publish a new version without re-clocking already-assigned cases."
        />
        {loading ? (
          <div style={{ fontSize: 13, color: C.textSub }}>Loading…</div>
        ) : (
          <ul style={{ listStyle: "none", margin: 0, padding: 0, fontSize: 13 }}>
            {policies.map((p, i) => (
              <li
                key={p.id}
                style={{
                  display: "flex", flexWrap: "wrap", alignItems: "center",
                  justifyContent: "space-between", gap: 8,
                  padding: "10px 0",
                  borderTop: i === 0 ? "none" : `1px solid ${C.borderLight}`,
                }}
              >
                <div>
                  <span style={{ fontWeight: 700, color: C.text }}>{p.code}</span>{" "}
                  <span style={{ fontSize: 11, color: C.textSub }}>
                    v{p.version} · {p.retention_days}d{p.jurisdiction ? ` · ${p.jurisdiction}` : ""}
                  </span>
                </div>
                <span
                  style={{
                    fontSize: 11, fontWeight: 700,
                    padding: "3px 10px", borderRadius: 999,
                    background: p.active ? C.greenBg : C.surfaceAlt,
                    color: p.active ? C.green : C.textSub,
                    border: `1px solid ${p.active ? C.greenBdr : C.border}`,
                  }}
                >
                  {p.active ? "Active" : "Superseded"}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Surface>
    </div>
  );
}

/**
 * Offboarding Register — searchable, filterable, paginated list of cases.
 *
 * Visual treatment matches the admin dashboard surface conventions:
 *   • white surface card with admin border radius + border colour
 *   • theme tokens for every colour (light/dark mode native)
 *   • table chrome identical to the Reports tab / Attendance table
 *
 * Behaviour is unchanged — `listCases()` with three orthogonal axes
 * (search, saved-filter chips, field-level filters) and server-driven
 * pagination.
 */
import React, { useEffect, useMemo, useState } from "react";
import {
  Search, Filter, ChevronLeft, ChevronRight, RefreshCw, Plus,
} from "lucide-react";

import { listCases, REGISTER_FILTERS } from "../../api/offboarding";
import {
  ActionButton, BlockerChip, EmptyState, RiskBadge, SectionHeading,
  SLAIndicator, StatusChip, Surface,
} from "./primitives";
import useDirectory from "./useDirectory";
import { useTheme } from "../../context/ThemeContext";

const PAGE_SIZES = [25, 50, 100];

const STATUS_OPTIONS = {
  draft: "Draft", pending_approval: "Pending approval", approved: "Approved",
  scheduled: "Scheduled", in_progress: "In progress",
  access_closure_pending: "Access closure",
  access_revocation_failed: "Revocation failed",
  final_review_pending: "Final review",
  payroll_closure_pending: "Payroll closure",
  billing_closure_pending: "Billing closure",
  legal_hold_review_pending: "Legal hold review",
  former_access_pending: "Former access",
  blocked: "Blocked", completed: "Completed",
  reversed: "Reversed", cancelled: "Cancelled",
};

export default function CaseRegister({
  onCreate, onOpen, initialChip = null,
  externalSearch = "", externalReloadKey = 0,
}) {
  const { C } = useTheme();
  const [search, setSearch] = useState("");
  const [activeFilter, setActiveFilter] = useState(initialChip);
  const [department, setDepartment] = useState("");
  const [statusOverride, setStatusOverride] = useState("");
  const [limit, setLimit] = useState(25);
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState({ items: [], total: 0, limit, offset });
  const [loading, setLoading] = useState(true);
  const [firstLoad, setFirstLoad] = useState(true);
  const [error, setError] = useState(null);
  const { labelFor, departments } = useDirectory();

  // Effective search merges the in-card field with the optional top-nav
  // search the parent page can push down. The local field always wins so
  // operators can override the global value without first clearing it.
  const effectiveSearch = (search.trim() || externalSearch.trim());

  const params = useMemo(() => {
    const chip = activeFilter ? REGISTER_FILTERS.find((f) => f.key === activeFilter)?.params : {};
    const p = { ...(chip || {}), limit, offset };
    if (statusOverride) p.status = statusOverride;
    if (department) p.department = department;
    if (effectiveSearch.length >= 2) p.engagement_id = effectiveSearch;
    return p;
  }, [activeFilter, statusOverride, department, effectiveSearch, limit, offset]);

  const [reloadKey, setReloadKey] = useState(0);
  // Bumping `externalReloadKey` (top-nav Refresh) re-runs the same query.
  useEffect(() => {
    if (externalReloadKey > 0) setReloadKey((k) => k + 1);
  }, [externalReloadKey]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    listCases(params)
      .then((res) => { if (!cancelled) setData(res); })
      .catch((e) => {
        if (cancelled) return;
        const msg = e?.response?.data?.detail || e?.message || "Failed to load cases";
        setError(typeof msg === "string" ? msg : JSON.stringify(msg));
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
        setFirstLoad(false);
      });
    return () => { cancelled = true; };
  }, [params, reloadKey]);

  const totalPages = Math.max(1, Math.ceil((data.total || 0) / limit));
  const currentPage = Math.floor(offset / limit) + 1;

  // Shared input styling — same focus ring + colours as the admin search bar.
  const focusInput = (e) => {
    e.target.style.borderColor = C.brand;
    e.target.style.boxShadow = `0 0 0 3px ${C.brandBg}`;
  };
  const blurInput = (e) => {
    e.target.style.borderColor = C.border;
    e.target.style.boxShadow = "none";
  };
  const inputStyle = {
    padding: "9px 12px", borderRadius: C.rSm,
    border: `1.5px solid ${C.border}`, background: C.bg,
    color: C.text, fontSize: 13, fontFamily: "inherit", outline: "none",
    transition: "border-color .2s, box-shadow .2s",
  };

  return (
    <Surface>
      <SectionHeading
        title="Offboarding Register"
        description="Every offboarding case in flight. Use chips and search to triage; click a row to open the detail view."
        right={
          <>
            <ActionButton variant="subtle" size="sm" onClick={() => setReloadKey((k) => k + 1)} aria-label="Refresh">
              <RefreshCw size={13} /> Refresh
            </ActionButton>
            <ActionButton onClick={onCreate}>
              <Plus size={14} /> New case
            </ActionButton>
          </>
        }
      />

      {/* ── Search + secondary filters ──────────────────────────────── */}
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10, marginBottom: 14 }}>
        <label style={{ position: "relative", flex: 1, minWidth: 240 }}>
          <span style={{ position: "absolute", left: 12, top: "50%", transform: "translateY(-50%)", display: "inline-flex" }}>
            <Search size={14} color={C.textLight} aria-hidden="true" />
          </span>
          <input
            type="search"
            value={search}
            onChange={(e) => { setSearch(e.target.value); setOffset(0); }}
            placeholder="Search by engagement id…"
            aria-label="Search by engagement id"
            onFocus={focusInput}
            onBlur={blurInput}
            style={{ ...inputStyle, width: "100%", paddingLeft: 34 }}
          />
        </label>

        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Filter size={14} color={C.textLight} aria-hidden="true" />
          <input
            type="text"
            value={department}
            onChange={(e) => { setDepartment(e.target.value); setOffset(0); }}
            placeholder="Department"
            aria-label="Filter by department"
            list="register-department-options"
            onFocus={focusInput}
            onBlur={blurInput}
            style={{ ...inputStyle, width: 170 }}
          />
          <datalist id="register-department-options">
            {departments.map((d) => <option key={d} value={d} />)}
          </datalist>
          <select
            value={statusOverride}
            onChange={(e) => { setStatusOverride(e.target.value); setOffset(0); }}
            aria-label="Filter by status"
            style={{ ...inputStyle, paddingRight: 28, cursor: "pointer" }}
          >
            <option value="">All statuses</option>
            {Object.entries(STATUS_OPTIONS).map(([k, v]) => (
              <option key={k} value={k}>{v}</option>
            ))}
          </select>
        </div>
      </div>

      {/* ── Filter chips ──────────────────────────────────────────── */}
      <div
        role="tablist"
        aria-label="Saved filters"
        style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6, marginBottom: 16 }}
      >
        <FilterChip
          label="All"
          active={!activeFilter}
          onClick={() => { setActiveFilter(null); setOffset(0); }}
        />
        {REGISTER_FILTERS.map((f) => (
          <FilterChip
            key={f.key}
            label={f.label}
            active={activeFilter === f.key}
            onClick={() => { setActiveFilter(f.key === activeFilter ? null : f.key); setOffset(0); }}
          />
        ))}
      </div>

      {/* ── Table ──────────────────────────────────────────────────── */}
      <div
        style={{
          overflowX: "auto",
          borderRadius: C.r,
          border: `1px solid ${C.border}`,
          background: C.surface,
        }}
      >
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr style={{ background: C.surfaceAlt, textAlign: "left" }}>
              {["Engagement", "Worker", "Status", "Risk", "Department", "Legal hold", "Final access", "Created"].map((h) => (
                <th
                  key={h}
                  scope="col"
                  style={{
                    padding: "10px 14px",
                    fontSize: 11, fontWeight: 700,
                    color: C.textSub, textTransform: "uppercase",
                    letterSpacing: "0.5px",
                    borderBottom: `1px solid ${C.border}`,
                  }}
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading && firstLoad && (
              <tr>
                <td colSpan={8} style={{ padding: "32px 14px", textAlign: "center", fontSize: 13, color: C.textSub }}>
                  Loading…
                </td>
              </tr>
            )}
            {!loading && error && (
              <tr>
                <td colSpan={8} style={{ padding: "32px 14px", textAlign: "center" }}>
                  <div role="alert" style={{ margin: "0 auto", maxWidth: 420, display: "flex", flexDirection: "column", alignItems: "center", gap: 10, fontSize: 13, color: C.red }}>
                    <span>{error}</span>
                    <ActionButton variant="secondary" size="sm" onClick={() => setReloadKey((k) => k + 1)} aria-label="Retry loading cases">
                      <RefreshCw size={13} /> Retry
                    </ActionButton>
                  </div>
                </td>
              </tr>
            )}
            {!firstLoad && !error && data.items?.length === 0 && (
              <tr>
                <td colSpan={8} style={{ padding: 16 }}>
                  <EmptyState
                    title="No cases match these filters"
                    hint="Click + New case to create one, or clear the filter chips above."
                  />
                </td>
              </tr>
            )}
            {data.items?.map((c, idx) => (
              <tr
                key={c.id}
                tabIndex={0}
                role="link"
                aria-label={`Open case ${c.engagement_id}`}
                onClick={() => onOpen?.(c.id)}
                onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") onOpen?.(c.id); }}
                onMouseEnter={(e) => { e.currentTarget.style.background = C.surfaceAlt; }}
                onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}
                style={{
                  cursor: "pointer",
                  borderTop: `1px solid ${C.borderLight}`,
                  transition: "background .15s",
                }}
              >
                <td style={cellStyle(C, { weight: 700, color: C.text })}>{c.engagement_id}</td>
                <td style={cellStyle(C)}>
                  {c.worker_id == null ? (
                    <span style={{ color: C.textLight }}>—</span>
                  ) : (
                    <div style={{ display: "flex", flexDirection: "column" }}>
                      <span style={{ color: C.text }}>{labelFor(c.worker_id)}</span>
                      <span style={{ fontSize: 11, color: C.textLight }}>#{c.worker_id}</span>
                    </div>
                  )}
                </td>
                <td style={cellStyle(C)}><StatusChip status={c.status} /></td>
                <td style={cellStyle(C)}>{c.risk_level && <RiskBadge risk={c.risk_level} />}</td>
                <td style={cellStyle(C)}>{c.department || <span style={{ color: C.textLight }}>—</span>}</td>
                <td style={cellStyle(C)}>
                  {c.legal_hold_status === "active" ? (
                    <span
                      style={{
                        display: "inline-flex", alignItems: "center", gap: 4,
                        padding: "3px 10px", borderRadius: 999,
                        background: C.amberBg, color: C.amber,
                        border: `1px solid ${C.amberBdr}`,
                        fontSize: 11, fontWeight: 700,
                      }}
                    >
                      Active
                    </span>
                  ) : (
                    <span style={{ fontSize: 12, color: C.textLight }}>—</span>
                  )}
                </td>
                <td style={cellStyle(C)}><SLAIndicator dueAt={c.final_access_at} label="Cutover" /></td>
                <td style={cellStyle(C, { color: C.textSub })}>
                  {c.created_at ? new Date(c.created_at).toLocaleDateString() : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* ── Pagination ─────────────────────────────────────────────── */}
      <div
        style={{
          marginTop: 14,
          display: "flex", flexWrap: "wrap", alignItems: "center",
          justifyContent: "space-between", gap: 10,
          fontSize: 13, color: C.textSub,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span>{data.total ?? 0} cases</span>
          <select
            value={limit}
            onChange={(e) => { setLimit(Number(e.target.value)); setOffset(0); }}
            aria-label="Rows per page"
            style={{ ...inputStyle, padding: "6px 10px", cursor: "pointer" }}
          >
            {PAGE_SIZES.map((n) => <option key={n} value={n}>{n} / page</option>)}
          </select>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <ActionButton
            variant="subtle"
            size="sm"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - limit))}
            aria-label="Previous page"
          >
            <ChevronLeft size={13} /> Prev
          </ActionButton>
          <span aria-live="polite" style={{ padding: "0 8px", color: C.textSub }}>
            Page {currentPage} / {totalPages}
          </span>
          <ActionButton
            variant="subtle"
            size="sm"
            disabled={currentPage >= totalPages}
            onClick={() => setOffset(offset + limit)}
            aria-label="Next page"
          >
            Next <ChevronRight size={13} />
          </ActionButton>
        </div>
      </div>
    </Surface>
  );
}

function FilterChip({ label, active, onClick }) {
  const { C } = useTheme();
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
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
      style={{
        padding: "6px 14px",
        borderRadius: 999,
        fontSize: 12,
        fontWeight: 700,
        cursor: "pointer",
        fontFamily: "inherit",
        transition: "all .18s ease",
        background: active ? C.brand : C.surface,
        color: active ? "#fff" : C.textSub,
        border: `1.5px solid ${active ? C.brand : C.border}`,
      }}
    >
      {label}
    </button>
  );
}

function cellStyle(C, extra = {}) {
  return {
    padding: "12px 14px",
    fontSize: 13,
    color: extra.color || C.text,
    fontWeight: extra.weight || 500,
    verticalAlign: "middle",
    ...extra,
  };
}

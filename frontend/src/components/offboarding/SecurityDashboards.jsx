/**
 * Security Dashboards — six queues into the enforcement layer.
 *
 *   1. Revocation failures   (ACCESS_REVOCATION_FAILED cases, revocation rows status=failed)
 *   2. Identity conflicts    (incidents whose type is one of the conflict scenarios)
 *   3. Token failures        (SESSION_REVOCATION_FAILURE / TOKEN_STILL_ACTIVE incidents)
 *   4. Telemetry violations  (TRACKING_AFTER_OFFBOARDING incidents + quarantine rows)
 *   5. Audit failures        (AUDIT_FAILURE incidents)
 *   6. Legal-hold violations (LEGAL_HOLD_DELETION_ATTEMPT / FORMER_WORKER_ABUSE)
 *
 * Visual treatment matches the admin module suite (theme tokens, surface
 * cards, brand-pill tabs).
 */
import React, { useEffect, useState } from "react";
import {
  Activity, AlertTriangle, FileWarning, Network, Shield, Skull,
} from "lucide-react";

import { listSecurityEvents, listReconciliation, retryRevocation } from "../../api/offboarding";
import {
  ActionButton, EmptyState, SectionHeading, SeverityDot, Surface,
} from "./primitives";
import { useTheme } from "../../context/ThemeContext";
import toast from "react-hot-toast";

const QUEUES = [
  {
    key: "revocation_failures",
    label: "Revocation failures",
    icon: <Shield size={14} />,
    description: "Cases where one or more access surfaces failed to revoke.",
    types: ["session_revocation_failure"],
  },
  {
    key: "identity_conflicts",
    label: "Identity conflicts",
    icon: <Network size={14} />,
    description: "SSO / SCIM drift, multiple identities, rehire conflicts.",
    types: [
      "idp_active_zoiko_revoked", "scim_deprovision_failure",
      "stale_sso_state", "multiple_identities", "rehire_conflict",
      "reconciliation_conflict",
    ],
  },
  {
    key: "token_failures",
    label: "Token failures",
    icon: <Skull size={14} />,
    description: "Refresh-token / session revocation that didn't land.",
    types: ["token_still_active"],
  },
  {
    key: "telemetry",
    label: "Telemetry violations",
    icon: <Activity size={14} />,
    description: "Post-exit tracking attempts. Quarantined; never written to the live tables.",
    types: ["tracking_after_offboarding"],
  },
  {
    key: "audit",
    label: "Audit failures",
    icon: <FileWarning size={14} />,
    description: "Hash-chain integrity errors, audit-write failures.",
    types: ["audit_failure"],
  },
  {
    key: "legal",
    label: "Legal hold violations",
    icon: <AlertTriangle size={14} />,
    description: "Deletion attempts against a held record, former-worker abuse.",
    types: ["legal_hold_deletion_attempt", "former_worker_abuse"],
  },
];

export default function SecurityDashboards() {
  const { C } = useTheme();
  const [tab, setTab] = useState(QUEUES[0].key);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <Surface padding="20px 24px">
        <SectionHeading
          title="Security queues"
          description="Live view of every incident the enforcement layer has raised. Critical incidents pulse — they're invariant breaches, not warnings."
        />
        <div
          role="tablist"
          aria-label="Security queues"
          style={{ display: "flex", flexWrap: "wrap", gap: 8 }}
        >
          {QUEUES.map((q) => {
            const active = tab === q.key;
            return (
              <button
                key={q.key}
                role="tab"
                aria-selected={active}
                onClick={() => setTab(q.key)}
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
                  display: "inline-flex", alignItems: "center", gap: 6,
                  padding: "8px 14px", borderRadius: 999,
                  fontSize: 12, fontWeight: 700, cursor: "pointer",
                  fontFamily: "inherit", transition: "all .18s ease",
                  background: active ? C.brand : C.surface,
                  color: active ? "#fff" : C.textSub,
                  border: `1.5px solid ${active ? C.brand : C.border}`,
                }}
              >
                <span style={{ display: "inline-flex" }} aria-hidden="true">{q.icon}</span>
                {q.label}
              </button>
            );
          })}
        </div>
      </Surface>

      {QUEUES.filter((q) => q.key === tab).map((q) => (
        <Queue key={q.key} queue={q} />
      ))}
    </div>
  );
}

function Queue({ queue }) {
  const { C } = useTheme();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = async () => {
    setLoading(true); setError(null);
    try {
      const data = await listSecurityEvents({ limit: 200 });
      const filtered = (data.incidents || []).filter((i) => queue.types.includes(i.incident_type));
      setRows(filtered);
    } catch (e) {
      setError(e?.response?.data?.detail?.message || e?.message || "Failed to load");
    } finally { setLoading(false); }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [queue.key]);

  return (
    <Surface>
      <SectionHeading
        title={queue.label}
        description={queue.description}
        right={<ActionButton variant="subtle" size="sm" onClick={load}>Refresh</ActionButton>}
      />

      {loading && <div style={{ fontSize: 13, color: C.textSub }} aria-live="polite">Loading…</div>}
      {!loading && error && <div style={{ fontSize: 13, color: C.red }}>{String(error)}</div>}
      {!loading && !error && rows.length === 0 && (
        <EmptyState title="Queue is clear" hint="No open incidents in this category." />
      )}

      {!loading && !error && rows.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {rows.map((i) => (
            <article
              key={i.id}
              aria-labelledby={`inc-${i.id}-title`}
              style={{
                padding: 12, borderRadius: C.r,
                border: `1px solid ${C.border}`, background: C.surface,
              }}
            >
              <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                  <SeverityDot severity={i.severity} />
                  <span id={`inc-${i.id}-title`} style={{ fontSize: 13, fontWeight: 700, color: C.text }}>{i.title}</span>
                  <Tag>{i.reference}</Tag>
                  <Tag>{i.severity}</Tag>
                  <Tag>{i.status}</Tag>
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  {i.case_id && queue.key === "revocation_failures" && (
                    <ActionButton variant="secondary" size="sm" onClick={async () => {
                      try { await retryRevocation(i.case_id); toast.success("Retry triggered"); load(); }
                      catch (e) { toast.error(e?.response?.data?.detail?.message || "Failed"); }
                    }}>Retry</ActionButton>
                  )}
                </div>
              </div>
              <div style={{ marginTop: 6, fontSize: 11, color: C.textSub }}>
                worker #{i.worker_id ?? "—"} · case {i.case_id?.slice(0, 12) || "—"}… · {i.occurrences} occurrence{i.occurrences === 1 ? "" : "s"}
              </div>
              {i.diagnostics && Object.keys(i.diagnostics).length > 0 && (
                <details style={{ marginTop: 8, fontSize: 11 }}>
                  <summary style={{ cursor: "pointer", color: C.textSub, fontWeight: 600 }}>Diagnostics</summary>
                  <pre
                    style={{
                      marginTop: 6, maxHeight: 160, overflow: "auto",
                      borderRadius: C.rSm, padding: 8,
                      background: C.surfaceAlt, color: C.textSub, fontSize: 11,
                    }}
                  >
                    {JSON.stringify(i.diagnostics, null, 2)}
                  </pre>
                </details>
              )}
            </article>
          ))}
        </div>
      )}

      {queue.key === "identity_conflicts" && <ReconciliationQueue />}
    </Surface>
  );
}

function Tag({ children }) {
  const { C } = useTheme();
  return (
    <span style={{
      padding: "2px 8px", borderRadius: 6,
      fontSize: 10, fontWeight: 700,
      background: C.surfaceAlt, color: C.textSub,
      border: `1px solid ${C.border}`,
    }}>
      {children}
    </span>
  );
}

function ReconciliationQueue() {
  const { C } = useTheme();
  const [data, setData] = useState({ queue: [], conflicts: [], queue_total: 0 });
  useEffect(() => {
    listReconciliation({ limit: 100 }).then(setData).catch(() => {});
  }, []);
  return (
    <div style={{ marginTop: 16 }}>
      <h4 style={{
        margin: "0 0 8px", fontSize: 11, fontWeight: 800,
        textTransform: "uppercase", letterSpacing: "0.6px", color: C.textLight,
      }}>
        Reconciliation queue ({data.queue_total ?? 0})
      </h4>
      {data.queue.length === 0
        ? <EmptyState title="Queue is empty" />
        : (
          <div style={{ overflowX: "auto", borderRadius: C.r, border: `1px solid ${C.border}` }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
              <thead>
                <tr style={{ background: C.surfaceAlt }}>
                  {["Integration", "Status", "Attempts", "Next attempt", "Last error"].map((h) => (
                    <th
                      key={h}
                      style={{
                        padding: "8px 12px", textAlign: "left",
                        fontSize: 10, fontWeight: 700, color: C.textSub,
                        textTransform: "uppercase", letterSpacing: "0.5px",
                        borderBottom: `1px solid ${C.border}`,
                      }}
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.queue.map((i, idx) => (
                  <tr key={i.id} style={{ borderTop: idx === 0 ? "none" : `1px solid ${C.borderLight}` }}>
                    <td style={{ padding: "8px 12px", fontWeight: 600, color: C.text }}>{i.integration}</td>
                    <td style={{ padding: "8px 12px", color: C.text }}>{i.status}</td>
                    <td style={{ padding: "8px 12px", color: C.text }}>{i.attempts}/{i.max_attempts}</td>
                    <td style={{ padding: "8px 12px", color: C.textSub }}>{i.next_attempt_at ? new Date(i.next_attempt_at).toLocaleString() : "—"}</td>
                    <td style={{ padding: "8px 12px", color: C.red }}>{i.last_error || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
    </div>
  );
}

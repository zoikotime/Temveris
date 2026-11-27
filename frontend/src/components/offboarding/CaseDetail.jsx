/**
 * Case Detail — 10 tabs covering every lifecycle surface for a single case.
 *
 * Visual treatment matches the rest of the admin module suite — surface
 * cards, brand-coloured action buttons, themed tabs.
 */
import React, { useEffect, useMemo, useState } from "react";
import {
  ArrowLeft, Banknote, FileCheck, Folder, Lock, Receipt,
  Scale, ShieldCheck, ScrollText, UserCheck, FileSearch,
} from "lucide-react";
import toast from "react-hot-toast";

import {
  approveCase, beginCase, cancelCase, closeAccess, closeBilling, closePayroll,
  completeCase, finalReview, generateCert, getAudit, getCase,
  retryAccess, setupFormerAccess,
  submitCase, unblockCase,
  applyLegalHold, releaseLegalHold, assignRetention, listPolicies,
  openDeletionReview, approveDeletionReview, denyDeletionReview, listDeletionReviews,
  listEvidencePackages, buildLegalEvidence, buildAuditEvidence,
  getCertificate,
} from "../../api/offboarding";
import {
  ActionButton, BlockerChip, DataRow, EmptyState, RedactedField, RiskBadge,
  SectionHeading, SLAIndicator, StatusChip, Surface,
} from "./primitives";
import useDirectory from "./useDirectory";
import Gate from "../../entitlements/Gate";
import { useTheme } from "../../context/ThemeContext";

const TABS = [
  { key: "overview",    label: "Overview",              icon: <FileSearch size={14} /> },
  { key: "access",      label: "Access Closure",        icon: <Lock size={14} /> },
  { key: "work_review", label: "Final Work Review",     icon: <FileCheck size={14} /> },
  { key: "payroll",     label: "Payroll Closure",       icon: <Banknote size={14} /> },
  { key: "billing",     label: "Billing Closure",       icon: <Receipt size={14} /> },
  { key: "former",      label: "Former Worker Access",  icon: <UserCheck size={14} /> },
  { key: "legal",       label: "Legal Hold & Retention", icon: <Scale size={14} /> },
  { key: "documents",   label: "Documents",             icon: <Folder size={14} /> },
  { key: "audit",       label: "Audit Trail",           icon: <ScrollText size={14} /> },
  { key: "certificate", label: "Completion Certificate", icon: <ShieldCheck size={14} /> },
];

export default function CaseDetail({ caseId, onBack }) {
  const { C } = useTheme();
  const [data, setData] = useState(null);
  const [tab, setTab] = useState("overview");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const { map: directoryMap } = useDirectory();
  const worker = data?.worker_id != null ? directoryMap.get(Number(data.worker_id)) : null;

  const reload = async () => {
    setError(null);
    try { setData(await getCase(caseId)); }
    catch (e) { setError(e?.response?.data?.detail || e.message); }
  };

  useEffect(() => { reload(); /* eslint-disable-next-line */ }, [caseId]);

  const run = async (label, fn) => {
    setBusy(true);
    try {
      await fn();
      toast.success(label);
      await reload();
    } catch (e) {
      const d = e?.response?.data?.detail;
      toast.error(typeof d === "string" ? d : d?.message || d?.detail || "Action failed");
    } finally {
      setBusy(false);
    }
  };

  if (error) {
    return (
      <Surface style={{ borderColor: C.redBdr, background: C.redBg }}>
        <p style={{ marginBottom: 12, fontSize: 13, color: C.red }} role="alert">
          {typeof error === "string" ? error : (error?.message || JSON.stringify(error))}
        </p>
        <div style={{ display: "flex", gap: 8 }}>
          <ActionButton variant="secondary" size="sm" onClick={reload} aria-label="Retry loading case">Retry</ActionButton>
          <ActionButton variant="subtle" size="sm" onClick={onBack}>
            <ArrowLeft size={13} /> Back
          </ActionButton>
        </div>
      </Surface>
    );
  }

  if (!data) {
    return (
      <Surface>
        <div style={{ fontSize: 13, color: C.textSub }} aria-live="polite">Loading case…</div>
      </Surface>
    );
  }

  const allowed = new Set(data.allowed_actions || []);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <button
        type="button"
        onClick={onBack}
        style={{
          display: "inline-flex", alignItems: "center", gap: 4,
          padding: "4px 8px", borderRadius: C.rSm,
          background: "transparent", border: "none",
          color: C.textSub, fontSize: 13, fontWeight: 600,
          cursor: "pointer", fontFamily: "inherit",
          alignSelf: "flex-start",
        }}
        onMouseEnter={(e) => { e.currentTarget.style.background = C.surfaceAlt; }}
        onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}
      >
        <ArrowLeft size={14} aria-hidden="true" /> Back to register
      </button>

      {/* Header card */}
      <Surface>
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "flex-start", justifyContent: "space-between", gap: 12 }}>
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
              <h1 style={{ margin: 0, fontSize: 22, fontWeight: 800, color: C.text }}>{data.engagement_id}</h1>
              <StatusChip status={data.status} />
              {data.risk_level && <RiskBadge risk={data.risk_level} />}
              <BlockerChip count={(data.blockers || []).filter((b) => b.status === "open").length} />
            </div>
            <p style={{ margin: "6px 0 0", fontSize: 12, color: C.textSub }}>
              {worker
                ? <><span style={{ fontWeight: 700, color: C.text }}>{worker.name || worker.email}</span> · #{data.worker_id}</>
                : <>Worker #{data.worker_id ?? "—"}</>}
              {" · "}
              {data.department || "no department"} · {data.jurisdiction || "no jurisdiction"} · correlation {data.correlation_id}
            </p>
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
            {data.status === "draft" && (
              <ActionButton onClick={() => run("Submitted", () => submitCase(caseId))} disabled={busy}>
                Submit for approval
              </ActionButton>
            )}
            {data.status === "pending_approval" && (
              <ActionButton onClick={() => run("Approved", () => approveCase(caseId, { schedule: !!data.final_access_at }))} disabled={busy}>
                Approve
              </ActionButton>
            )}
            {data.status === "scheduled" && allowed.has("in_progress") && (
              <ActionButton onClick={() => run("Started", () => beginCase(caseId))} disabled={busy}>
                Begin offboarding
              </ActionButton>
            )}
            {data.status === "in_progress" && (
              <ActionButton onClick={() => run("Access closure started", () => closeAccess(caseId))} disabled={busy}>
                Close access
              </ActionButton>
            )}
            {data.status === "access_revocation_failed" && (
              <ActionButton variant="danger" onClick={() => run("Retried", () => retryAccess(caseId))} disabled={busy}>
                Retry access closure
              </ActionButton>
            )}
            {data.status === "blocked" && (
              <ActionButton onClick={() => run("Unblocked", () => unblockCase(caseId))} disabled={busy}>
                Unblock
              </ActionButton>
            )}
            {!["completed", "cancelled", "reversed"].includes(data.status) && (
              <ActionButton variant="subtle" size="sm" onClick={() => {
                const r = window.prompt("Cancel reason?");
                if (r) run("Cancelled", () => cancelCase(caseId, r));
              }} disabled={busy}>
                Cancel case
              </ActionButton>
            )}
          </div>
        </div>
      </Surface>

      {/* Tabs */}
      <Surface padding="0">
        <div
          role="tablist"
          aria-label="Case sections"
          style={{
            display: "flex", flexWrap: "wrap", alignItems: "center",
            gap: 2, borderBottom: `1px solid ${C.border}`,
            padding: "0 12px",
            overflowX: "auto",
          }}
        >
          {TABS.map((t) => {
            const active = tab === t.key;
            return (
              <button
                key={t.key}
                role="tab"
                aria-selected={active}
                aria-controls={`tabpanel-${t.key}`}
                id={`tab-${t.key}`}
                onClick={() => setTab(t.key)}
                style={{
                  display: "inline-flex", alignItems: "center", gap: 6,
                  padding: "12px 14px",
                  borderBottom: active ? `2px solid ${C.brand}` : "2px solid transparent",
                  background: "transparent",
                  color: active ? C.brand : C.textSub,
                  fontWeight: active ? 800 : 600, fontSize: 13,
                  cursor: "pointer", fontFamily: "inherit",
                  transition: "color .18s, border-color .18s",
                  whiteSpace: "nowrap",
                }}
                onMouseEnter={(e) => { if (!active) e.currentTarget.style.color = C.text; }}
                onMouseLeave={(e) => { if (!active) e.currentTarget.style.color = C.textSub; }}
              >
                <span style={{ display: "inline-flex" }} aria-hidden="true">{t.icon}</span>
                {t.label}
              </button>
            );
          })}
        </div>

        <div
          id={`tabpanel-${tab}`}
          role="tabpanel"
          aria-labelledby={`tab-${tab}`}
          style={{ padding: 20 }}
        >
          {tab === "overview" && <OverviewTab data={data} worker={worker} />}
          {tab === "access" && <AccessClosureTab data={data} />}
          {tab === "work_review" && <SimpleAdvanceTab
              data={data}
              expectedStatus="final_review_pending"
              actionLabel="Mark final work review complete"
              onAct={() => run("Final review complete", () => finalReview(caseId))}
              busy={busy}
              description="Manager-led sign-off that the worker's final work is reviewed and accepted."
            />}
          {tab === "payroll" && <SimpleAdvanceTab
              data={data}
              expectedStatus="payroll_closure_pending"
              actionLabel="Close payroll"
              onAct={() => run("Payroll closed", () => closePayroll(caseId, {}))}
              busy={busy}
              description="Payroll-team confirmation. Produces a Payroll Evidence package."
            />}
          {tab === "billing" && <SimpleAdvanceTab
              data={data}
              expectedStatus="billing_closure_pending"
              actionLabel="Close billing"
              onAct={() => run("Billing closed", () => closeBilling(caseId, {}))}
              busy={busy}
              description="Finance team confirmation. Produces a Billing Evidence package."
            />}
          {tab === "former" && <FormerWorkerTab
              data={data}
              onSetup={() => run("Former-worker access granted", () => setupFormerAccess(caseId))}
              busy={busy}
            />}
          {/* Legal hold / retention / deletion-review is the Sovereign+ legal-hold
              governance surface (offboarding.legal_hold; evidence.legal_hold
              co-applies — identical presentation). Standard offboarding tabs
              (overview / access closure / former worker) stay All-plan. */}
          {tab === "legal" && (
            <Gate capability="offboarding.legal_hold">
              <LegalHoldRetentionTab caseId={caseId} reloadCase={reload} />
            </Gate>
          )}
          {tab === "documents" && <DocumentsTab caseId={caseId} />}
          {tab === "audit" && <AuditTab caseId={caseId} />}
          {tab === "certificate" && <CertificateTab
              caseId={caseId}
              onGenerate={() => run("Certificate generated", () => generateCert(caseId))}
              onComplete={() => run("Case completed", () => completeCase(caseId))}
              data={data} busy={busy}
            />}
        </div>
      </Surface>
    </div>
  );
}

// ─── Tab bodies ────────────────────────────────────────────────────────────

function OverviewTab({ data, worker }) {
  const { C } = useTheme();
  return (
    <dl style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", columnGap: 32, rowGap: 4, margin: 0 }}>
      <DataRow label="Engagement ID">{data.engagement_id}</DataRow>
      <DataRow label="Worker">
        {data.worker_id == null ? "—" : (
          <span>
            {worker?.name || worker?.email || `Worker #${data.worker_id}`}
            <span style={{ marginLeft: 6, fontSize: 11, color: C.textSub }}>#{data.worker_id}</span>
            {worker?.email && <div style={{ fontSize: 11, color: C.textSub, marginTop: 2 }}>{worker.email}</div>}
          </span>
        )}
      </DataRow>
      <DataRow label="Status"><StatusChip status={data.status} /></DataRow>
      <DataRow label="Risk level">{data.risk_level && <RiskBadge risk={data.risk_level} />}</DataRow>
      <DataRow label="Department">{data.department || "—"}</DataRow>
      <DataRow label="Jurisdiction">{data.jurisdiction || "—"}</DataRow>
      <DataRow label="Exit type">{data.exit_type || "—"}</DataRow>
      <DataRow label="Revocation mode">{data.revocation_mode}</DataRow>
      <DataRow label="Final access">
        {data.final_access_at ? new Date(data.final_access_at).toLocaleString() : "—"}
        {data.final_access_at && <span style={{ marginLeft: 8 }}><SLAIndicator dueAt={data.final_access_at} label="Cutover" /></span>}
      </DataRow>
      <DataRow label="Legal hold">{data.legal_hold_status}</DataRow>
      <DataRow label="Termination reason">
        <RedactedField value={data.termination_reason} fallback="—" />
      </DataRow>
      <DataRow label="Notes">{data.notes || "—"}</DataRow>
    </dl>
  );
}

function AccessClosureTab({ data }) {
  const { C } = useTheme();
  const revs = data.revocations || [];
  return (
    <div>
      <SectionHeading
        title="Access closure"
        description="Per-component revocation outcome. Each surface (login, sessions, tokens, SSO, SCIM, …) is recorded independently."
      />
      {revs.length === 0 && <EmptyState title="No revocations recorded yet" hint="Trigger Close access from the header." />}
      {revs.length > 0 && (
        <div style={{ overflowX: "auto", borderRadius: C.r, border: `1px solid ${C.border}` }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ background: C.surfaceAlt }}>
                {["Component", "Status", "Attempts", "Revoked at", "Last error"].map((h) => (
                  <th
                    key={h}
                    style={{
                      padding: "10px 14px", textAlign: "left",
                      fontSize: 11, fontWeight: 700, color: C.textSub,
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
              {revs.map((r, i) => (
                <tr key={r.component} style={{ borderTop: i === 0 ? "none" : `1px solid ${C.borderLight}` }}>
                  <td style={{ padding: "10px 14px", fontFamily: "ui-monospace, monospace", fontSize: 11, color: C.textSub }}>{r.component}</td>
                  <td style={{ padding: "10px 14px" }}><RevocationStatus s={r.status} /></td>
                  <td style={{ padding: "10px 14px", color: C.text }}>{r.attempts}</td>
                  <td style={{ padding: "10px 14px", color: C.textSub }}>{r.revoked_at ? new Date(r.revoked_at).toLocaleString() : "—"}</td>
                  <td style={{ padding: "10px 14px", fontSize: 11, color: C.red }}>{r.last_error || ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function RevocationStatus({ s }) {
  const { C } = useTheme();
  const tones = {
    revoked:        { bg: C.greenBg, fg: C.green,  bdr: C.greenBdr },
    pending:        { bg: C.amberBg, fg: C.amber,  bdr: C.amberBdr },
    failed:         { bg: C.redBg,   fg: C.red,    bdr: C.redBdr },
    not_applicable: { bg: C.surfaceAlt, fg: C.textSub, bdr: C.border },
    reversed:       { bg: C.surfaceAlt, fg: C.textSub, bdr: C.border },
  };
  const t = tones[s] || tones.pending;
  return (
    <span style={{
      display: "inline-flex", alignItems: "center",
      padding: "3px 10px", borderRadius: 999,
      fontSize: 11, fontWeight: 700,
      background: t.bg, color: t.fg, border: `1px solid ${t.bdr}`,
    }}>
      {s}
    </span>
  );
}

function SimpleAdvanceTab({ data, expectedStatus, actionLabel, onAct, busy, description }) {
  const { C } = useTheme();
  const ready = data.status === expectedStatus;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <p style={{ margin: 0, fontSize: 13, color: C.textSub }}>{description}</p>
      {ready
        ? <div><ActionButton onClick={onAct} disabled={busy}>{actionLabel}</ActionButton></div>
        : <EmptyState title={`Not actionable in ${data.status}`} hint="The case must reach this step in the lifecycle first." />}
    </div>
  );
}

function FormerWorkerTab({ data, onSetup, busy }) {
  const { C } = useTheme();
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <p style={{ margin: 0, fontSize: 13, color: C.textSub }}>
        Granting former-worker access mints a one-time portal token. The raw token is shown ONCE
        in the issuing response — copy it then.
      </p>
      <div>
        <ActionButton onClick={onSetup} disabled={busy || data.status !== "former_access_pending"}>
          Set up former-worker access
        </ActionButton>
      </div>
      {data.status !== "former_access_pending" && (
        <EmptyState title={`Not actionable in ${data.status}`} hint="The case must reach Former access pending first." />
      )}
    </div>
  );
}

function LegalHoldRetentionTab({ caseId, reloadCase }) {
  const { C } = useTheme();
  const [matter, setMatter] = useState("");
  const [reason, setReason] = useState("");
  const [policy, setPolicy] = useState("");
  const [policies, setPolicies] = useState([]);
  const [reviews, setReviews] = useState([]);
  const [busy, setBusy] = useState(false);

  const refresh = async () => {
    try {
      setPolicies(await listPolicies());
      setReviews(await listDeletionReviews({ case_id: caseId }));
    } catch (e) {
      const d = e?.response?.data?.detail;
      toast.error(typeof d === "string" ? d : d?.message || "Failed to load policies");
    }
  };
  useEffect(() => { refresh(); /* eslint-disable-next-line */ }, [caseId]);

  const apply = async () => {
    setBusy(true);
    try { await applyLegalHold({ case_id: caseId, matter_ref: matter, reason }); toast.success("Legal hold applied"); reloadCase(); refresh(); }
    catch (e) { toast.error(e?.response?.data?.detail?.message || e?.response?.data?.detail || "Failed"); }
    finally { setBusy(false); }
  };
  const assign = async () => {
    setBusy(true);
    try { await assignRetention({ case_id: caseId, policy_code: policy || undefined }); toast.success("Retention assigned"); reloadCase(); }
    catch (e) { toast.error(e?.response?.data?.detail?.message || "Failed"); }
    finally { setBusy(false); }
  };
  const openReview = async () => {
    setBusy(true);
    try { await openDeletionReview({ case_id: caseId }); toast.success("Deletion review opened"); refresh(); }
    catch (e) { toast.error(e?.response?.data?.detail?.message || "Failed"); }
    finally { setBusy(false); }
  };
  const approve = async (reviewId, role) => {
    setBusy(true);
    try { await approveDeletionReview(reviewId, { role }); toast.success(`Approved as ${role}`); refresh(); }
    catch (e) {
      const d = e?.response?.data?.detail;
      if (d?.code === "deletion_blocked") {
        toast.error(`Blocked: ${d.blockers.map((b) => b.type).join(", ")}`);
      } else {
        toast.error(d?.message || "Failed");
      }
    } finally { setBusy(false); }
  };

  const input = {
    padding: "9px 12px", borderRadius: C.rSm,
    border: `1.5px solid ${C.border}`, background: C.bg,
    color: C.text, fontSize: 13, fontFamily: "inherit", outline: "none",
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
      <section>
        <SectionHeading title="Legal hold" description="Active holds block destructive lifecycle steps and deletion." />
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", gap: 10 }}>
          <input value={matter} onChange={(e) => setMatter(e.target.value)} placeholder="Matter ref" style={input} />
          <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Reason (sensitive — redacted)" style={input} />
          <div><ActionButton onClick={apply} disabled={busy}>Apply legal hold</ActionButton></div>
        </div>
      </section>

      <section>
        <SectionHeading title="Retention policy" description="Versioned + jurisdiction-aware. Picked policy is snapshot onto the case." />
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
          <select value={policy} onChange={(e) => setPolicy(e.target.value)} style={{ ...input, cursor: "pointer" }}>
            <option value="">Jurisdiction default</option>
            {policies.map((p) => <option key={p.id} value={p.code}>{p.code} v{p.version} ({p.retention_days}d)</option>)}
          </select>
          <ActionButton onClick={assign} disabled={busy}>Assign retention</ActionButton>
        </div>
      </section>

      <section>
        <SectionHeading
          title="Deletion eligibility review"
          description="Dual approval required: Legal AND Compliance, with eligibility re-checked at decision time."
          right={<ActionButton variant="secondary" size="sm" onClick={openReview} disabled={busy}>Open review</ActionButton>}
        />
        {reviews.length === 0 && <EmptyState title="No deletion reviews on this case." />}
        {reviews.map((r) => (
          <div
            key={r.id}
            style={{
              marginBottom: 10, padding: 12,
              borderRadius: C.r, border: `1px solid ${C.border}`,
              background: C.surface, fontSize: 13,
            }}
          >
            <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
              <div>
                <span style={{ fontFamily: "ui-monospace, monospace", fontSize: 11, color: C.textSub }}>{r.id.slice(0, 12)}…</span>{" "}
                <span style={{ fontWeight: 700, color: C.text }}>{r.status}</span>{" "}
                <span style={{ color: C.textSub }}>— missing: {r.missing_approvers.length ? r.missing_approvers.join(", ") : "—"}</span>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                {r.status === "pending" && (
                  <>
                    <ActionButton variant="secondary" size="sm" onClick={() => approve(r.id, "legal")}>Approve (Legal)</ActionButton>
                    <ActionButton variant="secondary" size="sm" onClick={() => approve(r.id, "compliance")}>Approve (Compliance)</ActionButton>
                    <ActionButton variant="danger" size="sm" onClick={() => {
                      const why = window.prompt("Denial reason?"); if (why) denyDeletionReview(r.id, why).then(refresh);
                    }}>Deny</ActionButton>
                  </>
                )}
              </div>
            </div>
            {(r.blockers || []).length > 0 && (
              <ul style={{ marginTop: 8, paddingLeft: 20, fontSize: 11, color: C.amber }}>
                {r.blockers.map((b, i) => (
                  <li key={i}>{b.type}{b.detail ? ` — ${JSON.stringify(b.detail).slice(0, 120)}` : ""}</li>
                ))}
              </ul>
            )}
          </div>
        ))}
      </section>
    </div>
  );
}

function DocumentsTab({ caseId }) {
  const { C } = useTheme();
  const [pkgs, setPkgs] = useState([]);
  const [busy, setBusy] = useState(false);
  const load = async () => {
    try { setPkgs(await listEvidencePackages(caseId)); }
    catch (e) { toast.error(e?.response?.data?.detail?.message || "Failed to load packages"); }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [caseId]);

  const buildLegal = async () => {
    setBusy(true);
    try { await buildLegalEvidence(caseId); toast.success("Legal evidence built"); load(); }
    catch (e) { toast.error(e?.response?.data?.detail?.message || "Failed"); }
    finally { setBusy(false); }
  };
  const buildAudit = async () => {
    setBusy(true);
    try { await buildAuditEvidence(caseId); toast.success("Audit evidence built"); load(); }
    catch (e) { toast.error(e?.response?.data?.detail?.message || "Failed"); }
    finally { setBusy(false); }
  };

  return (
    <div>
      <SectionHeading
        title="Evidence packages"
        description="Hashed, immutable bundles. Click a row to copy the content hash."
        right={
          <>
            <ActionButton variant="secondary" size="sm" onClick={buildLegal} disabled={busy}>Build legal pack</ActionButton>
            <ActionButton variant="secondary" size="sm" onClick={buildAudit} disabled={busy}>Build audit pack</ActionButton>
          </>
        }
      />
      {pkgs.length === 0 && <EmptyState title="No evidence packages yet" hint="Closure steps emit packages automatically; you can also build legal/audit packs manually." />}
      {pkgs.length > 0 && (
        <div style={{ overflowX: "auto", borderRadius: C.r, border: `1px solid ${C.border}` }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ background: C.surfaceAlt }}>
                {["Type", "Content hash", "Created"].map((h) => (
                  <th
                    key={h}
                    style={{
                      padding: "10px 14px", textAlign: "left",
                      fontSize: 11, fontWeight: 700, color: C.textSub,
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
              {pkgs.map((p, i) => (
                <tr
                  key={p.id}
                  tabIndex={0}
                  onClick={() => { navigator.clipboard?.writeText(p.content_hash); toast.success("Hash copied"); }}
                  onKeyDown={(e) => { if (e.key === "Enter") { navigator.clipboard?.writeText(p.content_hash); toast.success("Hash copied"); } }}
                  onMouseEnter={(e) => { e.currentTarget.style.background = C.surfaceAlt; }}
                  onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}
                  style={{
                    cursor: "pointer",
                    borderTop: i === 0 ? "none" : `1px solid ${C.borderLight}`,
                    transition: "background .15s",
                  }}
                >
                  <td style={{ padding: "10px 14px", color: C.text }}>{p.package_type}</td>
                  <td style={{ padding: "10px 14px", fontFamily: "ui-monospace, monospace", fontSize: 11, color: C.textSub }}>{p.content_hash}</td>
                  <td style={{ padding: "10px 14px", color: C.textSub }}>{new Date(p.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function AuditTab({ caseId }) {
  const { C } = useTheme();
  const [rows, setRows] = useState([]);
  useEffect(() => {
    getAudit(caseId).then(setRows).catch((e) => toast.error(e?.response?.data?.detail?.message || "Failed"));
  }, [caseId]);
  return (
    <div>
      <SectionHeading
        title="Audit trail"
        description="Append-only, hash-chained. Every row carries the prior hash; tampering with any row breaks every subsequent hash."
      />
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {rows.length === 0 && <EmptyState title="No audit rows yet" />}
        {rows.map((a) => (
          <div
            key={a.id}
            style={{
              padding: 10, borderRadius: C.rSm,
              border: `1px solid ${C.borderLight}`, background: C.surfaceAlt,
              fontSize: 11,
            }}
          >
            <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
              <span style={{ fontWeight: 700, color: C.text, fontSize: 12 }}>{a.action}</span>
              <span style={{ color: C.textSub }}>{a.created_at ? new Date(a.created_at).toLocaleString() : ""}</span>
            </div>
            <div style={{ marginTop: 4, color: C.textSub }}>
              by actor #{a.actor_id ?? "—"} ({a.actor_role || "—"})
            </div>
            <div style={{ marginTop: 4, fontFamily: "ui-monospace, monospace", fontSize: 10, color: C.textLight }}>
              hash {a.current_hash?.slice(0, 16)}…
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function CertificateTab({ caseId, onGenerate, onComplete, data, busy }) {
  const { C } = useTheme();
  const [cert, setCert] = useState(null);
  const [missing, setMissing] = useState(false);
  const load = async () => {
    try { setCert(await getCertificate(caseId)); setMissing(false); }
    catch (e) {
      if (e?.response?.status === 404) setMissing(true);
      else toast.error(e?.response?.data?.detail?.message || "Failed to load certificate");
    }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [caseId]);

  return (
    <div>
      <SectionHeading
        title="Completion certificate"
        description="Immutable snapshot signed at completion. SHA-256 of the canonical JSON is stored as content_hash."
        right={
          <>
            <ActionButton variant="secondary" size="sm" onClick={onGenerate} disabled={busy}>Generate</ActionButton>
            {data.status === "former_access_pending" && (
              <ActionButton onClick={onComplete} disabled={busy}>Complete case</ActionButton>
            )}
          </>
        }
      />
      {missing && <EmptyState title="No certificate generated yet" hint="Generate one once readiness shows everything closed." />}
      {cert && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6, fontSize: 13 }}>
          <DataRow label="Certificate number">{cert.certificate_number}</DataRow>
          <DataRow label="Content hash">
            <span style={{ fontFamily: "ui-monospace, monospace", fontSize: 11, color: C.text }}>{cert.content_hash}</span>
          </DataRow>
          <DataRow label="Issued">{new Date(cert.issued_at).toLocaleString()}</DataRow>
          <details
            style={{
              padding: 10, borderRadius: C.rSm,
              border: `1px solid ${C.border}`, background: C.surfaceAlt,
              fontSize: 11,
            }}
          >
            <summary style={{ cursor: "pointer", fontWeight: 700, color: C.text }}>View signed payload</summary>
            <pre style={{ marginTop: 8, maxHeight: 420, overflow: "auto", fontSize: 11, color: C.textSub }}>
              {JSON.stringify(cert.payload, null, 2)}
            </pre>
          </details>
        </div>
      )}
    </div>
  );
}

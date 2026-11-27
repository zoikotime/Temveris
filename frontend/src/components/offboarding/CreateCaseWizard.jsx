/**
 * Create Case Wizard — 7 steps + inline warnings.
 *
 * The frontend's job here is to gather a clean payload and SURFACE warnings
 * before posting. The backend is authoritative for every gate (duplicate
 * active case, invalid transition, RBAC); the wizard just makes those
 * outcomes visible early to avoid round-trip frustration.
 *
 * Visual treatment matches the admin modal pattern used by Add User /
 * Edit Employee / Date Filter pickers on AdminDashboard — same surface,
 * border radius, stepper styling, brand-gradient primary CTA.
 */
import React, { useMemo, useState } from "react";
import toast from "react-hot-toast";
import {
  AlertTriangle, ArrowLeft, ArrowRight, CheckCircle2, Search, UserCheck, X,
} from "lucide-react";
import { createCase, listCases } from "../../api/offboarding";
import { ActionButton } from "./primitives";
import { useTheme } from "../../context/ThemeContext";
import useDirectory from "./useDirectory";

const EXIT_TYPES = [
  { value: "voluntary",       label: "Voluntary (resignation)" },
  { value: "involuntary",     label: "Involuntary (termination for cause)" },
  { value: "redundancy",      label: "Redundancy / layoff" },
  { value: "end_of_contract", label: "End of contract" },
  { value: "retirement",      label: "Retirement" },
  { value: "deceased",        label: "Deceased" },
];

const REVOCATION_MODES = [
  { value: "scheduled", label: "Scheduled — revoke at final access time" },
  { value: "immediate", label: "Immediate — revoke the moment approval lands" },
  { value: "graceful",  label: "Graceful — revoke at final access + grace window" },
];

const RISK_LEVELS = ["low", "standard", "elevated", "high", "critical"];

const STEPS = [
  "Worker", "Exit type", "Final access", "Security mode", "Reviews",
  "Former-worker setup", "Review & submit",
];

const initial = {
  worker_id: "",
  engagement_id: "",
  exit_type: "voluntary",
  final_access_at: "",
  revocation_mode: "scheduled",
  risk_level: "standard",
  department: "",
  jurisdiction: "",
  data_classification: "internal",
  termination_reason: "",
  notes: "",
  alternate_email: "",
  expires_in_days: 365,
};

export default function CreateCaseWizard({ onClose, onCreated }) {
  const { C } = useTheme();
  const [step, setStep] = useState(0);
  const [v, setV] = useState(initial);
  const [submitting, setSubmitting] = useState(false);
  const [duplicateHit, setDuplicateHit] = useState(false);
  const { employees, departments, labelFor, loading: dirLoading, error: dirError } = useDirectory();

  const set = (patch) => setV((s) => ({ ...s, ...patch }));

  const pickWorker = (emp) => {
    if (!emp) {
      set({ worker_id: "" });
      return;
    }
    set({
      worker_id: String(emp.id),
      engagement_id: v.engagement_id || emp.employee_id || `WORKER-${emp.id}`,
      department: v.department || emp.department || "",
      alternate_email: v.alternate_email || emp.email || "",
    });
  };

  const warnFinalInPast = useMemo(() => {
    if (!v.final_access_at) return false;
    return new Date(v.final_access_at).getTime() < Date.now();
  }, [v.final_access_at]);

  const warnMissingAltEmail = step === 5 && !v.alternate_email?.includes("@");

  const canNext = useMemo(() => {
    if (step === 0) return v.engagement_id.trim().length > 0;
    if (step === 1) return EXIT_TYPES.some((e) => e.value === v.exit_type);
    return true;
  }, [step, v]);

  const submit = async () => {
    setSubmitting(true);
    setDuplicateHit(false);
    try {
      try {
        const reg = await listCases({ engagement_id: v.engagement_id, limit: 1 });
        if (reg?.items?.length) setDuplicateHit(true);
      } catch { /* ignore — proceed to real submit */ }

      const payload = {
        engagement_id: v.engagement_id,
        worker_id: v.worker_id ? Number(v.worker_id) : undefined,
        exit_type: v.exit_type,
        final_access_at: v.final_access_at ? new Date(v.final_access_at).toISOString() : undefined,
        revocation_mode: v.revocation_mode,
        risk_level: v.risk_level,
        department: v.department || undefined,
        jurisdiction: v.jurisdiction || undefined,
        data_classification: v.data_classification,
        termination_reason: v.termination_reason || undefined,
        notes: v.notes || undefined,
      };
      const created = await createCase(payload);
      toast.success(`Case ${created.engagement_id} created`);
      onCreated?.(created);
    } catch (e) {
      const detail = e?.response?.data?.detail;
      const status = e?.response?.status;
      if (status === 409 && (detail?.code === "duplicate_active_case" || JSON.stringify(detail).includes("duplicate"))) {
        setDuplicateHit(true);
        toast.error("An active case already exists for this engagement.");
      } else {
        toast.error(typeof detail === "string" ? detail : detail?.message || "Failed to create case");
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="wizard-title"
      style={{
        position: "fixed", inset: 0, zIndex: 1000,
        display: "flex", alignItems: "flex-start", justifyContent: "center",
        padding: "32px 16px",
        background: "rgba(15, 23, 42, 0.55)",
        backdropFilter: "blur(2px)",
      }}
      onClick={(e) => { if (e.target === e.currentTarget) onClose?.(); }}
    >
      <div
        style={{
          width: "100%", maxWidth: 640,
          background: C.surface, borderRadius: 16,
          border: `1px solid ${C.border}`,
          boxShadow: "0 20px 60px rgba(0,0,0,0.20), 0 4px 16px rgba(0,0,0,0.10)",
          overflow: "hidden",
          maxHeight: "calc(100vh - 64px)",
          display: "flex", flexDirection: "column",
        }}
      >
        {/* Header — brand gradient to match the admin's dialog headers */}
        <div
          style={{
            padding: "16px 20px",
            borderBottom: `1px solid ${C.border}`,
            background: `linear-gradient(135deg, ${C.brand}, ${C.brandDark})`,
            display: "flex", justifyContent: "space-between", alignItems: "center",
            color: "#fff", flexShrink: 0,
          }}
        >
          <h2 id="wizard-title" style={{ margin: 0, fontSize: 16, fontWeight: 800 }}>
            New offboarding case
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close wizard"
            style={{
              background: "rgba(255,255,255,0.15)", color: "#fff",
              border: "none", borderRadius: 8, padding: 6,
              cursor: "pointer", display: "inline-flex",
            }}
          >
            <X size={16} />
          </button>
        </div>

        {/* Stepper */}
        <ol
          aria-label="Wizard steps"
          style={{
            display: "flex", alignItems: "center", gap: 4,
            overflowX: "auto",
            padding: "10px 20px",
            background: C.surfaceAlt,
            borderBottom: `1px solid ${C.border}`,
            margin: 0, listStyle: "none",
            fontSize: 11, color: C.textSub,
            flexShrink: 0,
          }}
        >
          {STEPS.map((label, i) => {
            const isPast = i < step;
            const isCurrent = i === step;
            return (
              <li
                key={label}
                aria-current={isCurrent ? "step" : undefined}
                style={{
                  display: "flex", alignItems: "center", gap: 6,
                  whiteSpace: "nowrap",
                  color: isCurrent ? C.text : isPast ? C.green : C.textLight,
                  fontWeight: isCurrent ? 800 : 600,
                }}
              >
                <span
                  style={{
                    display: "inline-flex", alignItems: "center", justifyContent: "center",
                    width: 20, height: 20, borderRadius: 999, fontSize: 10, fontWeight: 700,
                    background: isPast ? C.greenBg : isCurrent ? C.brand : C.surface,
                    color: isPast ? C.green : isCurrent ? "#fff" : C.textLight,
                    border: `1px solid ${isPast ? C.greenBdr : isCurrent ? C.brand : C.border}`,
                  }}
                >
                  {isPast ? <CheckCircle2 size={12} /> : i + 1}
                </span>
                <span>{label}</span>
                {i < STEPS.length - 1 && (
                  <span style={{ padding: "0 4px", color: C.textLight }} aria-hidden="true">/</span>
                )}
              </li>
            );
          })}
        </ol>

        {/* Body — scrollable */}
        <div style={{ flex: 1, overflowY: "auto", padding: "20px" }}>
          {step === 0 && (
            <>
              <WorkerPickerField
                employees={employees}
                loading={dirLoading}
                error={dirError}
                selectedId={v.worker_id}
                onSelect={pickWorker}
              />
              <Field
                label="Engagement ID"
                hint="Auto-filled from the worker's employee code when available; override only if your engagement ledger uses a different value."
                required
              >
                <input
                  type="text" maxLength={80}
                  value={v.engagement_id}
                  onChange={(e) => set({ engagement_id: e.target.value })}
                  style={inputStyle(C)}
                  placeholder="ENG-1234"
                  aria-required="true"
                  onFocus={(e) => focusInput(e, C)} onBlur={(e) => blurInput(e, C)}
                />
              </Field>
              {duplicateHit && (
                <Warning>An active case already exists for this engagement. Cancel or complete it first.</Warning>
              )}
            </>
          )}

          {step === 1 && (
            <Field label="Exit type" required>
              <select
                value={v.exit_type}
                onChange={(e) => set({ exit_type: e.target.value })}
                style={{ ...inputStyle(C), cursor: "pointer" }}
                onFocus={(e) => focusInput(e, C)} onBlur={(e) => blurInput(e, C)}
              >
                {EXIT_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
              </select>
            </Field>
          )}

          {step === 2 && (
            <>
              <Field label="Final access time" hint="Leave blank for immediate-mode revocation.">
                <input
                  type="datetime-local"
                  value={v.final_access_at}
                  onChange={(e) => set({ final_access_at: e.target.value })}
                  style={inputStyle(C)}
                  onFocus={(e) => focusInput(e, C)} onBlur={(e) => blurInput(e, C)}
                />
              </Field>
              {warnFinalInPast && (
                <Warning>
                  Final access time is in the past — the enforcement gate will deny
                  access and telemetry the moment the case is approved.
                </Warning>
              )}
            </>
          )}

          {step === 3 && (
            <Field label="Security mode (revocation_mode)">
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {REVOCATION_MODES.map((m) => {
                  const checked = v.revocation_mode === m.value;
                  return (
                    <label
                      key={m.value}
                      style={{
                        display: "flex", alignItems: "flex-start", gap: 10,
                        padding: "10px 12px",
                        borderRadius: C.rSm,
                        border: `1.5px solid ${checked ? C.brand : C.border}`,
                        background: checked ? C.brandBg : C.surface,
                        cursor: "pointer", fontSize: 13,
                        color: checked ? C.brand : C.text,
                        fontWeight: checked ? 700 : 500,
                        transition: "all .15s",
                      }}
                    >
                      <input
                        type="radio"
                        name="revocation_mode"
                        value={m.value}
                        checked={checked}
                        onChange={(e) => set({ revocation_mode: e.target.value })}
                        style={{ marginTop: 2, accentColor: C.brand }}
                      />
                      <span>{m.label}</span>
                    </label>
                  );
                })}
              </div>
            </Field>
          )}

          {step === 4 && (
            <>
              <Field label="Risk level">
                <select
                  value={v.risk_level}
                  onChange={(e) => set({ risk_level: e.target.value })}
                  style={{ ...inputStyle(C), cursor: "pointer" }}
                  onFocus={(e) => focusInput(e, C)} onBlur={(e) => blurInput(e, C)}
                >
                  {RISK_LEVELS.map((r) => <option key={r} value={r}>{r}</option>)}
                </select>
              </Field>
              <Field label="Department" hint="Suggestions come from the live employee directory.">
                <input
                  type="text"
                  maxLength={100}
                  list="wizard-department-options"
                  value={v.department}
                  onChange={(e) => set({ department: e.target.value })}
                  style={inputStyle(C)}
                  onFocus={(e) => focusInput(e, C)} onBlur={(e) => blurInput(e, C)}
                />
                <datalist id="wizard-department-options">
                  {departments.map((d) => <option key={d} value={d} />)}
                </datalist>
              </Field>
              <Field label="Jurisdiction (ISO code)" hint="EU/DE/US/UK/IN — used to pick the retention policy.">
                <input
                  type="text" maxLength={40}
                  value={v.jurisdiction}
                  onChange={(e) => set({ jurisdiction: e.target.value.toUpperCase().slice(0, 4) })}
                  style={{ ...inputStyle(C), textTransform: "uppercase" }}
                  onFocus={(e) => focusInput(e, C)} onBlur={(e) => blurInput(e, C)}
                />
              </Field>
              <Field label="Termination reason (sensitive)" hint="Auto-redacted from anyone without the field.termination_reason permission.">
                <textarea
                  rows={2} maxLength={4000}
                  value={v.termination_reason}
                  onChange={(e) => set({ termination_reason: e.target.value })}
                  style={{ ...inputStyle(C), resize: "vertical", minHeight: 60 }}
                  onFocus={(e) => focusInput(e, C)} onBlur={(e) => blurInput(e, C)}
                />
              </Field>
            </>
          )}

          {step === 5 && (
            <>
              <Field label="Alternate verified email (for MFA delivery)" required>
                <input
                  type="email"
                  value={v.alternate_email}
                  onChange={(e) => set({ alternate_email: e.target.value })}
                  style={inputStyle(C)}
                  placeholder="alex.kim@personal.example"
                  aria-required="true"
                  onFocus={(e) => focusInput(e, C)} onBlur={(e) => blurInput(e, C)}
                />
              </Field>
              <Field label="Former-access expiry (days)">
                <input
                  type="number" min={1} max={3650}
                  value={v.expires_in_days}
                  onChange={(e) => set({ expires_in_days: Number(e.target.value) || 365 })}
                  style={{ ...inputStyle(C), width: 140 }}
                  onFocus={(e) => focusInput(e, C)} onBlur={(e) => blurInput(e, C)}
                />
              </Field>
              {warnMissingAltEmail && (
                <Warning>
                  No alternate email captured. The former worker will be unable to enroll
                  MFA — and therefore unable to log into the portal. Capture one now.
                </Warning>
              )}
            </>
          )}

          {step === 6 && (
            <ReviewSummary
              v={v}
              workerLabel={v.worker_id ? labelFor(Number(v.worker_id)) : "—"}
              warnFinalInPast={warnFinalInPast}
              warnMissingAltEmail={!v.alternate_email?.includes("@")}
              duplicateHit={duplicateHit}
            />
          )}
        </div>

        {/* Footer */}
        <div
          style={{
            padding: "12px 20px",
            borderTop: `1px solid ${C.border}`,
            background: C.surfaceAlt,
            display: "flex", justifyContent: "space-between", alignItems: "center",
            flexShrink: 0,
          }}
        >
          <ActionButton variant="subtle" size="sm" onClick={() => (step === 0 ? onClose() : setStep(step - 1))}>
            {step === 0 ? <X size={13} /> : <ArrowLeft size={13} />}
            {step === 0 ? "Cancel" : "Back"}
          </ActionButton>
          {step < STEPS.length - 1 ? (
            <ActionButton onClick={() => setStep(step + 1)} disabled={!canNext}>
              Next <ArrowRight size={13} />
            </ActionButton>
          ) : (
            <ActionButton onClick={submit} disabled={submitting}>
              {submitting ? "Creating…" : "Create case"}
            </ActionButton>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Helpers ──────────────────────────────────────────────────────────────

function inputStyle(C) {
  return {
    width: "100%",
    padding: "9px 12px",
    borderRadius: C.rSm,
    border: `1.5px solid ${C.border}`,
    background: C.bg,
    color: C.text,
    fontSize: 13,
    fontFamily: "inherit",
    outline: "none",
    transition: "border-color .2s, box-shadow .2s",
  };
}

function focusInput(e, C) {
  e.target.style.borderColor = C.brand;
  e.target.style.boxShadow = `0 0 0 3px ${C.brandBg}`;
}

function blurInput(e, C) {
  e.target.style.borderColor = C.border;
  e.target.style.boxShadow = "none";
}

function Field({ label, children, hint, required }) {
  const { C } = useTheme();
  return (
    <label style={{ display: "block", marginBottom: 14 }}>
      <span style={{ display: "block", marginBottom: 6, fontSize: 12, fontWeight: 700, color: C.textSub }}>
        {label} {required && <span style={{ color: C.red }}>*</span>}
      </span>
      {children}
      {hint && (
        <span style={{ display: "block", marginTop: 6, fontSize: 11, color: C.textLight }}>{hint}</span>
      )}
    </label>
  );
}

function Warning({ children }) {
  const { C } = useTheme();
  return (
    <div
      role="alert"
      style={{
        display: "flex", alignItems: "flex-start", gap: 8,
        padding: "10px 12px", borderRadius: C.rSm,
        background: C.amberBg, border: `1px solid ${C.amberBdr}`,
        color: C.amber, fontSize: 12, marginBottom: 12,
      }}
    >
      <AlertTriangle size={14} style={{ flexShrink: 0, marginTop: 1 }} aria-hidden="true" />
      <span>{children}</span>
    </div>
  );
}

function ReviewSummary({ v, workerLabel, warnFinalInPast, warnMissingAltEmail, duplicateHit }) {
  const { C } = useTheme();
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <h3 style={{ margin: 0, fontSize: 13, fontWeight: 800, color: C.text }}>Review</h3>
      <dl
        style={{
          display: "grid", gridTemplateColumns: "1fr 1fr",
          columnGap: 24, rowGap: 8,
          padding: 14, margin: 0,
          background: C.surfaceAlt, borderRadius: C.r,
          border: `1px solid ${C.border}`,
          fontSize: 12,
        }}
      >
        <Row label="Engagement">{v.engagement_id || "—"}</Row>
        <Row label="Worker">{workerLabel}{v.worker_id ? ` (#${v.worker_id})` : ""}</Row>
        <Row label="Exit type">{v.exit_type}</Row>
        <Row label="Security mode">{v.revocation_mode}</Row>
        <Row label="Final access">{v.final_access_at ? new Date(v.final_access_at).toLocaleString() : "—"}</Row>
        <Row label="Risk">{v.risk_level}</Row>
        <Row label="Department">{v.department || "—"}</Row>
        <Row label="Jurisdiction">{v.jurisdiction || "—"}</Row>
        <Row label="Alt email">{v.alternate_email || "—"}</Row>
        <Row label="Expiry">{v.expires_in_days} d</Row>
      </dl>
      {warnFinalInPast && <Warning>Final access is in the past — closure will start immediately on approval.</Warning>}
      {warnMissingAltEmail && <Warning>No alternate email — former-worker portal MFA cannot complete.</Warning>}
      {duplicateHit && <Warning>An active case for this engagement already exists.</Warning>}
    </div>
  );
}

function Row({ label, children }) {
  const { C } = useTheme();
  return (
    <div style={{ display: "contents" }}>
      <dt style={{ color: C.textSub, fontWeight: 600 }}>{label}</dt>
      <dd style={{ color: C.text, margin: 0 }}>{children}</dd>
    </div>
  );
}

function WorkerPickerField({ employees, loading, error, selectedId, onSelect }) {
  const { C } = useTheme();
  const [query, setQuery] = useState("");

  const selected = useMemo(() => {
    if (!selectedId) return null;
    return employees.find((e) => Number(e.id) === Number(selectedId)) || null;
  }, [employees, selectedId]);

  const suggestions = useMemo(() => {
    const q = query.trim().toLowerCase();
    const pool = (employees || []).filter((e) => e.is_active !== false);
    if (!q) return pool.slice(0, 8);
    return pool
      .filter((e) => {
        const blob = [
          e.name, e.email, e.employee_id, e.department, e.team, e.role, e.designation,
        ].filter(Boolean).join(" ").toLowerCase();
        return blob.includes(q);
      })
      .slice(0, 8);
  }, [employees, query]);

  if (selected) {
    return (
      <Field label="Worker" required>
        <div
          style={{
            display: "flex", alignItems: "center", justifyContent: "space-between",
            gap: 12, padding: "10px 12px",
            borderRadius: C.rSm, background: C.greenBg,
            border: `1px solid ${C.greenBdr}`,
            fontSize: 13,
          }}
          aria-live="polite"
        >
          <div style={{ minWidth: 0 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, color: C.text }}>
              <UserCheck size={14} color={C.green} aria-hidden="true" />
              <span style={{ fontWeight: 700, overflow: "hidden", textOverflow: "ellipsis" }}>
                {selected.name || selected.email}
              </span>
              <span
                style={{
                  fontSize: 10, fontWeight: 700,
                  padding: "2px 8px", borderRadius: 999,
                  background: C.surface, color: C.textSub,
                  border: `1px solid ${C.border}`,
                }}
              >
                #{selected.id}
              </span>
            </div>
            <div style={{ marginTop: 2, fontSize: 11, color: C.textSub }}>
              {[selected.designation, selected.department, selected.email]
                .filter(Boolean).join(" · ") || "—"}
            </div>
          </div>
          <button
            type="button"
            onClick={() => { onSelect(null); setQuery(""); }}
            style={{
              padding: "6px 12px", borderRadius: C.rSm,
              fontSize: 12, fontWeight: 700, color: C.textSub,
              background: C.surface, border: `1.5px solid ${C.border}`,
              cursor: "pointer", fontFamily: "inherit",
            }}
          >
            Change
          </button>
        </div>
      </Field>
    );
  }

  return (
    <Field label="Worker" required hint="Pick the employee being offboarded — pulled live from the team directory.">
      <div style={{ position: "relative" }}>
        <span style={{ position: "absolute", left: 12, top: "50%", transform: "translateY(-50%)", display: "inline-flex" }}>
          <Search size={14} color={C.textLight} aria-hidden="true" />
        </span>
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={loading ? "Loading directory…" : "Search by name, email, employee id…"}
          aria-label="Search for a worker"
          aria-required="true"
          style={{ ...inputStyle(C), paddingLeft: 34 }}
          onFocus={(e) => focusInput(e, C)} onBlur={(e) => blurInput(e, C)}
        />
      </div>
      {error && (
        <div
          role="alert"
          style={{
            marginTop: 8, padding: "6px 10px", borderRadius: C.rSm,
            background: C.redBg, color: C.red, border: `1px solid ${C.redBdr}`,
            fontSize: 11,
          }}
        >
          {String(typeof error === "string" ? error : error?.message || "Could not load directory")}
        </div>
      )}
      <ul
        role="listbox"
        style={{
          marginTop: 8, maxHeight: 280, overflowY: "auto",
          borderRadius: C.r, border: `1px solid ${C.border}`,
          background: C.surface,
          listStyle: "none", padding: 0,
        }}
      >
        {!loading && suggestions.length === 0 && (
          <li style={{ padding: "10px 14px", fontSize: 12, color: C.textSub }}>No matching workers.</li>
        )}
        {loading && (
          <li style={{ padding: "10px 14px", fontSize: 12, color: C.textSub }}>Loading workers…</li>
        )}
        {!loading && suggestions.map((e, i) => (
          <li key={e.id} style={{ borderTop: i === 0 ? "none" : `1px solid ${C.borderLight}` }}>
            <button
              type="button"
              role="option"
              onClick={() => onSelect(e)}
              onMouseEnter={(ev) => { ev.currentTarget.style.background = C.surfaceAlt; }}
              onMouseLeave={(ev) => { ev.currentTarget.style.background = "transparent"; }}
              style={{
                width: "100%", display: "flex", alignItems: "flex-start", gap: 10,
                padding: "10px 14px", textAlign: "left",
                background: "transparent", border: "none",
                cursor: "pointer", fontFamily: "inherit",
                color: C.text, fontSize: 13,
                transition: "background .15s",
              }}
            >
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ fontWeight: 700, color: C.text, overflow: "hidden", textOverflow: "ellipsis" }}>
                    {e.name || e.email || `Worker #${e.id}`}
                  </span>
                  {e.employee_id && (
                    <span
                      style={{
                        fontSize: 10, fontWeight: 700,
                        padding: "2px 8px", borderRadius: 999,
                        background: C.surfaceAlt, color: C.textSub,
                        border: `1px solid ${C.border}`,
                      }}
                    >
                      {e.employee_id}
                    </span>
                  )}
                </div>
                <div style={{ marginTop: 2, fontSize: 11, color: C.textSub }}>
                  {[e.designation, e.department, e.team, e.email].filter(Boolean).join(" · ") || "—"}
                </div>
              </div>
              <span style={{ marginLeft: 8, fontSize: 10, fontWeight: 700, color: C.textLight, letterSpacing: 0.5 }}>
                #{e.id}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </Field>
  );
}

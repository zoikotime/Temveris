/**
 * Organization setup wizard — the three steps a newly self-registered admin
 * walks before entering the product.
 *
 *   Step 1  Organization    -> PUT  /org/settings          (existing API)
 *   Step 2  Teams           -> POST /org/teams             (existing API)
 *   Step 3  Completion      -> POST /api/v1/onboarding/setup/complete
 *
 * Only the progress marker is a new endpoint. Steps 1 and 2 reuse the
 * organization-settings and team APIs the product already exposes, so there is
 * no second way to name an organization or create a team.
 *
 * TENANCY: no request from this page carries an organization id. The server
 * derives the tenant from the authenticated user on every call — the wizard
 * configures "my organization", it cannot address someone else's.
 *
 * RESUMABILITY: the current step lives on the server, so a refresh, a closed
 * browser or a back-navigation all resume in the right place, and a completed
 * tenant never sees the wizard again.
 *
 * SURFACE: this is the first screen after signup, so it wears the same skin as
 * the auth screens rather than a plain grey admin page — the animated brand
 * gradient and gradient-bordered card from AuthLayout, the field styling from
 * Start, and the logo-left header bar from the install guides. The visitor
 * should not feel handed off to a different product between "create account"
 * and "create organization".
 */

// React is imported explicitly (rather than relying on the automatic JSX
// runtime) to match every other component under test here: vitest.config.js is
// a separate config from vite.config.js and does not enable the automatic
// runtime, so a component without this import renders fine in the app but
// throws "React is not defined" under test.
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Building2, Users, CheckCircle2, Loader2, Plus, X, ArrowRight, AlertTriangle,
  Check, Globe2, MapPin, UserPlus, SlidersHorizontal, MonitorDown, Sparkles,
  ShieldCheck,
} from "lucide-react";
import { API_BASE } from "../config";
import { readToken } from "../api/authStorage";
import logo from "../assets/Temveris_Logo_PNG.png";
import DesktopDownloadCards from "../components/auth/DesktopDownloadCards";

const STEPS = [
  { key: "organization", label: "Organization", hint: "Name & region", icon: Building2 },
  { key: "teams",        label: "Teams",        hint: "How you're structured", icon: Users },
  { key: "complete",     label: "Finish",       hint: "You're ready", icon: CheckCircle2 },
];

function authHeaders() {
  const t = readToken();
  return {
    "Content-Type": "application/json",
    ...(t ? { Authorization: `Bearer ${t}` } : {}),
  };
}

/** Best-effort browser timezone; the admin can always override, and the server
 *  remains authoritative for what is stored. */
function guessTimezone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch {
    return "";
  }
}

export default function OrganizationSetup() {
  const navigate = useNavigate();

  const [loading, setLoading] = useState(true);
  const [state, setState] = useState(null);
  const [loadError, setLoadError] = useState(null);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const res = await fetch(`${API_BASE}/api/v1/onboarding/setup`, {
        headers: authHeaders(),
      });
      if (res.status === 401) {
        navigate("/", { replace: true });
        return;
      }
      if (!res.ok) {
        setLoadError("We couldn't load your setup progress.");
        return;
      }
      const data = await res.json();
      setState(data);
      // A tenant that has already finished must never be shown the wizard again.
      if (data.completed || data.setup_required === false) {
        navigate("/admin", { replace: true });
      }
    } catch {
      setLoadError("We couldn't reach the server. Check your connection and retry.");
    } finally {
      setLoading(false);
    }
  }, [navigate]);

  useEffect(() => { load(); }, [load]);

  if (loading) {
    return (
      <Shell>
        <Panel>
          <div className="flex flex-col items-center justify-center gap-3 py-16 text-slate-500">
            <Loader2 className="h-6 w-6 animate-spin text-brand-green" aria-hidden />
            <span className="text-[14px] font-medium">Loading your setup…</span>
          </div>
        </Panel>
      </Shell>
    );
  }

  if (loadError) {
    return (
      <Shell>
        <Panel>
          <div className="px-1 py-2 text-center sm:px-4">
            <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-amber-50 ring-1 ring-amber-200">
              <AlertTriangle className="h-7 w-7 text-amber-600" aria-hidden />
            </div>
            <h1 className="text-[20px] font-bold tracking-tight text-slate-900">
              Something went wrong
            </h1>
            <p className="mx-auto mt-1.5 max-w-[42ch] text-[14px] leading-relaxed text-slate-500">
              {loadError}
            </p>
            <button onClick={load} className={`${btnPrimary} mt-5`}>
              Try again
            </button>
          </div>
        </Panel>
      </Shell>
    );
  }

  if (!state) return null;

  const stepIndex = Math.max(
    0, STEPS.findIndex((s) => s.key === state.current_step),
  );

  return (
    <Shell>
      <Stepper current={stepIndex} />
      {state.current_step === "organization" && (
        <OrganizationStep state={state} onDone={load} />
      )}
      {state.current_step === "teams" && (
        <TeamsStep state={state} onDone={load} />
      )}
      {state.current_step === "complete" && (
        <CompletionStep state={state} />
      )}
    </Shell>
  );
}

/* ── chrome ──────────────────────────────────────────────────────────────── */

/**
 * Page frame: brand gradient background (same treatment as AuthLayout) with a
 * translucent header bar carrying the logo in the left corner, matching the
 * install guides and the admin shell.
 */
function Shell({ children }) {
  return (
    <div className="relative min-h-screen w-full overflow-hidden bg-[#F4FAF7]">
      <div
        aria-hidden
        className="absolute inset-0 -z-30 bg-gradient-to-br from-brand-green-light via-white to-brand-blue-light bg-[length:200%_200%] animate-[gradientMove_14s_ease_infinite]"
      />
      <div
        aria-hidden
        className="absolute -z-20 h-[520px] w-[520px] -top-40 -left-40 rounded-full bg-brand-green/20 blur-[140px]"
      />
      <div
        aria-hidden
        className="absolute -z-20 h-[460px] w-[460px] -bottom-48 -right-40 rounded-full bg-brand-blue/15 blur-[150px]"
      />

      {/* Full-bleed, deliberately NOT constrained to the max-w-3xl content
          column: the logo belongs hard against the left edge and the help link
          against the right, the way every other chrome bar in the app sits. */}
      <header className="relative border-b border-white/70 bg-white/70 backdrop-blur-md">
        <div className="flex w-full items-center justify-between gap-3 px-4 py-3.5 sm:px-6 lg:px-8">
          <a href="/" aria-label="Temveris home" className="shrink-0">
            <img
              src={logo}
              alt="Temveris"
              className="logo-glow h-7 w-auto select-none sm:h-8"
              draggable={false}
            />
          </a>
          <a
            href="mailto:support@temveris.example"
            className="text-[13px] font-semibold text-brand-blue hover:underline underline-offset-2"
          >
            Need help?
          </a>
        </div>
      </header>

      <main className="relative mx-auto w-full max-w-3xl px-4 py-7 sm:px-6 sm:py-10">
        {children}
      </main>
    </div>
  );
}

/** The gradient-bordered white card used by every screen of the wizard. */
function Panel({ children }) {
  return (
    <div className="page-enter-active rounded-[28px] bg-gradient-to-b from-brand-green/35 via-brand-green/10 to-brand-blue/25 p-[1.5px] shadow-[0_30px_80px_-30px_rgba(17,73,106,0.25)]">
      <div className="rounded-[22px] bg-white px-5 py-6 sm:px-8 sm:py-8">
        {children}
      </div>
    </div>
  );
}

function Stepper({ current }) {
  return (
    <nav aria-label="Setup progress" className="mb-6 sm:mb-8">
      <ol className="flex items-center">
        {STEPS.map((s, i) => {
          const done = i < current;
          const active = i === current;
          const Icon = s.icon;
          return (
            <li
              key={s.key}
              className={"flex items-center " + (i < STEPS.length - 1 ? "flex-1" : "")}
            >
              <div className="flex items-center gap-2.5">
                <span
                  aria-current={active ? "step" : undefined}
                  className={
                    "flex h-10 w-10 shrink-0 items-center justify-center rounded-full transition-all duration-300 " +
                    (done
                      ? "bg-brand-green text-white shadow-brandGreen"
                      : active
                        ? "bg-brand-blue text-white shadow-brandSoft ring-4 ring-brand-blue/15"
                        : "border border-slate-200 bg-white/80 text-slate-400")
                  }
                >
                  {done
                    ? <Check className="h-5 w-5" strokeWidth={3} aria-hidden />
                    : <Icon className="h-[18px] w-[18px]" aria-hidden />}
                </span>
                <span className="hidden leading-tight sm:block">
                  <span
                    className={
                      "block text-[13.5px] font-semibold transition-colors duration-300 " +
                      (active
                        ? "text-slate-900"
                        : done
                          ? "text-brand-green-dark"
                          : "text-slate-400")
                    }
                  >
                    {s.label}
                  </span>
                  <span className="block text-[11.5px] text-slate-400">{s.hint}</span>
                </span>
              </div>
              {i < STEPS.length - 1 && (
                <span
                  aria-hidden
                  className={
                    "mx-2.5 h-[3px] flex-1 rounded-full transition-colors duration-500 sm:mx-4 " +
                    (done ? "bg-brand-green" : "bg-slate-200/80")
                  }
                />
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

/** Card header: brand icon tile, step eyebrow, title and supporting line. */
function Card({ eyebrow, title, subtitle, icon, children }) {
  const Icon = icon;
  return (
    <Panel>
      <div className="flex items-start gap-3.5 sm:gap-4">
        {Icon && (
          <span className="hidden h-12 w-12 shrink-0 items-center justify-center rounded-2xl bg-brand-green/10 ring-1 ring-brand-green/20 sm:flex">
            <Icon className="h-6 w-6 text-brand-green-dark" aria-hidden />
          </span>
        )}
        <div className="min-w-0">
          {eyebrow && (
            <p className="mb-1 text-[11px] font-bold uppercase tracking-[0.14em] text-brand-green-dark">
              {eyebrow}
            </p>
          )}
          <h1 className="text-[22px] font-bold tracking-tight text-slate-900 sm:text-[26px]">
            {title}
          </h1>
          {subtitle && (
            <p className="mt-1.5 text-[14px] leading-relaxed text-slate-500">{subtitle}</p>
          )}
        </div>
      </div>
      <div className="mt-6 sm:mt-7">{children}</div>
    </Panel>
  );
}

const btnPrimary =
  "btn-hover inline-flex items-center justify-center gap-2 rounded-xl bg-brand-blue px-5 py-3 " +
  "text-[15px] font-semibold text-white shadow-brandSoft transition-colors hover:bg-brand-blue-dark " +
  "disabled:cursor-not-allowed disabled:opacity-60";
const btnGhost =
  "inline-flex items-center justify-center gap-2 rounded-xl border border-slate-200 " +
  "bg-white px-5 py-3 text-[15px] font-semibold text-slate-600 transition-colors " +
  "hover:border-slate-300 hover:bg-slate-50 hover:text-slate-800 " +
  "disabled:cursor-not-allowed disabled:opacity-60";
// Matches the login/signup inputs so the wizard focuses and glows identically.
const inputCls =
  "w-full rounded-xl border border-slate-200 bg-white px-4 py-2.5 text-[15px] " +
  "text-slate-800 placeholder-slate-400 input-glow focus:outline-none " +
  "focus:border-brand-green focus:ring-2 focus:ring-brand-green/15";
const inputWithIcon = `${inputCls} pl-11`;

function Field({ label, required, hint, children }) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-[13px] font-semibold text-brand-green-dark">
        {label}
        {required && <span className="text-red-500"> *</span>}
      </span>
      {children}
      {hint && (
        <span className="mt-1.5 block text-[12px] leading-relaxed text-slate-400">
          {hint}
        </span>
      )}
    </label>
  );
}

/** Leading-icon wrapper for an input. */
function WithIcon({ icon, children }) {
  const Icon = icon;
  return (
    <div className="relative">
      <Icon
        className="pointer-events-none absolute left-3.5 top-1/2 h-[18px] w-[18px] -translate-y-1/2 text-slate-400"
        aria-hidden
      />
      {children}
    </div>
  );
}

function ErrorNote({ children }) {
  if (!children) return null;
  return (
    <p
      role="alert"
      className="mt-4 flex items-start gap-2 rounded-xl border border-red-200 bg-red-50 px-3.5 py-2.5 text-[13px] leading-relaxed text-red-800"
    >
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
      <span>{children}</span>
    </p>
  );
}

/* ── step 1 ──────────────────────────────────────────────────────────────── */

/** Timezone suggestions for the datalist. Falls back to a short curated list on
 *  engines without Intl.supportedValuesOf, so the field is never empty-handed. */
function timezoneOptions() {
  try {
    const all = Intl.supportedValuesOf?.("timeZone");
    if (Array.isArray(all) && all.length) return all;
  } catch {
    /* fall through */
  }
  return [
    "Asia/Kolkata", "Asia/Dubai", "Asia/Singapore", "Australia/Sydney",
    "Europe/London", "Europe/Berlin", "Europe/Madrid", "America/New_York",
    "America/Chicago", "America/Denver", "America/Los_Angeles", "UTC",
  ];
}

/** Live "it's 4:12 PM there" preview, and a gentle warning on an unknown zone. */
function timezonePreview(tz) {
  if (!tz.trim()) return null;
  try {
    const time = new Intl.DateTimeFormat(undefined, {
      timeZone: tz.trim(), hour: "numeric", minute: "2-digit",
    }).format(new Date());
    return { ok: true, text: `Local time there is ${time}.` };
  } catch {
    return { ok: false, text: "We don't recognise that timezone — pick one from the list." };
  }
}

/** Resolve a two-letter code to a country name so the admin can see what they typed. */
function countryName(code) {
  const c = code.trim().toUpperCase();
  if (c.length !== 2) return null;
  try {
    const name = new Intl.DisplayNames(undefined, { type: "region" }).of(c);
    return name && name !== c ? name : null;
  } catch {
    return null;
  }
}

function OrganizationStep({ state, onDone }) {
  const org = state.organization || {};
  const [name, setName] = useState(org.company_name || "");
  const [timezone, setTimezone] = useState(org.default_timezone || guessTimezone());
  const [country, setCountry] = useState(org.primary_country || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const zones = useMemo(timezoneOptions, []);
  const tzPreview = timezonePreview(timezone);
  const resolvedCountry = countryName(country);

  const trimmed = name.trim();
  const canSubmit = trimmed.length > 0 && !busy;

  async function submit(e) {
    e.preventDefault();
    if (!canSubmit) return;            // also guards Enter-key double submits
    setBusy(true);
    setError(null);
    try {
      // Existing organization-settings API. No organization id is sent — the
      // server writes to the caller's own organization.
      const res = await fetch(`${API_BASE}/org/settings`, {
        method: "PUT",
        headers: authHeaders(),
        body: JSON.stringify({
          company_name: trimmed,
          default_timezone: timezone || null,
          primary_country: country ? country.toUpperCase().slice(0, 2) : null,
        }),
      });
      if (!res.ok) {
        setError(
          res.status === 403
            ? "You don't have permission to configure this organization."
            : "We couldn't save your organization details. Please try again.",
        );
        return;
      }
      const step = await fetch(`${API_BASE}/api/v1/onboarding/setup/step`, {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({ step: "teams" }),
      });
      if (!step.ok) {
        setError("Your details were saved, but we couldn't advance. Please retry.");
        return;
      }
      onDone();
    } catch {
      setError("We couldn't reach the server. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card
      eyebrow="Step 1 of 3"
      title="Create your organization"
      subtitle="Welcome to Temveris — let's get you set up. You can change any of this later in Organization Settings."
      icon={Building2}
    >
      <form onSubmit={submit} noValidate>
        <Field label="Organization name" required>
          <WithIcon icon={Building2}>
            <input
              className={inputWithIcon}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Acme Corporation"
              autoComplete="organization"
              autoFocus
              required
              aria-required="true"
            />
          </WithIcon>
        </Field>

        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          <Field
            label="Timezone"
            hint={
              tzPreview && !tzPreview.ok
                ? undefined
                : "Detected from your browser — edit if your team works elsewhere."
            }
          >
            <WithIcon icon={Globe2}>
              <input
                className={inputWithIcon}
                value={timezone}
                onChange={(e) => setTimezone(e.target.value)}
                placeholder="Asia/Kolkata"
                list="zt-setup-timezones"
                autoComplete="off"
                spellCheck={false}
              />
            </WithIcon>
            <datalist id="zt-setup-timezones">
              {zones.map((z) => <option key={z} value={z} />)}
            </datalist>
            {tzPreview && (
              <span
                className={
                  "mt-1.5 block text-[12px] leading-relaxed " +
                  (tzPreview.ok ? "text-brand-green-dark" : "text-amber-600")
                }
              >
                {tzPreview.text}
              </span>
            )}
          </Field>

          <Field
            label="Country"
            hint="Two-letter code. Sets your default currency and holiday calendar."
          >
            <WithIcon icon={MapPin}>
              <input
                className={`${inputWithIcon} uppercase tracking-wide`}
                value={country}
                onChange={(e) => setCountry(e.target.value)}
                placeholder="IN"
                maxLength={2}
                autoComplete="country"
                spellCheck={false}
              />
            </WithIcon>
            {resolvedCountry && (
              <span className="mt-1.5 block text-[12px] font-medium text-brand-green-dark">
                {resolvedCountry}
              </span>
            )}
          </Field>
        </div>

        <ErrorNote>{error}</ErrorNote>

        <div className="mt-7 flex items-center justify-between gap-3 border-t border-slate-100 pt-5">
          <p className="hidden items-center gap-1.5 text-[12.5px] text-slate-400 sm:flex">
            <ShieldCheck className="h-4 w-4 text-brand-green" aria-hidden />
            Everything here is editable later.
          </p>
          <button
            type="submit"
            className={`${btnPrimary} w-full sm:w-auto`}
            disabled={!canSubmit}
          >
            {busy ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> Saving…
              </>
            ) : (
              <>
                Save &amp; continue <ArrowRight className="h-4 w-4" aria-hidden />
              </>
            )}
          </button>
        </div>
      </form>
    </Card>
  );
}

/* ── step 2 ──────────────────────────────────────────────────────────────── */

// Offered as one-tap chips because most organizations name their first teams
// after the same handful of functions, and typing them out is pure friction.
const TEAM_SUGGESTIONS = [
  "Engineering", "Sales", "Operations", "Support", "Finance", "Marketing",
];

function TeamsStep({ state, onDone }) {
  const existingCount = state.teams?.count ?? 0;
  // Rendered ONLY when the server reports a real, enforced cap. A limit shown
  // here is always one the backend will actually apply.
  const limit = state.teams?.limit ?? null;

  const [drafts, setDrafts] = useState([""]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const named = drafts.map((d) => d.trim()).filter(Boolean);
  const atLimit = limit !== null && existingCount + named.length >= limit;

  const update = (i, v) => setDrafts((d) => d.map((x, j) => (j === i ? v : x)));
  const add = () => setDrafts((d) => [...d, ""]);
  const remove = (i) => setDrafts((d) => (d.length === 1 ? [""] : d.filter((_, j) => j !== i)));

  // A chip fills the first blank row if there is one, otherwise appends —
  // so tapping three chips in a row builds three teams, not one.
  const addSuggestion = (label) => setDrafts((d) => {
    const blank = d.findIndex((x) => !x.trim());
    return blank === -1 ? [...d, label] : d.map((x, j) => (j === blank ? label : x));
  });

  const takenSuggestions = new Set(named.map((n) => n.toLowerCase()));

  async function advance() {
    const res = await fetch(`${API_BASE}/api/v1/onboarding/setup/step`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ step: "complete" }),
    });
    return res.ok;
  }

  async function submit(e) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      // Existing team API, one call per team. Duplicates (409) are treated as
      // already-done so a retry after a partial failure is safe.
      for (const teamName of named) {
        const res = await fetch(`${API_BASE}/org/teams`, {
          method: "POST",
          headers: authHeaders(),
          body: JSON.stringify({ name: teamName }),
        });
        if (res.status === 409) continue;
        if (!res.ok) {
          let msg = `We couldn't create the team "${teamName}".`;
          if (res.status === 403) {
            const body = await res.json().catch(() => null);
            msg = body?.detail?.message || "Your plan's team limit has been reached.";
          }
          setError(msg);
          return;
        }
      }
      if (!(await advance())) {
        setError("Your teams were created, but we couldn't advance. Please retry.");
        return;
      }
      onDone();
    } catch {
      setError("We couldn't reach the server. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  async function skip() {
    setBusy(true);
    setError(null);
    try {
      if (!(await advance())) setError("We couldn't continue. Please try again.");
      else onDone();
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card
      eyebrow="Step 2 of 3"
      title="Let's set up your teams"
      subtitle="Group people by department or function. You can add or manage teams later from Organization Settings."
      icon={Users}
    >
      <form onSubmit={submit} noValidate>
        {existingCount > 0 && (
          <p className="mb-4 inline-flex items-center gap-2 rounded-full bg-brand-blue-light px-3 py-1.5 text-[12.5px] font-medium text-brand-blue">
            <Users className="h-3.5 w-3.5" aria-hidden />
            {existingCount} team{existingCount === 1 ? "" : "s"} already exist
            {existingCount === 1 ? "s" : ""} in your organization.
          </p>
        )}

        <div className="flex flex-col gap-2.5">
          {drafts.map((value, i) => (
            <div key={i} className="flex items-center gap-2.5">
              <span
                aria-hidden
                className="hidden h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-slate-50 text-[13px] font-bold text-slate-400 ring-1 ring-slate-200 sm:flex"
              >
                {i + 1}
              </span>
              <input
                className={inputCls}
                value={value}
                onChange={(e) => update(i, e.target.value)}
                placeholder={i === 0 ? "Engineering" : "Add another team"}
                aria-label={`Team ${i + 1} name`}
              />
              <button
                type="button"
                onClick={() => remove(i)}
                className="shrink-0 rounded-xl border border-slate-200 p-2.5 text-slate-400 transition-colors hover:border-red-200 hover:bg-red-50 hover:text-red-600"
                aria-label={`Remove team ${i + 1}`}
              >
                <X className="h-4 w-4" aria-hidden />
              </button>
            </div>
          ))}
        </div>

        <button
          type="button"
          onClick={add}
          disabled={atLimit}
          className="mt-3 inline-flex items-center gap-1.5 rounded-lg px-1 py-1 text-[13.5px] font-semibold text-brand-blue transition-colors hover:text-brand-blue-dark hover:underline underline-offset-2 disabled:cursor-not-allowed disabled:text-slate-300 disabled:no-underline"
        >
          <Plus className="h-4 w-4" aria-hidden /> Add another team
        </button>

        <div className="mt-5 rounded-xl border border-slate-100 bg-slate-50/70 p-3.5">
          <p className="mb-2 flex items-center gap-1.5 text-[12px] font-semibold uppercase tracking-wider text-slate-500">
            <Sparkles className="h-3.5 w-3.5 text-brand-green" aria-hidden />
            Popular teams
          </p>
          <div className="flex flex-wrap gap-2">
            {TEAM_SUGGESTIONS.map((label) => {
              const used = takenSuggestions.has(label.toLowerCase());
              return (
                <button
                  key={label}
                  type="button"
                  onClick={() => addSuggestion(label)}
                  disabled={used || atLimit}
                  className={
                    "inline-flex items-center gap-1 rounded-full border px-3 py-1.5 text-[13px] font-medium transition-colors " +
                    (used
                      ? "cursor-default border-brand-green-mid bg-brand-green-light text-brand-green-dark"
                      : "border-slate-200 bg-white text-slate-600 hover:border-brand-green hover:bg-brand-green-light hover:text-brand-green-dark disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:border-slate-200 disabled:hover:bg-white disabled:hover:text-slate-600")
                  }
                >
                  {used
                    ? <Check className="h-3.5 w-3.5" strokeWidth={3} aria-hidden />
                    : <Plus className="h-3.5 w-3.5" aria-hidden />}
                  {label}
                </button>
              );
            })}
          </div>
        </div>

        {limit !== null && (
          <p className="mt-3 text-[12px] text-slate-400">
            Your plan includes {limit} team{limit === 1 ? "" : "s"}.
          </p>
        )}

        <ErrorNote>{error}</ErrorNote>

        <div className="mt-7 flex flex-col-reverse gap-2.5 border-t border-slate-100 pt-5 sm:flex-row sm:justify-end">
          <button type="button" onClick={skip} className={btnGhost} disabled={busy}>
            Skip for now
          </button>
          <button type="submit" className={btnPrimary} disabled={busy || named.length === 0}>
            {busy ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> Creating…
              </>
            ) : (
              <>
                Create &amp; continue <ArrowRight className="h-4 w-4" aria-hidden />
              </>
            )}
          </button>
        </div>
      </form>
    </Card>
  );
}

/* ── step 3 ──────────────────────────────────────────────────────────────── */

const NEXT_STEPS = [
  {
    icon: UserPlus,
    title: "Invite your teammates",
    body: "Send invitations and they'll set their own password.",
  },
  {
    icon: SlidersHorizontal,
    title: "Configure tracking and timezone",
    body: "Choose what's tracked, and the hours your organization works.",
  },
  {
    icon: MonitorDown,
    title: "Install the desktop app",
    body: "Time tracking starts as soon as your team signs in.",
  },
];

function SummaryTile({ label, value }) {
  return (
    <div className="rounded-xl border border-slate-100 bg-slate-50/70 px-4 py-3">
      <dt className="text-[11px] font-bold uppercase tracking-[0.12em] text-slate-400">
        {label}
      </dt>
      <dd className="mt-1 truncate text-[15px] font-semibold text-slate-900" title={value}>
        {value}
      </dd>
    </div>
  );
}

function CompletionStep({ state }) {
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const org = state.organization || {};
  const admin = state.admin || {};
  const seats = state.seats || {};

  const seatLabel = seats.used == null
    ? "—"
    : seats.limit != null
      ? `${seats.used} of ${seats.limit}`
      : String(seats.used);

  async function finish(destination) {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/v1/onboarding/setup/complete`, {
        method: "POST",
        headers: authHeaders(),
      });
      if (!res.ok) {
        setError("We couldn't finish setup. Please try again.");
        return;
      }
      navigate(destination, { replace: true });
    } catch {
      setError("We couldn't reach the server. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel>
      <div className="text-center">
        <span className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-2xl bg-brand-green/10 ring-1 ring-brand-green/25">
          <CheckCircle2 className="h-8 w-8 text-brand-green-dark" aria-hidden />
        </span>
        <p className="mb-1 text-[11px] font-bold uppercase tracking-[0.14em] text-brand-green-dark">
          Step 3 of 3
        </p>
        <h1 className="text-[24px] font-bold tracking-tight text-slate-900 sm:text-[28px]">
          Congratulations!
        </h1>
        <p className="mx-auto mt-1.5 max-w-[46ch] text-[14px] leading-relaxed text-slate-500">
          Your organization is ready to use Temveris.
        </p>
      </div>

      <dl className="mt-6 grid gap-3 sm:grid-cols-2">
        <SummaryTile label="Organization" value={org.company_name || "—"} />
        <SummaryTile label="Administrator" value={admin.email || "—"} />
        <SummaryTile label="Teams" value={String(state.teams?.count ?? 0)} />
        <SummaryTile label="Seats" value={seatLabel} />
      </dl>

      <h2 className="mt-7 text-[12px] font-bold uppercase tracking-[0.14em] text-slate-400">
        Next steps
      </h2>
      <ul className="mt-3 grid gap-2.5">
        {NEXT_STEPS.map((step) => {
          // Capitalized local rather than `{ icon: Icon }` in the parameter
          // list: eslint-plugin-react is not installed here, so a bare <Icon />
          // is not counted as a usage and a destructured binding would be
          // flagged unused. A capitalized *variable* is covered by the config's
          // varsIgnorePattern.
          const Icon = step.icon;
          return (
            <li
              key={step.title}
              className="flex items-start gap-3 rounded-xl border border-slate-100 bg-white px-4 py-3"
            >
              <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-brand-blue-light">
                <Icon className="h-4 w-4 text-brand-blue" aria-hidden />
              </span>
              <span className="min-w-0">
                <span className="block text-[14px] font-semibold text-slate-800">
                  {step.title}
                </span>
                <span className="block text-[13px] leading-relaxed text-slate-500">
                  {step.body}
                </span>
              </span>
            </li>
          );
        })}
      </ul>

      {/* Same component the invite page uses — one implementation of the
          per-OS download cards, so the two screens cannot drift. */}
      <div className="mt-6 rounded-2xl border border-slate-100 bg-slate-50/70 p-4 sm:p-5">
        <p className="mb-1 text-[14px] font-semibold text-slate-800">Get the desktop app</p>
        <p className="mb-4 text-[13px] leading-relaxed text-slate-500">
          Install it on each machine that should track time — it runs quietly in
          the background once signed in.
        </p>
        <DesktopDownloadCards />
      </div>

      <ErrorNote>{error}</ErrorNote>

      <div className="mt-7 flex flex-col gap-2.5 border-t border-slate-100 pt-5 sm:flex-row sm:flex-wrap sm:justify-end">
        <button
          className={btnGhost}
          disabled={busy}
          onClick={() => finish("/settings/workspace-profile")}
        >
          Configure settings
        </button>
        <button
          className={btnGhost}
          disabled={busy}
          onClick={() => finish("/settings/workers-users")}
        >
          Invite team members
        </button>
        <button
          className={btnPrimary}
          disabled={busy}
          onClick={() => finish("/admin")}
        >
          {busy ? (
            <>
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> Finishing…
            </>
          ) : (
            <>
              Go to dashboard <ArrowRight className="h-4 w-4" aria-hidden />
            </>
          )}
        </button>
      </div>
    </Panel>
  );
}

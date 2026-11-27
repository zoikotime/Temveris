/**
 * Configure your subscription — the conversion surface between the activation
 * flow's Ready step and Stripe Checkout.
 *
 * WHY THIS IS A SEPARATE PAGE FROM BillingModule
 * ----------------------------------------------
 * `BillingModule` is the ongoing billing console: it compares every plan, lists
 * invoices, manages payment methods and downgrade requests. This page does one
 * thing — size and buy ONE already-chosen plan — and it is reached from Ready,
 * before the customer has ever seen the console. Folding a checkout
 * configurator into a 2,600-line plan browser would have made both harder to
 * reason about. No billing logic is duplicated: every number on screen comes
 * from the same endpoints the console uses.
 *
 * WHAT IS AND IS NOT DECIDED HERE
 * -------------------------------
 * Nothing on this page computes money. The order summary is rendered from
 * `POST /api/v1/billing/quote`, which prices through `quotes.compute_amounts` —
 * the same function the paid path uses. The seat count, plan and cycle are a
 * *request*; the server clamps seats to the billable floor, enforces the plan
 * ceiling from the catalog, picks the Stripe Price from configuration, and
 * refuses anything it does not like. A tampered price, plan or quantity in the
 * browser changes what is asked for, never what is charged.
 *
 * The plan ceiling is also read from the catalog for an inline hint, so the
 * customer sees the problem while typing rather than after paying attention to
 * a modal. That hint is a courtesy — `POST /upgrade` re-checks it and is the
 * only thing that decides.
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Check, Minus, Plus, Lock, ShieldCheck, Building2, AlertTriangle } from "lucide-react";

import { API_BASE } from "../config";
import { authFetch } from "../api/authFetch";
import ActivationHeader from "../components/activation/ActivationHeader";

const CARD =
  "rounded-2xl border border-slate-200 bg-white shadow-[0_1px_2px_rgba(15,23,42,0.04)]";

/** Cents → display string, in the currency the server quoted. */
function money(cents, currency = "USD") {
  if (cents === null || cents === undefined) return "—";
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency", currency, minimumFractionDigits: 2,
    }).format(cents / 100);
  } catch {
    return `$${(cents / 100).toFixed(2)}`;
  }
}

function longDate(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" });
}

/** Currency label for the read-only field. Only what the server supports. */
const CURRENCY_LABELS = { USD: "USD – US Dollar" };


export default function SubscriptionConfigure() {
  const navigate = useNavigate();
  const [params] = useSearchParams();

  const [plans, setPlans] = useState(null);
  const [planCode, setPlanCode] = useState(params.get("plan") || null);
  const [cycle, setCycle] = useState(params.get("cycle") === "annual" ? "annual" : "monthly");
  const [seats, setSeats] = useState(null);

  const [quote, setQuote] = useState(null);
  const [quoting, setQuoting] = useState(false);
  const [quoteError, setQuoteError] = useState(null);

  const [paying, setPaying] = useState(false);
  const [payError, setPayError] = useState(null);
  const [limitBlock, setLimitBlock] = useState(null);

  const [loadError, setLoadError] = useState(null);

  // ── Catalog + current subscription ──────────────────────────────────────
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [pr, sr] = await Promise.all([
          authFetch(`${API_BASE}/api/admin/billing/plans`),
          authFetch(`${API_BASE}/api/admin/billing/summary`),
        ]);
        if (cancelled) return;
        if (!pr.ok) { setLoadError("We couldn't load the plans. Please try again."); return; }
        const { plans: list } = await pr.json();
        const active = (list || []).filter((p) => p.active_status !== false);
        setPlans(active);

        let sub = null;
        if (sr.ok) sub = (await sr.json())?.subscription || null;

        // Seed the plan from the URL, then the current subscription, then the
        // smallest plan that can actually hold this organisation — never a
        // literal.
        //
        // That last step used to be "whichever plan is flagged Recommended",
        // which meant a three-person workspace landed on Governed at $15 when
        // Verified at $8 covers it. The catalog's own `user_limit` answers the
        // question better: the cheapest tier the headcount fits in. This
        // mirrors the server's `smallest_plan_holding`, and it only picks the
        // starting selection — the customer can change it, and `/upgrade`
        // re-checks whatever they end up with.
        const headcount = Math.max(1, Number(sub?.billable_users) || 1);
        setPlanCode((prev) => {
          if (prev && active.some((p) => p.code === prev)) return prev;
          if (sub?.plan_code && active.some((p) => p.code === sub.plan_code)) return sub.plan_code;
          const selfServe = active
            .filter((p) => !p.is_enterprise)
            .sort((a, b) => (a.tier_order ?? 0) - (b.tier_order ?? 0));
          const fits = selfServe.find(
            (p) => p.user_limit === null || p.user_limit === undefined || headcount <= p.user_limit,
          );
          const popular = selfServe.find((p) => p.is_most_popular);
          return (fits || popular || selfServe[0] || {}).code || null;
        });
        // Seats: whatever the pricing page proposed, otherwise what the
        // organization actually has. Either way it is only a starting point —
        // `/upgrade` clamps it to the billable floor and refuses anything over
        // the plan ceiling, so the URL cannot buy a seat count the server has
        // not agreed to.
        const requested = Number.parseInt(params.get("seats") || "", 10);
        const fromUrl = Number.isFinite(requested) && requested > 0 ? requested : null;
        setSeats((prev) => prev ?? fromUrl ?? Math.max(1, Number(sub?.billable_users) || 1));
        if (sub?.billing_cycle && !params.get("cycle")) {
          setCycle(sub.billing_cycle === "annual" ? "annual" : "monthly");
        }
      } catch {
        if (!cancelled) setLoadError("We couldn't reach the billing service. Please try again.");
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const plan = useMemo(
    () => (plans || []).find((p) => p.code === planCode) || null,
    [plans, planCode],
  );
  const limit = plan?.user_limit ?? null;
  const overLimit = limit !== null && seats !== null && seats > limit;

  // ── Server-authoritative quote ──────────────────────────────────────────
  // Re-requested on every change to plan, seats or cycle. The last request to
  // be *sent* is not always the last to arrive, so responses are matched to a
  // request id and stale ones dropped — otherwise a fast click sequence can
  // leave the summary showing the price of a configuration nobody selected.
  const reqId = useRef(0);

  const refreshQuote = useCallback(async () => {
    if (!planCode || !seats || plan?.is_enterprise) return;
    const mine = ++reqId.current;
    setQuoting(true);
    setQuoteError(null);
    try {
      const res = await authFetch(`${API_BASE}/api/v1/billing/quote`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          plan: planCode, billing_cycle: cycle, seat_count: seats, currency: "USD",
        }),
      });
      if (mine !== reqId.current) return;          // superseded
      if (!res.ok) {
        setQuote(null);
        setQuoteError("We couldn't price that configuration. Adjust it and try again.");
        return;
      }
      setQuote(await res.json());
    } catch {
      if (mine === reqId.current) {
        setQuote(null);
        setQuoteError("We couldn't reach the pricing service. Please try again.");
      }
    } finally {
      if (mine === reqId.current) setQuoting(false);
    }
  }, [planCode, cycle, seats, plan]);

  useEffect(() => {
    const t = setTimeout(refreshQuote, 250);       // coalesce rapid +/- taps
    return () => clearTimeout(t);
  }, [refreshQuote]);

  // ── Proceed to payment ──────────────────────────────────────────────────
  async function proceed() {
    if (paying || !plan || !seats) return;
    setPaying(true);
    setPayError(null);
    setLimitBlock(null);
    try {
      const res = await authFetch(`${API_BASE}/api/admin/billing/upgrade`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ plan_code: plan.code, billing_cycle: cycle, seats }),
      });
      const body = await res.json().catch(() => ({}));

      if (res.status === 409 && body?.detail?.code === "PLAN_LIMIT_EXCEEDED") {
        setLimitBlock(body.detail);               // the server's own numbers
        return;
      }
      if (!res.ok) {
        const d = body?.detail;
        setPayError(typeof d === "string" ? d
          : "We couldn't start checkout. No payment has been taken. Please try again.");
        return;
      }
      if (body?.url) { window.location.assign(body.url); return; }
      // No url = the server changed an existing subscription in place.
      navigate("/settings/billing-subscription", { replace: true });
    } catch {
      setPayError("We couldn't reach the billing service. No payment has been taken.");
    } finally {
      setPaying(false);
    }
  }

  // ── Render ──────────────────────────────────────────────────────────────
  if (loadError) {
    return (
      <Shell>
        <div className={`${CARD} p-6`} role="alert">
          <p className="text-[14px] text-slate-700">{loadError}</p>
        </div>
      </Shell>
    );
  }
  if (!plans || !plan || seats === null) {
    return (
      <Shell>
        <div className={`${CARD} p-6`} aria-busy="true">
          <p className="text-[14px] text-slate-500">Loading your plan…</p>
        </div>
      </Shell>
    );
  }

  const currency = quote?.currency || plan.currency || "USD";
  const annualPct = plan.annual_discount_percentage;

  return (
    <Shell>
      <h1 className="mb-5 text-[22px] font-bold tracking-tight text-slate-900 sm:text-[26px]">
        Configure your subscription
      </h1>

      <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,1.55fr)_minmax(0,1fr)]">
        {/* ── Configuration ─────────────────────────────────────────────── */}
        <section className={`${CARD} p-5 sm:p-6`}>
          <Legend>Plan</Legend>
          <div className="rounded-xl border-2 border-brand-teal bg-brand-green-light/50 p-4">
            <div className="flex items-start gap-2.5">
              <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-brand-teal">
                <Check size={13} strokeWidth={3} className="text-white" aria-hidden />
              </span>
              <div>
                <p className="text-[14.5px] font-bold text-slate-900">
                  Temveris {plan.name}
                </p>
                {plan.short_description && (
                  <p className="mt-0.5 text-[12.5px] leading-snug text-slate-600">
                    {plan.short_description}
                  </p>
                )}
              </div>
            </div>
          </div>

          {/* Seats */}
          <Legend className="mt-5">Active workers</Legend>
          <div className="flex flex-wrap items-center gap-3">
            <div className="flex items-center gap-3">
              <StepBtn label="Decrease active workers" onClick={() => setSeats((n) => Math.max(1, n - 1))}
                       disabled={seats <= 1}>
                <Minus size={15} aria-hidden />
              </StepBtn>
              <input
                type="number" min={1} inputMode="numeric"
                aria-label="Active workers"
                value={seats}
                onChange={(e) => {
                  const n = parseInt(e.target.value, 10);
                  setSeats(Number.isFinite(n) && n > 0 ? n : 1);
                }}
                className="w-[74px] rounded-lg border border-transparent bg-transparent px-1 py-1 text-center text-[21px] font-bold tabular-nums text-slate-900 focus:border-brand-teal focus:outline-none focus:ring-2 focus:ring-brand-teal/20"
              />
              <StepBtn label="Increase active workers" onClick={() => setSeats((n) => n + 1)}>
                <Plus size={15} aria-hidden />
              </StepBtn>
            </div>
            <span className="text-[12.5px] text-slate-500">Update anytime</span>
          </div>
          {overLimit && (
            <p role="status" className="mt-2 flex items-start gap-1.5 text-[12.5px] leading-snug text-amber-700">
              <AlertTriangle size={13} className="mt-0.5 shrink-0" aria-hidden />
              {plan.name} supports up to {limit} seats. Continuing will ask you to choose a larger plan.
            </p>
          )}

          {/* Billing cycle */}
          <Legend className="mt-5">Billing</Legend>
          <div className="grid gap-3 sm:grid-cols-2" role="radiogroup" aria-label="Billing cycle">
            <CycleOption
              selected={cycle === "monthly"} onSelect={() => setCycle("monthly")}
              title="Monthly"
              price={`${money(Math.round((plan.monthly_price_per_user || 0) * 100), currency)} /active worker / month`}
              note="Billed monthly"
            />
            <CycleOption
              selected={cycle === "annual"} onSelect={() => setCycle("annual")}
              title="Annual (Prepaid)"
              price={`${money(Math.round((plan.annual_price_per_user || 0) * 100), currency)} /active worker / year`}
              note={`Equivalent to ${money(Math.round(((plan.annual_price_per_user || 0) / 12) * 100), currency)}/active worker / month`}
              badge={annualPct ? `Save ${Math.round(annualPct)}%` : null}
            />
          </div>

          {/* Currency — single supported value, shown for completeness. */}
          <Legend className="mt-5">Currency</Legend>
          <div className="rounded-xl border border-slate-200 bg-white px-3.5 py-2.5 text-[14px] text-slate-800">
            {CURRENCY_LABELS[currency] || currency}
          </div>
        </section>

        {/* ── Order summary ─────────────────────────────────────────────── */}
        <section className={`${CARD} overflow-hidden`} aria-live="polite" aria-busy={quoting}>
          <h2 className="border-b border-slate-100 px-5 py-4 text-[15px] font-bold text-slate-900">
            Order summary
          </h2>
          <div className="px-5 py-4">
            {quoteError ? (
              <p role="alert" className="text-[13px] leading-snug text-red-700">{quoteError}</p>
            ) : (
              <>
                <dl className="space-y-2.5">
                  <Row label="Active workers" value={quote ? quote.seat_count : seats} />
                  <Row label="Billing" value={cycle === "annual" ? "Annual (Prepaid)" : "Monthly"} />
                  <Row label="List price" value={money(quote?.subtotal_cents, currency)} />
                  {quote?.discount_cents > 0 && (
                    <Row
                      label={`Annual discount (${Math.round(annualPct || 0)}%)`}
                      value={`-${money(quote.discount_cents, currency)}`}
                      tone="credit"
                    />
                  )}
                  <Row label="Estimated tax" value={money(quote?.tax_cents ?? 0, currency)} />
                </dl>

                <div className="mt-3 border-t border-slate-100 pt-3">
                  <dl className="space-y-2.5">
                    <Row
                      label={cycle === "annual" ? "Annual subtotal" : "Monthly subtotal"}
                      value={quote ? `${money(quote.total_cents, currency)} ${currency}` : "—"}
                    />
                  </dl>
                </div>

                <div className="mt-3 flex items-baseline justify-between border-t border-slate-100 pt-3">
                  <span className="text-[15px] font-bold text-slate-900">Today's total</span>
                  {/* Named, because the same figure legitimately appears as the
                      list price when there is no discount and no tax. */}
                  <span aria-label="Today's total"
                        className="text-[22px] font-bold tabular-nums text-slate-900">
                    {money(quote?.total_cents, currency)}
                  </span>
                </div>

                <p className="mt-3 text-[11.5px] leading-snug text-slate-500">
                  {quote?.renewal_at
                    ? <>Renews on {longDate(quote.renewal_at)}. Cancel anytime · no hidden fees.</>
                    : <>Cancel anytime · no hidden fees.</>}
                </p>
              </>
            )}

            {payError && (
              <p role="alert" className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-[12.5px] leading-snug text-red-800">
                {payError}
              </p>
            )}

            <p className="mt-4 flex items-center justify-center gap-1.5 text-[12px] text-slate-500">
              <Lock size={12} aria-hidden /> Secure checkout
            </p>
            <button
              type="button"
              onClick={proceed}
              disabled={paying || quoting || !quote}
              className="mt-2 w-full rounded-xl bg-brand-blue px-4 py-3 text-[14.5px] font-semibold text-white transition-colors hover:bg-brand-blue-dark focus:outline-none focus:ring-2 focus:ring-brand-blue/30 focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {paying ? "Starting checkout…" : "Proceed to payment"}
            </button>
          </div>
        </section>
      </div>

      {limitBlock && (
        <PlanLimitModal
          detail={limitBlock}
          onClose={() => setLimitBlock(null)}
          onReduceSeats={() => setLimitBlock(null)}
          onSwitchPlan={(code) => { setLimitBlock(null); setPlanCode(code); }}
          onEnterprise={() => navigate("/contact-sales?reason=enterprise")}
        />
      )}
    </Shell>
  );
}


/* ── Plan-limit modal ─────────────────────────────────────────────────────
 * Every number and name here comes from the 409 body the server sent. The
 * component never decides that a limit was exceeded, which plan solves it, or
 * what that plan costs — it renders the server's answer.
 */
function PlanLimitModal({ detail, onClose, onReduceSeats, onSwitchPlan, onEnterprise }) {
  const ref = useRef(null);
  const current = detail.current_plan;
  const upgrade = detail.upgrade_plan;
  const enterprise = detail.enterprise_plan;

  useEffect(() => {
    ref.current?.focus();
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 px-4 py-6">
      <div
        ref={ref} tabIndex={-1}
        role="dialog" aria-modal="true" aria-labelledby="zt-limit-title"
        className="max-h-full w-full max-w-[560px] overflow-y-auto rounded-2xl bg-white p-5 shadow-xl focus:outline-none sm:p-6"
      >
        <p className="flex items-center gap-1.5 text-[11.5px] font-bold uppercase tracking-wide text-amber-700">
          <AlertTriangle size={13} aria-hidden /> Plan limit reached
        </p>
        <h2 id="zt-limit-title" className="mt-2 text-[19px] font-bold text-slate-900">
          You've exceeded the {current?.name} plan limit
        </h2>
        <p className="mt-1.5 text-[13px] leading-relaxed text-slate-600">
          {detail.message} To continue, choose a plan that fits your team size and requirements.
        </p>

        {current && (
          <p className="mt-3 rounded-lg bg-slate-50 px-3 py-2 text-[12.5px] text-slate-600">
            Current plan: <strong className="font-semibold text-slate-900">{current.name}</strong>
            {current.user_limit !== null && <> · Limit: {current.user_limit} seats</>}
          </p>
        )}

        <div className="mt-4 space-y-3">
          {upgrade && (
            <OptionCard
              icon={ShieldCheck}
              eyebrow={upgrade.name.toUpperCase()}
              headline={`${money(Math.round((upgrade.monthly_price_per_user || 0) * 100), upgrade.currency)} / active worker / month`}
              tagline={upgrade.badge}
              body={upgrade.short_description}
              action={`Switch to ${upgrade.name}`}
              onAction={() => onSwitchPlan(upgrade.code)}
              primary
            />
          )}
          {enterprise && (
            <OptionCard
              icon={Building2}
              eyebrow={enterprise.name.toUpperCase()}
              headline="Custom"
              tagline={enterprise.badge}
              body={enterprise.short_description}
              action="Request Enterprise Demo"
              onAction={onEnterprise}
            />
          )}
        </div>

        <div className="mt-5 flex items-center justify-between border-t border-slate-100 pt-4">
          <button type="button" onClick={onReduceSeats}
                  className="rounded text-[13px] font-semibold text-brand-green-dark underline-offset-2 hover:underline focus:outline-none focus:ring-2 focus:ring-brand-teal/30">
            Reduce seats to stay on {current?.name}
          </button>
          <button type="button" onClick={onClose}
                  className="rounded text-[13px] font-semibold text-slate-500 hover:text-slate-700 focus:outline-none focus:ring-2 focus:ring-slate-300">
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}


/* ── Small presentational pieces ─────────────────────────────────────────── */
function Shell({ children }) {
  return (
    <div className="min-h-screen bg-brand-canvas px-4 py-6 sm:py-8">
      <ActivationHeader current={4} className="mb-6" />

      <div className="mx-auto w-full max-w-[1000px]">
        {children}
      </div>
    </div>
  );
}

function Legend({ children, className = "" }) {
  return (
    <p className={`mb-2 text-[12.5px] font-semibold text-slate-800 ${className}`}>{children}</p>
  );
}

function StepBtn({ children, label, onClick, disabled }) {
  return (
    <button
      type="button" onClick={onClick} disabled={disabled} aria-label={label}
      className="flex h-9 w-9 items-center justify-center rounded-lg border border-slate-200 text-slate-700 transition-colors hover:bg-slate-50 focus:outline-none focus:ring-2 focus:ring-brand-teal/30 disabled:cursor-not-allowed disabled:opacity-40"
    >
      {children}
    </button>
  );
}

function CycleOption({ selected, onSelect, title, price, note, badge }) {
  return (
    <button
      type="button" role="radio" aria-checked={selected} onClick={onSelect}
      className={
        "relative rounded-xl border-2 p-3.5 text-left transition-colors focus:outline-none focus:ring-2 focus:ring-brand-teal/30 " +
        (selected ? "border-brand-teal bg-brand-green-light/50" : "border-slate-200 hover:border-slate-300")
      }
    >
      {badge && (
        <span className="absolute -top-2.5 right-3 rounded-full bg-brand-teal px-2 py-0.5 text-[10.5px] font-bold text-white">
          {badge}
        </span>
      )}
      <p className="text-[13.5px] font-bold text-slate-900">{title}</p>
      <p className="mt-1 text-[13px] font-semibold text-slate-800">{price}</p>
      <p className="mt-0.5 text-[11.5px] text-slate-500">{note}</p>
    </button>
  );
}

function Row({ label, value, tone }) {
  return (
    <div className="flex items-baseline justify-between gap-4">
      <dt className="text-[12.5px] text-slate-500">{label}</dt>
      <dd className={
        "text-right text-[12.5px] font-semibold tabular-nums " +
        (tone === "credit" ? "text-brand-teal" : "text-slate-900")
      }>
        {value}
      </dd>
    </div>
  );
}

function OptionCard({ icon, eyebrow, headline, tagline, body, action, onAction, primary }) {
  // Capitalized local rather than `{ icon: Icon }` in the parameter list:
  // eslint-plugin-react is not installed here, so a bare <Icon /> is not
  // counted as a usage and a destructured binding would be flagged unused. A
  // capitalized *variable* is covered by the config's varsIgnorePattern. Same
  // convention as installGuideParts.jsx and OrganizationSetup.jsx.
  const Icon = icon;

  return (
    <div className="flex flex-wrap items-center gap-3 rounded-xl border border-slate-200 p-3.5">
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-slate-50 text-slate-500">
        <Icon size={17} aria-hidden />
      </span>
      <div className="min-w-[180px] flex-1">
        <p className="text-[10.5px] font-bold uppercase tracking-wide text-slate-400">{eyebrow}</p>
        <p className="text-[15px] font-bold text-slate-900">{headline}</p>
        {tagline && <p className="text-[12.5px] font-semibold text-brand-blue">{tagline}</p>}
        {body && <p className="mt-0.5 text-[12px] leading-snug text-slate-500">{body}</p>}
      </div>
      <button
        type="button" onClick={onAction}
        className={
          "shrink-0 rounded-lg px-3.5 py-2 text-[13px] font-semibold transition-colors focus:outline-none focus:ring-2 focus:ring-offset-2 " +
          (primary
            ? "bg-brand-teal text-white hover:bg-brand-teal-dark focus:ring-brand-teal/40"
            : "border border-slate-300 text-slate-700 hover:bg-slate-50 focus:ring-slate-300")
        }
      >
        {action}
      </button>
    </div>
  );
}

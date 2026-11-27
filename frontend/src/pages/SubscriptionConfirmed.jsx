/**
 * Where Stripe sends the customer after a successful Checkout.
 *
 * Before this page, `success_url` pointed straight at
 * /settings/billing-subscription: the customer paid, then landed in a settings
 * console with a toast. This gives the moment somewhere to happen, and then
 * points them at something useful.
 *
 * WHAT IT IS ALLOWED TO CLAIM
 * ---------------------------
 * Returning from Stripe is not proof of payment — a Checkout Session reaches
 * `status: complete` even when `payment_status` is `unpaid`. So this page
 * claims nothing on arrival. It calls the EXISTING
 * `GET /api/admin/billing/upgrade/confirm`, which asks Stripe directly, refuses
 * anything that is not actually paid, refuses a session belonging to another
 * tenant, and writes the plan and renewal date from Stripe's own
 * `current_period_end`. Every fact on screen comes from that response.
 *
 * WHY THE TIMELINE IS TWO ROWS AND NOT FIVE
 * -----------------------------------------
 * The reference design showed "Entitlements applying", "Workspace preparing"
 * and "Ready" beneath the payment rows. None of those correspond to anything
 * this system does after a payment: the workspace is provisioned during signup,
 * long before the customer ever reaches checkout, and neither the confirm
 * endpoint nor the Stripe webhook recomputes entitlements on this path. Rows
 * that always complete instantly because nothing is behind them are an
 * animation, not a status — and this flow has been careful throughout not to
 * show invented progress. The two rows here are the two things that genuinely
 * happen.
 */

import React, { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  Check, AlertTriangle, ArrowRight, UserPlus, ShieldCheck, CheckSquare, BarChart3,
} from "lucide-react";

import { API_BASE } from "../config";
import { authFetch } from "../api/authFetch";
import ActivationHeader from "../components/activation/ActivationHeader";

const CARD =
  "rounded-2xl border border-slate-200 bg-white shadow-[0_1px_2px_rgba(15,23,42,0.04)]";

/** Each one goes somewhere that already exists. */
const NEXT_STEPS = [
  { icon: UserPlus, to: "/my-team", title: "Invite your team",
    body: "Add teammates and roles." },
  { icon: ShieldCheck, to: "/settings", title: "Configure policy controls",
    body: "Set rules and approvals." },
  { icon: CheckSquare, to: "/admin/leave", title: "Set up approvals",
    body: "Define approvers." },
  { icon: BarChart3, to: "/admin", title: "Explore analytics",
    body: "See insights and reports." },
];

function money(value, currency = "USD") {
  const n = Number(value);
  if (!Number.isFinite(n)) return null;
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(n);
  } catch {
    return `$${n.toFixed(2)}`;
  }
}

function longDate(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? null
    : d.toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" });
}

function longDateTime(d) {
  return d.toLocaleString(undefined, {
    year: "numeric", month: "long", day: "numeric",
    hour: "numeric", minute: "2-digit",
  });
}


export default function SubscriptionConfirmed() {
  const [params] = useSearchParams();
  const sessionId = params.get("session_id");

  // A missing session id is answerable before any render, so it is resolved
  // here rather than by a setState inside the effect below — which would queue
  // a second render immediately after the first for a fact already known.
  const [state, setState] = useState(() =>
    (sessionId ? { phase: "confirming" } : { phase: "no-session" }));
  // Confirming twice would ask Stripe twice for the same session. React 18
  // mounts effects twice in development, so the guard is not optional.
  const asked = useRef(false);

  useEffect(() => {
    if (asked.current) return;
    asked.current = true;

    if (!sessionId) return;      // already resolved in the initializer
    (async () => {
      try {
        const res = await authFetch(
          `${API_BASE}/api/admin/billing/upgrade/confirm?session_id=${encodeURIComponent(sessionId)}`,
        );
        const body = await res.json().catch(() => ({}));
        if (!res.ok) {
          setState({
            phase: "failed",
            // The endpoint's messages are already written for a customer
            // ("Payment not completed…", "…belongs to another organization").
            message: typeof body?.detail === "string"
              ? body.detail
              : "We couldn't confirm this payment. Your plan has not been changed.",
          });
          return;
        }
        setState({ phase: "ok", subscription: body.subscription || null, at: new Date() });
      } catch {
        setState({
          phase: "unreachable",
          message: "We couldn't reach the billing service to confirm your payment.",
        });
      }
    })();
  }, [sessionId]);

  return (
    <div className="min-h-screen bg-brand-canvas px-4 py-5 sm:py-6">
      <ActivationHeader steps={false} />

      <div className="mx-auto mt-5 w-full max-w-[1000px]">
        {state.phase === "confirming" && <Confirming />}
        {state.phase === "ok" && <Confirmed {...state} />}
        {(state.phase === "failed" || state.phase === "unreachable") && (
          <Problem message={state.message} retryable={state.phase === "unreachable"} />
        )}
        {state.phase === "no-session" && (
          <Problem
            message="This page needs a checkout reference from Stripe. If you've just paid, open your billing page to see your current plan."
            retryable={false}
          />
        )}
      </div>
    </div>
  );
}


function Confirming() {
  return (
    <section className={`${CARD} p-6 text-center`} aria-busy="true" aria-live="polite">
      <span className="loader mx-auto" />
      <h1 className="mt-3 text-[19px] font-bold text-slate-900">Confirming your payment…</h1>
      <p className="mt-1 text-[13.5px] text-slate-500">
        Checking with our payment provider. This only takes a moment.
      </p>
    </section>
  );
}


function Confirmed({ subscription, at }) {
  const planName = subscription?.plan_name || subscription?.plan?.name || "your plan";
  const cycle = subscription?.billing_cycle === "annual" ? "Annual" : "Monthly";
  const seats = subscription?.billable_users ?? subscription?.active_users;
  const renews = longDate(subscription?.renewal_date);
  const perUser = money(
    subscription?.billing_cycle === "annual"
      ? subscription?.plan?.annual_price_per_user
      : subscription?.plan?.monthly_price_per_user,
    subscription?.currency || "USD",
  );

  return (
    <>
      <header className="mb-5 flex items-start gap-3">
        <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-brand-green-light">
          <Check size={22} strokeWidth={3} className="text-brand-teal" aria-hidden />
        </span>
        <div>
          <h1 className="text-[24px] font-bold tracking-tight text-slate-900 sm:text-[27px]">
            Payment confirmed
          </h1>
          <p className="mt-0.5 text-[14px] text-slate-500">
            Your {planName} subscription is active.
          </p>
        </div>
      </header>

      <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)]">
        <section className={`${CARD} p-5 sm:p-6`}>
          <ol className="relative" aria-label="What happened">
            <Row
              title="Payment confirmed"
              detail={longDateTime(at)}
              last={false}
            />
            <Row
              title="Subscription active"
              detail={[
                planName,
                cycle,
                perUser && `${perUser}/worker`,
                Number.isFinite(Number(seats)) && `${seats} ${seats === 1 ? "seat" : "seats"}`,
              ].filter(Boolean).join(" · ")}
              last
            />
          </ol>

          {renews && (
            <p className="mt-4 rounded-xl bg-brand-green-light/60 px-4 py-3 text-[12.5px] leading-relaxed text-slate-700">
              <strong className="font-semibold">Next charge {renews}.</strong>{" "}
              Cancel any time from your billing page — no hidden fees.
            </p>
          )}

          <div className="mt-5 flex flex-col gap-2.5 sm:flex-row">
            <Link
              to="/admin"
              className="inline-flex items-center justify-center gap-1.5 rounded-xl bg-brand-teal px-5 py-3 text-[14.5px] font-semibold text-white transition-colors hover:bg-brand-teal-dark focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-teal/50 focus-visible:ring-offset-2"
            >
              Open Temveris <ArrowRight size={15} aria-hidden />
            </Link>
            <Link
              to="/settings/billing-subscription"
              className="inline-flex items-center justify-center rounded-xl border border-slate-300 px-5 py-3 text-[14.5px] font-semibold text-slate-700 transition-colors hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-slate-300 focus-visible:ring-offset-2"
            >
              View billing
            </Link>
          </div>
        </section>

        <section className={`${CARD} p-5 sm:p-6`}>
          <h2 className="text-[14px] font-bold text-slate-900">Recommended next steps</h2>
          <ul className="mt-3 space-y-1">
            {NEXT_STEPS.map((step) => {
              const Icon = step.icon;
              const { to, title, body } = step;
              return (
              <li key={to}>
                <Link
                  to={to}
                  className="-mx-2 flex items-start gap-2.5 rounded-xl px-2 py-2 transition-colors hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-teal/40"
                >
                  <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-brand-green-light">
                    <Icon size={14} className="text-brand-teal" aria-hidden />
                  </span>
                  <span className="min-w-0">
                    <span className="block text-[13px] font-semibold text-slate-900">{title}</span>
                    <span className="block text-[12px] leading-snug text-slate-500">{body}</span>
                  </span>
                </Link>
              </li>
              );
            })}
          </ul>
        </section>
      </div>
    </>
  );
}


/** One timeline row. The rail is drawn on the row above so it stops at the last. */
function Row({ title, detail, last }) {
  return (
    <li className="relative flex gap-3 pb-5 last:pb-0">
      {!last && (
        <span aria-hidden className="absolute left-[13px] top-7 bottom-0 w-px bg-brand-teal/35" />
      )}
      <span className="relative z-10 flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-full bg-brand-teal">
        <Check size={14} strokeWidth={3} className="text-white" aria-hidden />
      </span>
      <span className="min-w-0 pt-0.5">
        <span className="block text-[14px] font-semibold text-slate-900">{title}</span>
        {detail && <span className="block text-[12.5px] leading-snug text-slate-500">{detail}</span>}
      </span>
    </li>
  );
}


function Problem({ message, retryable }) {
  return (
    <section className={`${CARD} p-5 sm:p-6`} role="alert">
      <div className="flex items-start gap-3">
        <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-amber-50">
          <AlertTriangle size={19} className="text-amber-600" aria-hidden />
        </span>
        <div>
          <h1 className="text-[19px] font-bold text-slate-900">
            We couldn't confirm your payment
          </h1>
          <p className="mt-1 text-[13.5px] leading-relaxed text-slate-600">{message}</p>
          <p className="mt-2 text-[12.5px] leading-relaxed text-slate-500">
            {retryable
              ? "Your billing page shows the authoritative state — open it to check whether the change went through."
              : "Nothing has been charged twice. If your card was debited, your billing page will show it once our payment provider confirms."}
          </p>
        </div>
      </div>
      <div className="mt-4 flex flex-col gap-2.5 sm:flex-row">
        <Link
          to="/settings/billing-subscription"
          className="inline-flex items-center justify-center rounded-xl bg-brand-blue px-5 py-3 text-[14.5px] font-semibold text-white transition-colors hover:bg-brand-blue-dark focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-blue/40 focus-visible:ring-offset-2"
        >
          Open billing page
        </Link>
        <Link
          to="/subscription/configure"
          className="inline-flex items-center justify-center rounded-xl border border-slate-300 px-5 py-3 text-[14.5px] font-semibold text-slate-700 transition-colors hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-slate-300 focus-visible:ring-offset-2"
        >
          Back to plans
        </Link>
      </div>
    </section>
  );
}

/**
 * The plan-summary panel beside the Account form.
 *
 * Purely presentational: it renders the offer `useSignupOffer` fetched and
 * formats it. It does no pricing arithmetic and calculates no dates — the
 * server owns price, currency, discount, trial length and trial end, and this
 * component would rather show a dash than invent one of them.
 */

import { BadgeCheck, CheckCircle2 } from "lucide-react";

/** Cents → "$8" / "$6.40". Whole amounts drop the ".00", as the design shows. */
function money(cents, currency) {
  if (typeof cents !== "number") return null;
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: currency || "USD",
    minimumFractionDigits: cents % 100 === 0 ? 0 : 2,
    maximumFractionDigits: 2,
  }).format(cents / 100);
}

/** ISO-8601 from the server → "September 20, 2026" in the reader's locale. */
function longDate(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric", month: "long", day: "numeric",
  }).format(d);
}

export default function PlanSummaryCard({
  offer, error, loading, onRetry, mode = "trial", cycle,
}) {
  const subscribing = mode === "subscribe";
  const dueToday = offer ? money(offer.amount_due_today_cents, offer.currency) : null;
  const unitPrice = offer ? money(offer.unit_price_monthly_cents, offer.currency) : null;
  const trialEnds = offer ? longDate(offer.trial_ends_at) : null;

  // "Annual (Save 20%)" — the discount is the server's number, not a literal.
  // On the subscribe path the cadence comes from the pricing-page link, since
  // that is where the customer chose it; the trial offer has no opinion on it.
  const shownCycle = subscribing ? (cycle || "monthly") : offer?.billing_cycle;
  const cadence = offer
    ? (shownCycle === "annual"
        ? `Annual (Save ${Number(offer.annual_discount_pct)}%)`
        : "Monthly")
    : null;

  return (
    <aside
      aria-label="Your plan summary"
      aria-busy={loading || undefined}
      className="rounded-2xl border border-slate-200 bg-white p-5 shadow-[0_1px_2px_rgba(15,23,42,0.04)] sm:p-6"
    >
      <h2 className="text-[14px] font-bold text-slate-900">Your plan summary</h2>

      {/* Verified badge */}
      <div className="mt-3 flex items-center gap-2.5 rounded-xl bg-brand-green-light px-3.5 py-3">
        <BadgeCheck size={18} className="shrink-0 text-brand-teal" aria-hidden />
        <span className="text-[13.5px] font-semibold text-slate-900">
          {offer?.plan_label || "Temveris"}
        </span>
      </div>

      {error ? (
        <div className="mt-5" role="status">
          <p className="text-[12.5px] leading-snug text-slate-600">
            We couldn't load your plan summary just now. It doesn't affect your
            signup — you can still create your account.
          </p>
          <button
            type="button"
            onClick={onRetry}
            className="mt-2.5 rounded-lg text-[13px] font-semibold text-brand-green-dark underline underline-offset-2 hover:text-brand-teal-dark focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-teal/40"
          >
            Try again
          </button>
        </div>
      ) : (
        <>
          {/* No total on the subscribe path, deliberately. The customer has
              not chosen a seat count yet — that happens on the next screen —
              so any figure here would be the marketing page's guess or
              browser arithmetic. The authoritative total comes from the
              server quote on the configurator, for the seats they pick. */}
          <p className="mt-4 text-[22px] font-bold leading-none tracking-tight text-slate-900">
            {subscribing
              ? (unitPrice ? `${unitPrice} / worker` : <Skeleton className="h-7 w-40" />)
              : (dueToday ? `${dueToday} due today` : <Skeleton className="h-7 w-40" />)}
          </p>
          <p className="mt-1.5 text-[13px] text-slate-500">
            {subscribing
              ? "You'll choose seats and see the total next."
              : offer ? (offer.card_required ? "Card required" : "No card required") : " "}
          </p>

          <dl className="mt-4 space-y-2.5">
            {subscribing ? (
              <>
                <Row label="Plan" value={offer?.plan_label || null} />
                <Row label="Billing preference" value={cadence} />
                <Row label="Payment" value={offer ? "On the next step" : null} />
              </>
            ) : (
              <>
                <Row label="Expected paid price"
                     value={unitPrice ? `${unitPrice} / worker / mo` : null} />
                <Row label="Billing preference" value={cadence} />
                <Row label="Trial ends" value={trialEnds} />
              </>
            )}
          </dl>
        </>
      )}

      <div className="mt-5 flex items-start gap-2 border-t border-slate-100 pt-3.5">
        <CheckCircle2 size={15} className="mt-[2px] shrink-0 text-slate-400" aria-hidden />
        <p className="text-[11.5px] leading-snug text-slate-500">
          {subscribing
            ? "Nothing is charged until you confirm on the payment step."
            : "No automatic charge without your authorisation."}
        </p>
      </div>
    </aside>
  );
}

function Row({ label, value }) {
  return (
    <div className="flex items-baseline justify-between gap-4">
      <dt className="text-[12.5px] text-slate-500">{label}</dt>
      <dd className="text-right text-[12.5px] font-semibold text-slate-900">
        {value || <Skeleton className="h-3.5 w-24" />}
      </dd>
    </div>
  );
}

function Skeleton({ className }) {
  return (
    <span
      aria-hidden
      className={`inline-block animate-pulse rounded bg-slate-100 align-middle ${className}`}
    />
  );
}

/**
 * The reassurance panel beside the Verify step's form.
 *
 * It answers the question a security-conscious buyer is actually asking when a
 * time-tracking product wants their country, headcount and timezone: what is
 * this for, and how much of my team's life are you about to watch. Each line
 * names a real property of the platform rather than a benefit — region-aware
 * provisioning, append-only evidence — because a vague reassurance from a
 * monitoring product reads as evasion.
 *
 * Icons come from lucide-react, already the project's icon set.
 */

import { ShieldCheck, Lock, BadgeCheck, FileText } from "lucide-react";

const REASONS = [
  {
    icon: ShieldCheck,
    title: "Privacy by design",
    body: "We verify work, not private lives.",
  },
  {
    icon: Lock,
    title: "Secure provisioning",
    body: "Region-aware, encrypted workspace.",
  },
  {
    icon: BadgeCheck,
    title: "Right controls",
    body: "Policies matched to your setup.",
  },
  {
    icon: FileText,
    title: "Audit ready",
    body: "Append-only evidence from day one.",
  },
];

export default function WhyWeAsk() {
  return (
    <aside
      aria-labelledby="zt-why-we-ask"
      className="rounded-2xl border border-slate-200 bg-white p-5 shadow-[0_1px_2px_rgba(15,23,42,0.04)] sm:p-6"
    >
      <h2 id="zt-why-we-ask" className="text-[14px] font-bold text-slate-900">
        Why we ask
      </h2>
      <p className="mt-1.5 text-[12.5px] leading-relaxed text-slate-500">
        This helps us configure your workspace security and apply the right controls.
      </p>

      <ul className="mt-4 space-y-3.5">
        {REASONS.map((reason) => {
          // Bound to a local rather than destructured in the parameter list:
          // eslint's no-unused-vars does not see JSX usage of a destructured
          // arg, but the config's varsIgnorePattern (^[A-Z_]) covers a const.
          const Icon = reason.icon;
          return (
            <li key={reason.title} className="flex items-start gap-2.5">
              <Icon size={16} className="mt-[2px] shrink-0 text-brand-teal" aria-hidden />
              <div>
                <p className="text-[12.5px] font-semibold text-slate-900">{reason.title}</p>
                <p className="text-[12px] leading-snug text-slate-500">{reason.body}</p>
              </div>
            </li>
          );
        })}
      </ul>
    </aside>
  );
}

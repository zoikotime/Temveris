import { Check } from "lucide-react";

/**
 * ResetStepper — 3-step progress indicator for the forgot-password flow.
 * Shared across ForgotPassword (0), VerifyOtp (1) and ResetPassword (2) so the
 * flow reads as one coherent multi-step journey.
 *
 *   current: 0 = Email · 1 = Verify · 2 = Reset
 */
const STEPS = ["Email", "Verify", "Reset"];

export default function ResetStepper({ current = 0 }) {
  return (
    <div className="flex items-center justify-center mb-6 select-none" aria-hidden>
      {STEPS.map((label, i) => {
        const done = i < current;
        const active = i === current;
        return (
          <div key={label} className="flex items-center">
            <div className="flex items-center gap-2">
              <span
                className={
                  "flex items-center justify-center w-7 h-7 rounded-full text-[12px] font-bold transition-all duration-300 " +
                  (done
                    ? "bg-brand-green text-white"
                    : active
                      ? "bg-brand-blue text-white ring-4 ring-brand-blue/15"
                      : "bg-slate-100 text-slate-400")
                }
              >
                {done ? <Check size={14} strokeWidth={3} /> : i + 1}
              </span>
              <span
                className={
                  "text-[12.5px] font-semibold transition-colors duration-300 " +
                  (active ? "text-slate-800" : done ? "text-brand-green-dark" : "text-slate-400")
                }
              >
                {label}
              </span>
            </div>
            {i < STEPS.length - 1 && (
              <span
                className={
                  "mx-2.5 h-px w-6 sm:w-9 rounded-full transition-colors duration-300 " +
                  (done ? "bg-brand-green" : "bg-slate-200")
                }
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

/**
 * The four-step progress indicator across the top of the commercial activation
 * flow: Account → Verify → Workspace → Ready.
 *
 * Shared rather than per-page so the four screens cannot disagree about how
 * many steps there are or what they are called — the indicator is the only
 * thing telling a visitor how much is left, and a step list that changes
 * between screens reads as a broken flow.
 *
 * Accessibility: rendered as an ordered list with `aria-current="step"` on the
 * active item, and each step's state is spelled out in text for screen readers
 * ("completed" / "current step") rather than being carried by colour alone.
 * The connectors and the numerals are decorative and hidden from the tree.
 */

import { Check } from "lucide-react";

// Not exported: this file exports a component, and mixing the two breaks Fast
// Refresh (react-refresh/only-export-components, the same rule that forced
// challengeConfig.js out of SignupChallenge.jsx). Steps 2-4 pass `current`.
const ACTIVATION_STEPS = [
  { id: 1, label: "Account" },
  { id: 2, label: "Verify" },
  { id: 3, label: "Workspace" },
  { id: 4, label: "Ready" },
];

/**
 * @param {number} current  1-based id of the step being shown.
 */
export default function ActivationProgress({ current = 1 }) {
  return (
    <nav aria-label="Activation progress">
      <ol className="flex items-start justify-center">
        {ACTIVATION_STEPS.map((step, i) => {
          const done = step.id < current;
          const active = step.id === current;
          const state = done ? "completed" : active ? "current step" : "not started";

          return (
            <li
              key={step.id}
              className="flex items-start"
              aria-current={active ? "step" : undefined}
            >
              <div className="flex w-[78px] shrink-0 flex-col items-center sm:w-[92px]">
                <span
                  aria-hidden
                  className={
                    "flex h-8 w-8 items-center justify-center rounded-full border-2 " +
                    "text-[13px] font-semibold transition-colors " +
                    (done
                      ? "border-brand-teal bg-brand-teal text-white"
                      : active
                      ? "border-brand-teal bg-brand-teal text-white"
                      : "border-slate-200 bg-white text-slate-400")
                  }
                >
                  {done ? <Check size={15} strokeWidth={3} /> : step.id}
                </span>

                <span
                  className={
                    "mt-1.5 text-center text-[11.5px] leading-tight sm:text-[12px] " +
                    (active
                      ? "font-semibold text-slate-900"
                      : done
                      ? "font-medium text-slate-600"
                      : "text-slate-400")
                  }
                >
                  {step.label}
                </span>

                {/* The state in words, for anyone who cannot see the fill. */}
                <span className="sr-only">{`Step ${step.id} of ${ACTIVATION_STEPS.length}: ${state}`}</span>
              </div>

              {i < ACTIVATION_STEPS.length - 1 && (
                <span
                  aria-hidden
                  className={
                    "mt-[15px] -mx-2.5 h-px w-9 sm:w-16 " +
                    (done ? "bg-brand-teal/50" : "bg-slate-200")
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

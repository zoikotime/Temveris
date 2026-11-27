import { PASSWORD_RULES_STRICT } from "./passwordRules";

const STRENGTH_LABELS = ["Too weak", "Weak", "Fair", "Strong", "Strong"];
const STRENGTH_COLORS = ["#E11D48", "#F97316", "#F59E0B", "#10B981", "#059669"];

export default function PasswordStrengthMeter({
  value,
  rules = PASSWORD_RULES_STRICT,
  extraRules = [],
}) {
  const allRules = [...rules, ...extraRules];
  const score = allRules.reduce((n, r) => {
    const ok = r.re ? r.re.test(value) : !!r.test?.(value);
    return ok ? n + 1 : n;
  }, 0);
  const filled = value.length === 0 ? 0 : Math.max(1, Math.round((score / allRules.length) * 4));
  const tone = value.length === 0 ? 0 : Math.min(4, Math.floor((score / allRules.length) * 5));
  const isStrong = score === allRules.length;
  const label = value.length === 0 ? "" : STRENGTH_LABELS[tone];
  const accent = STRENGTH_COLORS[tone];

  return (
    <div className="space-y-2.5">
      <div className="flex gap-1.5" aria-hidden>
        {[0, 1, 2, 3].map((i) => (
          <div
            key={i}
            className="h-1.5 flex-1 rounded-full transition-colors duration-300"
            style={{ background: i < filled ? accent : "#E5E7EB" }}
          />
        ))}
      </div>

      {value.length > 0 && (
        <div
          className="rounded-xl px-3.5 py-3 border text-[12.5px] leading-tight"
          style={{
            background: isStrong ? "#ECFDF5" : "#FEF2F2",
            borderColor: isStrong ? "#A7F3D0" : "#FECACA",
            color: isStrong ? "#065F46" : "#991B1B",
          }}
        >
          <div className="font-semibold mb-1.5">
            Password strength: {label}
          </div>
          <div
            className="text-[11.5px] leading-snug"
            style={{ color: isStrong ? "#047857" : "#B91C1C" }}
          >
            {isStrong
              ? "Great job! This password meets all security requirements."
              : "This password does not meet Temveris's security standard. Please create a stronger password before continuing."}
          </div>
        </div>
      )}

      <ul className="space-y-1.5 pt-0.5">
        {allRules.map((r) => {
          const ok = value.length > 0 && (r.re ? r.re.test(value) : !!r.test?.(value));
          return (
            <li
              key={r.label}
              className="flex items-center gap-2 text-[12.5px]"
              style={{ color: ok ? "#047857" : value.length === 0 ? "#94A3B8" : "#B91C1C" }}
            >
              <span
                className="inline-block w-1.5 h-1.5 rounded-full"
                style={{ background: ok ? "#10B981" : value.length === 0 ? "#CBD5E1" : "#F87171" }}
              />
              {r.label}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

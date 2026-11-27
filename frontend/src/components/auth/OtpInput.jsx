/**
 * A row of single-digit cells for entering a numeric code.
 *
 * Extracted from pages/VerifyOtp.jsx, which had this behaviour inline and
 * hardcoded to four digits. The activation flow's Verify step needs six, and
 * the last thing this product needs is a second, subtly different
 * implementation of code entry — the interactions here are all the small ones
 * people notice only when they are missing:
 *
 *   • separate cells, so iOS/Android SMS autofill actually offers the code
 *   • auto-advance while typing, auto-rewind on Backspace
 *   • paste anywhere in the row distributes across the remaining cells
 *   • arrow keys move between cells; Enter submits when the row is full
 *   • non-digits are dropped rather than shown and then rejected
 *
 * Controlled: the parent owns `value` (a string, possibly shorter than
 * `length`) and receives every change. `onComplete` fires once the row is full
 * so the caller can auto-submit instead of asking for an extra click.
 *
 * Presentation is themeable through `tone` because the two callers sit on
 * different surfaces — password reset uses the blue auth card, activation uses
 * the teal flow — while the behaviour stays identical.
 */

import React, { useEffect, useRef, useState } from "react";

/** "123" → ["1","2","3","","",""] for length 6. */
function spread(value, length) {
  const out = Array(length).fill("");
  const src = String(value || "");
  for (let i = 0; i < Math.min(src.length, length); i++) out[i] = src[i];
  return out;
}

const TONES = {
  teal: {
    base: "border-slate-200 bg-white focus:border-brand-teal focus:ring-brand-teal/20",
    error: "border-red-300 bg-white focus:border-red-400 focus:ring-red-200",
  },
  blue: {
    base: "border-slate-200 bg-slate-50/70 focus:border-brand-blue focus:bg-white focus:ring-brand-blue/20",
    error: "border-red-400 bg-slate-50/70 focus:border-red-400 focus:ring-red-200",
  },
};

export default function OtpInput({
  value = "",
  onChange,
  onComplete,
  length = 6,
  disabled = false,
  invalid = false,
  autoFocus = true,
  tone = "teal",
  label = "Verification code",
  describedBy,
  id = "otp",
}) {
  const refs = useRef([]);

  // Positions are held internally as an array rather than derived from the
  // joined string. A string cannot represent a gap — someone who clicks the
  // fourth cell and types would have their digit silently slide to whatever
  // index the join produced. The parent still sees a plain string.
  const [digits, setDigits] = useState(() => spread(value, length));

  // Resync when the parent replaces the value outright — clearing the row after
  // a wrong code, or restoring a draft. Adjusted during render rather than in
  // an effect (React's documented pattern for state that follows a prop): an
  // effect would paint the stale row for a frame first, and the project's
  // eslint config rejects setState inside one. Guarded on the joined form so a
  // change this component just emitted does not bounce back and clobber a gap.
  const [lastValue, setLastValue] = useState(value);
  if (value !== lastValue) {
    setLastValue(value);
    if (digits.join("") !== value) setDigits(spread(value, length));
  }

  useEffect(() => {
    if (autoFocus) refs.current[0]?.focus();
  }, [autoFocus]);

  const emit = (next) => {
    setDigits(next);
    const joined = next.join("");
    onChange?.(joined);
    // `next.every` rather than a check on the joined string: `"".includes("")`
    // is true for every string, so testing the join for gaps never fails.
    if (next.every((d) => d !== "")) onComplete?.(joined);
  };

  const setAt = (idx, ch) => {
    const next = [...digits];
    next[idx] = ch;
    emit(next);
  };

  const handleChange = (idx, e) => {
    const onlyDigits = e.target.value.replace(/\D/g, "");
    if (onlyDigits.length === 0) {
      setAt(idx, "");
      return;
    }
    if (onlyDigits.length === 1) {
      setAt(idx, onlyDigits);
      if (idx < length - 1) refs.current[idx + 1]?.focus();
      return;
    }
    // More than one character in a single cell means an autofill or a paste
    // the browser routed through onChange — spread it across the row.
    const slice = onlyDigits.slice(0, length - idx);
    const next = [...digits];
    for (let i = 0; i < slice.length; i++) next[idx + i] = slice[i];
    emit(next);
    refs.current[Math.min(length - 1, idx + slice.length)]?.focus();
  };

  const handleKeyDown = (idx, e) => {
    if (e.key === "Backspace") {
      if (digits[idx]) {
        setAt(idx, "");
        return;
      }
      if (idx > 0) {
        refs.current[idx - 1]?.focus();
        setAt(idx - 1, "");
      }
    } else if (e.key === "ArrowLeft" && idx > 0) {
      refs.current[idx - 1]?.focus();
    } else if (e.key === "ArrowRight" && idx < length - 1) {
      refs.current[idx + 1]?.focus();
    }
  };

  const handlePaste = (e) => {
    const text = (e.clipboardData?.getData("text") || "").replace(/\D/g, "").slice(0, length);
    if (!text) return;
    e.preventDefault();
    const next = Array(length).fill("");
    for (let i = 0; i < text.length; i++) next[i] = text[i];
    emit(next);
    refs.current[Math.min(length - 1, text.length)]?.focus();
  };

  const palette = TONES[tone] || TONES.teal;

  return (
    <div
      role="group"
      aria-label={label}
      aria-describedby={describedBy}
      className="flex gap-2 sm:gap-2.5"
      onPaste={handlePaste}
    >
      {digits.map((d, i) => (
        <input
          key={i}
          id={i === 0 ? id : `${id}-${i}`}
          ref={(el) => (refs.current[i] = el)}
          type="text"
          inputMode="numeric"
          pattern="[0-9]*"
          // Only the first cell claims the one-time-code hint; repeating it on
          // every cell makes some browsers fill the whole code into each one.
          autoComplete={i === 0 ? "one-time-code" : "off"}
          maxLength={1}
          value={d}
          disabled={disabled}
          aria-label={`Digit ${i + 1} of ${length}`}
          aria-invalid={invalid ? "true" : undefined}
          onChange={(e) => handleChange(i, e)}
          onKeyDown={(e) => handleKeyDown(i, e)}
          className={
            "h-[52px] w-full min-w-0 rounded-xl border text-center text-[20px] font-semibold " +
            "text-slate-800 transition-colors focus:outline-none focus:ring-2 " +
            "disabled:bg-slate-100 disabled:text-slate-400 sm:h-14 sm:text-[22px] " +
            (invalid ? palette.error : palette.base)
          }
        />
      ))}
    </div>
  );
}

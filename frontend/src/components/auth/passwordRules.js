export const DEFAULT_PASSWORD_RULES = [
  { re: /.{12,}/,                 label: "At least 12 characters" },
  { re: /(?=.*[a-z])(?=.*[A-Z])/, label: "Uppercase and lowercase letters" },
  { re: /[0-9]/,                  label: "Number" },
  { re: /[^A-Za-z0-9]/,           label: "Symbol" },
];

const COMMON = new Set([
  "password", "password1", "12345678", "123456789", "qwerty", "qwerty123",
  "letmein", "welcome", "admin", "abc123", "iloveyou", "111111",
]);

export const PASSWORD_RULES_STRICT = [
  ...DEFAULT_PASSWORD_RULES,
  { test: (v) => v.length === 0 || !COMMON.has(v.toLowerCase()), label: "Not commonly used" },
];

export function meetsAllPasswordRules(value, rules = PASSWORD_RULES_STRICT, extraRules = []) {
  const all = [...rules, ...extraRules];
  return all.every((r) => (r.re ? r.re.test(value) : !!r.test?.(value)));
}

/**
 * The platform signup policy, mirroring `app.auth.service._PASSWORD_RULES`
 * character for character.
 *
 * Kept separate from DEFAULT_PASSWORD_RULES above, which is the *reset* flow's
 * policy and differs in two ways that matter: it wants 12 characters (stricter),
 * but it accepts any non-alphanumeric as a symbol (looser). The backend's signup
 * validator only counts `@$!%*?&`, so validating a signup password against the
 * reset rules would pass "correct-horse-1A" on the client and 422 it on the
 * server. The frontend must never be the weaker of the two, and it must never
 * disagree about *which* passwords are acceptable either.
 */
export const SIGNUP_PASSWORD_RULES = [
  { re: /.{8,}/,     label: "At least 8 characters" },
  { re: /[A-Z]/,     label: "An uppercase letter" },
  { re: /[a-z]/,     label: "A lowercase letter" },
  { re: /[0-9]/,     label: "A number" },
  { re: /[@$!%*?&]/, label: "A special character (@$!%*?&)" },
];

/** The rules `value` does not yet satisfy, in policy order. */
export function unmetPasswordRules(value, rules = SIGNUP_PASSWORD_RULES) {
  return rules.filter((r) => (r.re ? !r.re.test(value) : !r.test?.(value)));
}

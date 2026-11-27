/**
 * WF-ECO-03 — the Worker's own ECO v2 day view.
 *
 * ZT-SPEC-ECO-002 v2.0 §28. One day at a time, for the signed-in Worker only.
 *
 * THIS PAGE DECIDES NOTHING
 * -------------------------
 * The backend returns an already-rendered day: `status` is the locked §28.1
 * Status label and `body` is the approved explanation. Both are printed
 * verbatim. There is no status table here, no mapping from `evaluation_state`,
 * and no arithmetic over the minute fields — every number shown is a value the
 * server sent. That is not a stylistic preference: duplicating any of it would
 * create a second, unreviewed implementation of legally load-bearing product
 * semantics, and the two would drift.
 *
 * WHAT IS DELIBERATELY MISSING
 * ----------------------------
 * §28.1 also asks for an expanded derivation — the rounding basis in raw
 * seconds, and the exclusions considered and NOT applied. Neither is available:
 * `qualifying_seconds` is not persisted (a known §35 gap) and only the leave
 * exclusion category exists. Reconstructing either in the browser would mean
 * inventing figures the platform cannot vouch for, so this page shows what the
 * contract actually carries and no more. The weekly roll-up, the dispute
 * control and the seven-day view are future increments for the same reason.
 *
 * NOT IN NAVIGATION, ON PURPOSE
 * -----------------------------
 * Reachable by direct URL only. §45's OD-03 has not been ruled — the
 * specification titles this surface "My working time" but "My time record" is
 * the open alternative — and adding a menu entry would settle that by accident.
 */
import React, { useCallback, useEffect, useState } from "react";

import {
  ECO_DAY_ERROR,
  fetchMyEcoDay,
  todayLocalISO,
} from "../api/ecoDay";
import { useLanguage } from "../context/LanguageContext";
import { useTheme } from "../context/ThemeContext";
import TopNavbar from "../components/TopNavbar";

/** The measurements this surface shows, in §28's own column order.
 *
 *  Labels come from §28's day table, which is the specification's own wording
 *  for these columns. Provenance identifiers (ledger_version,
 *  evaluation_generation, policy_version, config_version,
 *  leave_input_snapshot_key) are deliberately absent: they belong to the
 *  derivation block, which is deferred, and mean nothing to a Worker. */
const FIGURES = [
  { key: "required_minutes",        label: "Required",   fallback: "Required" },
  { key: "qualifying_minutes",      label: "Qualifying", fallback: "Qualifying" },
  { key: "gross_shortfall_minutes", label: "Gross",      fallback: "Gross" },
  { key: "exclusion_minutes",       label: "Excluded",   fallback: "Excluded" },
  { key: "attributable_minutes",    label: "Counted",    fallback: "Counted" },
];

/** Minutes as "8h 45m" / "45m". Presentation only — it converts a single
 *  server-supplied number for display and combines nothing. */
const formatMinutes = (m) => {
  if (m === null || m === undefined) return "—";
  const total = Math.trunc(m);
  const sign = total < 0 ? "-" : "";
  const abs = Math.abs(total);
  const h = Math.floor(abs / 60);
  const mins = abs % 60;
  return h > 0 ? `${sign}${h}h ${mins}m` : `${sign}${mins}m`;
};

export default function MyWorkingTime() {
  const { T } = useLanguage();
  const { isDark } = useTheme();

  const [workDate, setWorkDate] = useState(() => todayLocalISO());
  const [day, setDay] = useState(null);
  const [state, setState] = useState("loading");   // loading | ready | unavailable | forbidden | error

  const load = useCallback(async (on) => {
    setState("loading");
    // Clear first: a failed refresh must never leave the previous day's
    // figures on screen under a new date.
    setDay(null);
    try {
      setDay(await fetchMyEcoDay(on));
      setState("ready");
    } catch (err) {
      if (err?.kind === ECO_DAY_ERROR.UNAVAILABLE) setState("unavailable");
      else if (err?.kind === ECO_DAY_ERROR.FORBIDDEN) setState("forbidden");
      else setState("error");
    }
  }, []);

  useEffect(() => { load(workDate); }, [workDate, load]);

  const C = {
    bg:      isDark ? "#0B1220" : "#F6F8FB",
    card:    isDark ? "#111A2B" : "#FFFFFF",
    border:  isDark ? "#1E2A3F" : "#E4E9F0",
    text:    isDark ? "#E6EDF7" : "#11253C",
    textSub: isDark ? "#93A4BC" : "#5B6B80",
  };

  return (
    <div style={{ minHeight: "100vh", background: C.bg, color: C.text,
                  fontFamily: "'DM Sans', sans-serif" }}>
      <TopNavbar />

      <main
        aria-busy={state === "loading"}
        style={{ maxWidth: 720, margin: "0 auto", padding: "24px 16px 64px" }}
      >
        <h1 style={{ fontSize: 22, fontWeight: 800, margin: "0 0 4px" }}>
          {T.myWorkingTime || "My working time"}
        </h1>

        <section style={{ margin: "20px 0" }}>
          <label
            htmlFor="eco-work-date"
            style={{ display: "block", fontSize: 13, fontWeight: 700,
                     color: C.textSub, marginBottom: 6 }}
          >
            {T.date || "Date"}
          </label>
          <input
            id="eco-work-date"
            type="date"
            value={workDate}
            max={todayLocalISO()}
            onChange={(e) => e.target.value && setWorkDate(e.target.value)}
            style={{
              padding: "10px 12px", fontSize: 15, borderRadius: 10,
              border: `1px solid ${C.border}`, background: C.card,
              color: C.text, fontFamily: "inherit",
            }}
          />
        </section>

        {/* One live region for every outcome, so a screen reader is told what
            happened rather than only sighted users seeing the panel swap. */}
        <div role="status" aria-live="polite">
          {state === "loading" && (
            <p style={{ color: C.textSub }}>{T.loading || "Loading…"}</p>
          )}

          {state === "unavailable" && (
            /* 404 — rollout OFF, shadow-blocked, or no record. The three are
               indistinguishable by design, so this says only what is true of
               all three and asserts nothing about why. */
            <p style={{ color: C.textSub }}>
              {T.noWorkingTimeRecord ||
                "No working-time record is available for this day."}
            </p>
          )}

          {state === "forbidden" && (
            <p style={{ color: C.textSub }}>
              {T.noAccessToRecord ||
                "You do not have access to this working-time record."}
            </p>
          )}

          {state === "error" && (
            <p style={{ color: C.textSub }}>
              {T.couldNotLoad || "We couldn't load this right now. Please try again."}
            </p>
          )}
        </div>

        {state === "ready" && day && (
          <article style={{
            background: C.card, border: `1px solid ${C.border}`,
            borderRadius: 14, padding: 20,
          }}>
            <h2 style={{ fontSize: 16, fontWeight: 800, margin: "0 0 12px" }}>
              {day.work_date}
            </h2>

            {/* Status is printed verbatim. When the backend sends null the
                whole pair is omitted — no placeholder, no "Unknown", and no
                orphaned label.

                Carried by a <dl> for the same reason the figures below are:
                <dt>/<dd> is a native term→definition pair, so the value is
                announced as "Status, Counted" and cannot be mistaken for the
                "Counted, 1h 0m" metric further down. §28's own day table heads
                this column "Status"; dropping that header when the weekly
                table became a card is what left the two occurrences of
                "Counted" indistinguishable. The label is read through the
                shared translation map like every other label on this page. */}
            {day.status && (
              <dl style={{ margin: "0 0 10px" }}>
                <dt style={{ fontSize: 12, fontWeight: 700, color: C.textSub }}>
                  {T.status || "Status"}
                </dt>
                <dd style={{ fontSize: 15, fontWeight: 700, margin: "2px 0 0" }}>
                  {day.status}
                </dd>
              </dl>
            )}

            {/* The approved explanation, exactly as the server rendered it. */}
            {day.body && (
              <p style={{ fontSize: 14, lineHeight: 1.55, color: C.textSub,
                          margin: "0 0 16px", whiteSpace: "pre-line" }}>
                {day.body}
              </p>
            )}

            <dl style={{
              display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))",
              gap: 12, margin: 0,
            }}>
              {FIGURES.map(({ key, label, fallback }) => (
                <div key={key}>
                  <dt style={{ fontSize: 12, fontWeight: 700, color: C.textSub }}>
                    {T[label.toLowerCase()] || fallback}
                  </dt>
                  <dd style={{ fontSize: 16, fontWeight: 700, margin: "2px 0 0" }}>
                    {formatMinutes(day[key])}
                  </dd>
                </div>
              ))}
            </dl>
          </article>
        )}
      </main>
    </div>
  );
}

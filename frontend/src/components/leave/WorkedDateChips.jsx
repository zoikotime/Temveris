/**
 * "Dates already worked" on a Compensatory Leave request (admin review modal).
 *
 * Each date links to Time Playback for that employee and day, opened in a new
 * tab, so the reviewer can confirm the work actually happened before
 * approving the comp-off. The Time Playback tab reads ?user=&date= and loads
 * on arrival (see TimelapseTab / AdminDashboard).
 */
import React from "react";

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

/** Deep link into Admin > Time Playback for one employee and one day. */
export function playbackLink(userId, date) {
  const p = new URLSearchParams({ tab: "timelapse", user: String(userId), date });
  return `/admin?${p.toString()}`;
}

/** The ?user=&date= a Time Playback deep link carries, or null if absent/invalid. */
export function readPlaybackDeepLink(search) {
  try {
    const p = new URLSearchParams(search);
    if (p.get("tab") !== "timelapse") return null;
    const user = p.get("user") || "";
    const date = p.get("date") || "";
    if (!/^\d+$/.test(user) || !ISO_DATE.test(date)) return null;
    return { user, date };
  } catch {
    return null;
  }
}

/** "7h 30m" / "45m" / "0m". */
export function fmtWorked(seconds) {
  const m = Math.round((Number(seconds) || 0) / 60);
  const h = Math.floor(m / 60), r = m % 60;
  return h ? `${h}h ${String(r).padStart(2, "0")}m` : `${r}m`;
}

/** What Temveris recorded on one claimed date (from /leave/{id}/worked-evidence). */
function Verdict({ ev, loading, T }) {
  const line = { fontSize: 12, fontWeight: 600 };
  if (loading) return <span style={{ ...line, color: "#64748B" }}>{T.checkingRecords || "Checking records…"}</span>;
  if (!ev || ev.valid === false) return null;
  if (!ev.worked_seconds) {
    return <span style={{ ...line, color: "#B91C1C" }}>❌ {T.noWorkRecorded || "No work recorded on this date"}</span>;
  }
  const parts = [`${T.worked || "Worked"} ${fmtWorked(ev.worked_seconds)}`];
  if (ev.first_clock_in) parts.push(`${ev.first_clock_in}–${ev.still_clocked_in ? (T.stillClockedIn || "still clocked in") : (ev.last_clock_out || "?")}`);
  parts.push(`${ev.screenshots || 0} ${T.screenshotsLower || "screenshots"}`);
  if (ev.adjustment_seconds) parts.push(`${T.inclManualAdjustment || "incl."} ${fmtWorked(ev.adjustment_seconds)} ${T.manualAdjustment || "manual adjustment"}`);
  return <span style={{ ...line, color: "#047857" }}>✅ {parts.join(" · ")}</span>;
}

export default function WorkedDateChips({ dates, userId, evidence, loading = false, T = {} }) {
  if (!Array.isArray(dates) || dates.length === 0) return null;
  const chip = {
    background: "#fff", border: "1px solid #86EFAC", color: "#166534", borderRadius: 12,
    padding: "3px 10px", fontSize: 12, fontWeight: 600, whiteSpace: "nowrap",
  };
  const byDate = Object.fromEntries((evidence || []).map(e => [e.date, e]));
  const showVerdicts = loading || Array.isArray(evidence);
  return (
    <div style={{ background: "#F0FDF4", border: "1px solid #BBF7D0", borderRadius: 8, padding: "12px 14px", marginBottom: 16 }}>
      <div style={{ fontSize: 13, fontWeight: 700, color: "#15803D", marginBottom: 4 }}>
        {/* Reviewer wording; `workedDatesLabel` is the employee's ("you"). */}
        🛠️ {T.workedDatesAdminLabel || "Dates worked (claimed)"} ({dates.length})
      </div>
      {userId != null && (
        <div style={{ fontSize: 12, color: "#166534", marginBottom: 8 }}>
          {showVerdicts
            ? (T.workedDatesEvidenceHint || "What Temveris recorded on each date. Click a date for the full day in Time Playback.")
            : (T.workedDatesVerifyHint || "Click a date to check that day in Time Playback.")}
        </div>
      )}
      <div style={{ display: "flex", flexDirection: showVerdicts ? "column" : "row", gap: 6, flexWrap: "wrap" }}>
        {dates.map((d, i) => {
          const link = (userId != null && ISO_DATE.test(String(d))) ? (
            <a href={playbackLink(userId, String(d))} target="_blank" rel="noopener noreferrer"
              title={T.workedDateOpenPlayback || "Open Time Playback for this day"}
              style={{ ...chip, textDecoration: "underline", cursor: "pointer" }}>
              {d} ↗
            </a>
          ) : (
            <span style={chip}>{d}</span>
          );
          return showVerdicts ? (
            <div key={i} data-testid="worked-date-row" style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
              {link}
              <Verdict ev={byDate[String(d)]} loading={loading} T={T} />
            </div>
          ) : <React.Fragment key={i}>{link}</React.Fragment>;
        })}
      </div>
    </div>
  );
}

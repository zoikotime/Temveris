/**
 * Pure helpers and constants behind the Live Workforce Overview
 * (OverviewLive.jsx). Kept apart from the components so the view file only
 * exports components (React fast refresh) and so the arithmetic can be tested
 * without rendering anything.
 */
import {
  effectiveStatus, computeShiftInfo, fmtTimeIST, istDateKey,
  normEmpType, getStatusCfg,
} from "./adminShared";

// Status colours: the Workforce Status System palette used by the chips.
export const OV_COLORS = {
  working:      "#16A34A",
  on_break:     "#F59E0B",
  not_started:  "#2563EB",
  idle:         "#64748B",
  on_leave:     "#7C3AED",
  absent:       "#DC2626",
  late_pending: "#EA580C",
  in_meeting:   "#0D9488",
  shift_ended:  "#94A3B8",
  other:        "#CBD5E1",
};

export const workforcePct = (n, total) =>
  total > 0 ? Math.round(((Number(n) || 0) / total) * 100) : 0;

export const fmtHm = (s) => {
  const v = Math.max(0, Math.floor(Number(s) || 0));
  return `${Math.floor(v / 3600)}h ${String(Math.floor((v % 3600) / 60)).padStart(2, "0")}m`;
};

export const navyFor = (isDark) => (isDark ? "#1F4B7A" : "#0F2846");

// A live-update socket can drop for a moment (a network blip, a token
// refresh) and reconnect on its own. Only a drop that lasts this long changes
// the Overview's LIVE badge, so it never flickers between states.
export const LIVE_GRACE_MS = 10000;

// Layout lives in a class sheet so the columns can collapse at narrower
// widths; inline styles cannot carry media queries.
export const OVERVIEW_CSS = `
  .zt-ov-layout { display: grid; grid-template-columns: minmax(0, 1fr) 340px; gap: 20px; align-items: start; }
  .zt-ov-main   { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
  .zt-ov-side   { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
  .zt-ov-big    { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
  .zt-ov-small  { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) minmax(0, 1.3fr); gap: 14px; }
  /* Hover lifts with a shadow only. Moving the card (translateY) made it
     flicker when the pointer rested on its bottom edge: the card slid out
     from under the pointer, lost hover, slid back, and repeated. */
  .zt-ov-card   { transition: box-shadow .18s ease; }
  .zt-ov-card:hover { box-shadow: 0 12px 24px -14px rgba(15,23,42,.45); }
  .zt-ov-card:focus-visible, .zt-ov-qa:focus-visible, .zt-ov-chip:focus-visible, .zt-ov-seg:focus-visible {
    outline: 2px solid #2563EB; outline-offset: 2px;
  }
  .zt-ov-qa { transition: box-shadow .18s ease, border-color .18s ease; }
  .zt-ov-qa:hover { box-shadow: 0 10px 20px -12px rgba(15,23,42,.45); border-color: #94A3B8 !important; }
  .zt-ov-row:hover td { background: var(--zt-ov-row-hover); }
  .zt-ov-table-wrap { overflow-x: auto; }
  /* Below ~1560px the three small cards no longer fit side by side: On Leave
     and Absent share a row and Requires Attention takes the full width. */
  @media (max-width: 1560px) {
    .zt-ov-small { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
    .zt-ov-small > :last-child { grid-column: 1 / -1; }
  }
  @media (max-width: 1440px) {
    .zt-ov-layout { grid-template-columns: minmax(0, 1fr) 300px; }
    .zt-ov-big { gap: 12px; }
    .zt-ov-big .zt-ov-card { padding: 14px 12px 0 !important; }
    .zt-ov-big-ic { width: 42px !important; height: 42px !important; border-radius: 12px !important; }
    .zt-ov-big-ic svg { width: 22px; height: 22px; }
    .zt-ov-big-num { font-size: 28px !important; }
    .zt-ov-big-foot { font-size: 11.5px !important; margin-top: 12px !important; }
  }
  @media (max-width: 1200px) {
    .zt-ov-layout { grid-template-columns: minmax(0, 1fr); }
    .zt-ov-side   { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); align-items: start; }
  }
  @media (max-width: 1100px) {
    .zt-ov-big   { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .zt-ov-small { grid-template-columns: minmax(0, 1fr); }
    .zt-ov-small > :last-child { grid-column: auto; }
  }
`;

const AVATAR_TONES = [
  ["#DCFCE7", "#166534"], ["#DBEAFE", "#1E40AF"], ["#F3E8FF", "#6B21A8"], ["#FFEDD5", "#9A3412"],
  ["#E0F2FE", "#075985"], ["#FCE7F3", "#9D174D"], ["#FEF9C3", "#854D0E"], ["#CCFBF1", "#115E59"],
];
export const avatarTone = (name = "") => {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return AVATAR_TONES[h % AVATAR_TONES.length];
};

const EMP_TYPE_LABEL = {
  full_time: "Full Time", part_time: "Part Time", temporary: "Temporary",
  casual: "Casual", field: "Field", factory: "Factory",
};

// One row's display values. Status follows the grid card (EmpCard): approved
// leave wins, and a worker past their start without a clock-in reads Absent.
export function rowView(emp, now = new Date()) {
  const shift = computeShiftInfo(emp);
  const raw = effectiveStatus(emp);
  const rawLower = String(raw || "").toLowerCase().replace("_", " ");
  const onLeave = rawLower === "on leave" || rawLower === "leave";
  const display = onLeave ? "on_leave"
    : (shift.phase === "late_pending" || shift.phase === "absent_after_noon") ? "absent"
    : raw;
  const cfg = getStatusCfg(display);
  const key = cfg.key || "";

  const today = !!emp.clock_in_time && istDateKey(emp.clock_in_time) === istDateKey(now);
  const worked = Number(emp.worked_today_seconds) || 0;
  const expected = Number(emp.expected_seconds);

  const rawPct = emp.active_pct != null
    ? Number(emp.active_pct)
    : (worked > 0 ? (100 * (Number(emp.active_today_seconds) || 0)) / worked : 0);
  const activePct = Math.max(65, Math.max(0, Math.min(100, Math.round(rawPct))));
  const showActive = ["working", "on_break", "idle"].includes(key);

  const type = normEmpType(emp.employment_type);
  return {
    cfg,
    clockIn: today ? fmtTimeIST(emp.clock_in_time) : null,
    worked: today && worked > 0 ? fmtHm(worked) : null,
    activePct: showActive ? activePct : null,
    progress: today && Number.isFinite(expected) && expected > 0
      ? Math.min(100, Math.round((worked / expected) * 100)) : null,
    progressTitle: Number.isFinite(expected) && expected > 0 ? `${fmtHm(worked)} / ${fmtHm(expected)}` : "",
    typeLabel: EMP_TYPE_LABEL[type] || type.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()),
  };
}

// The donut's slices: every bucket the cards report, plus the rarer states
// only when present, plus "Other" for anything uncounted, so the ring always
// adds up to the total in its centre.
export function distributionRows(counts, total, T = {}) {
  const base = [
    { k: "working",     label: T.working || "Working" },
    { k: "on_break",    label: T.onBreak || "On Break" },
    { k: "not_started", label: T.awaitingClockIn || "Awaiting Clock-In" },
    { k: "idle",        label: T.stoppedWorking || "Stopped Working" },
    { k: "on_leave",    label: T.onLeave || "On Leave" },
    { k: "absent",      label: T.absent || "Absent" },
  ];
  const rare = [
    { k: "late_pending", label: T.lateNotClockedIn || "Late, not clocked in" },
    { k: "in_meeting",   label: T.inMeeting || "In Meeting" },
    { k: "shift_ended",  label: T.shiftEnded || "Shift Ended" },
  ].filter((r) => (Number(counts[r.k]) || 0) > 0);
  const rows = [...base, ...rare].map((r) => ({ ...r, value: Number(counts[r.k]) || 0, color: OV_COLORS[r.k] }));
  const counted = rows.reduce((s, r) => s + r.value, 0);
  if (total > counted) rows.push({ k: "other", label: T.otherStatus || "Other", value: total - counted, color: OV_COLORS.other });
  return rows;
}

// A past date has three disjoint outcomes (clocked-out workers are inside
// Present), with the same "Other" remainder so the ring still adds up.
export function historyDistributionRows(history, total, T = {}) {
  const rows = [
    { k: "present",  label: T.present || "Present",   value: Number(history.present) || 0,  color: OV_COLORS.working },
    { k: "on_leave", label: T.onLeave || "On Leave",  value: Number(history.on_leave) || 0, color: OV_COLORS.on_leave },
    { k: "absent",   label: T.absent || "Absent",     value: Number(history.absent) || 0,   color: OV_COLORS.absent },
  ];
  const counted = rows.reduce((s, r) => s + r.value, 0);
  if (total > counted) rows.push({ k: "other", label: T.otherStatus || "Other", value: total - counted, color: OV_COLORS.other });
  return rows;
}

/**
 * Organization Timezone picker — a searchable combobox over the IANA zone list.
 *
 * Why not the simpler controls this replaced:
 *   • free text  — is how one workspace came to hold "California, Sacramento".
 *     Not a zone, so it silently fell back to another organization's clock
 *     while the settings screen showed a US city.
 *   • <datalist> — the options only appear once you start typing, so an admin
 *     who does not already know IANA naming sees an empty box.
 *   • <select>   — shows everything on click, but ~420 options cannot be
 *     filtered; the browser only jumps by prefix.
 *
 * So: click to see the whole list immediately, type to filter it, and nothing
 * outside the list can be chosen. The backend's `zoneinfo` check in
 * PUT /org/settings remains the authority — this component decides what is
 * offered, never what is valid.
 *
 * No dependency: the option list comes from the browser's own tz database via
 * `Intl.supportedValuesOf` (see lib/orgTime.js).
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import { groupedIanaZones, isSelectableZone, zoneLabel, zoneSearchText } from "../lib/orgTime";

const GROUPS = groupedIanaZones();
const FLAT = GROUPS.flatMap(g => g.zones);

// Rows rendered at once. Every row builds two Intl formatters for its
// human-readable name, so painting all 420 costs ~450ms — a visible stall on
// the click that opens the picker. 100 fills the panel several times over, and
// the search box is right there for anything further down.
const MAX_VISIBLE = 100;

// Pinned to the top of the unfiltered list. Without this the cap above hides
// them: the identifiers sort alphabetically, so "UTC" lands at position ~400
// and "Europe/London" past 250 — an admin who clicks to browse would never see
// either. These are the zones this product is actually configured with.
const COMMON_ZONES = [
  "UTC",
  "Asia/Kolkata",
  "Asia/Dubai",
  "Europe/London",
  "Europe/Berlin",
  "America/New_York",
  "America/Chicago",
  "America/Los_Angeles",
  "Australia/Sydney",
];

export default function TimezoneCombobox({
  value,
  onChange,
  C,
  T = {},
  invalid = false,
  id = "org-timezone",
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const rootRef = useRef(null);
  const inputRef = useRef(null);
  const listRef = useRef(null);

  // Filter on the identifier plus its aliases, so "Kolkata" finds
  // "Asia/Calcutta" (what most browsers expose) and vice versa. An admin
  // should not have to know which spelling their browser's tz database uses.
  // Unfiltered order: the zone currently saved (so it is always the first
  // thing an admin sees), then the common ones, then everything else
  // alphabetically. Searching drops the pinning and scans the whole list.
  const baseList = useMemo(() => {
    const pinned = [value, ...COMMON_ZONES].filter(z => z && isSelectableZone(z));
    return [...new Set([...pinned, ...FLAT])];
  }, [value]);

  const allMatches = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return baseList;
    return baseList.filter(z => zoneSearchText(z).includes(q));
  }, [query, baseList]);

  // What is actually rendered — and therefore what keyboard navigation moves
  // over, so Enter can never select a row the user cannot see.
  const matches = useMemo(() => allMatches.slice(0, MAX_VISIBLE), [allMatches]);
  const hiddenCount = allMatches.length - matches.length;

  // Keep the highlighted row inside the result set as it shrinks.
  useEffect(() => { setActiveIndex(0); }, [query]);

  useEffect(() => {
    if (!open) return undefined;
    const onDocMouseDown = (e) => {
      if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("mousedown", onDocMouseDown);
    return () => document.removeEventListener("mousedown", onDocMouseDown);
  }, [open]);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  // Scroll the active option into view for keyboard users.
  useEffect(() => {
    if (!open || !listRef.current) return;
    const el = listRef.current.querySelector(`[data-idx="${activeIndex}"]`);
    // Optional call: scrollIntoView is missing in jsdom and in a few older
    // mobile engines. Keyboard navigation still works without the scroll —
    // losing it must never take the whole picker down.
    el?.scrollIntoView?.({ block: "nearest" });
  }, [activeIndex, open]);

  const commit = (zone) => {
    onChange(zone);
    setOpen(false);
    setQuery("");
  };

  const onKeyDown = (e) => {
    if (!open) {
      if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        setOpen(true);
      }
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      setOpen(false);
      setQuery("");
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIndex(i => Math.min(i + 1, matches.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIndex(i => Math.max(i - 1, 0));
    } else if (e.key === "Home") {
      e.preventDefault();
      setActiveIndex(0);
    } else if (e.key === "End") {
      e.preventDefault();
      setActiveIndex(Math.max(0, matches.length - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (matches[activeIndex]) commit(matches[activeIndex]);
    } else if (e.key === "Tab") {
      setOpen(false);
    }
  };

  const border = invalid ? (C.danger || "#DC2626") : C.border;
  const listboxId = `${id}-listbox`;

  return (
    <div ref={rootRef} style={{ position: "relative" }}>
      {/* Closed state: a button, so the whole control is one tab stop and
          screen readers announce the current selection rather than an empty
          textbox. */}
      {!open && (
        <button
          type="button"
          id={id}
          onClick={() => setOpen(true)}
          onKeyDown={onKeyDown}
          role="combobox"
          aria-expanded="false"
          aria-controls={listboxId}
          aria-haspopup="listbox"
          aria-invalid={invalid || undefined}
          aria-label={T.organizationTimezone || "Organization Timezone"}
          style={{
            width: "100%", padding: "10px 12px", borderRadius: 7,
            border: `1px solid ${border}`, fontSize: 14, fontFamily: "inherit",
            background: C.surface, color: value ? C.text : C.textLight,
            outline: "none", boxSizing: "border-box", marginBottom: 4,
            textAlign: "left", cursor: "pointer",
            display: "flex", alignItems: "center", justifyContent: "space-between",
          }}>
          {/* Readable when the value is a real zone; shown verbatim when it is
              not, so a legacy value like "California, Sacramento" still reads
              back exactly as stored instead of being dressed up. */}
          <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {value
              ? (isSelectableZone(value) ? zoneLabel(value).label : value)
              : `— ${T.selectTimezone || "Select a timezone"} —`}
          </span>
          <span aria-hidden="true" style={{ opacity: 0.6, fontSize: 11, flexShrink: 0 }}>▼</span>
        </button>
      )}

      {open && (
        <>
          <input
            ref={inputRef}
            type="text"
            role="combobox"
            aria-expanded="true"
            aria-controls={listboxId}
            aria-autocomplete="list"
            aria-activedescendant={matches[activeIndex] ? `${id}-opt-${activeIndex}` : undefined}
            aria-label={T.searchTimezones || "Search timezones"}
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder={value || T.searchTimezones || "Search timezones"}
            style={{
              width: "100%", padding: "10px 12px", borderRadius: 7,
              border: `1px solid ${C.brand || border}`, fontSize: 14,
              fontFamily: "inherit", background: C.surface, color: C.text,
              outline: "none", boxSizing: "border-box", marginBottom: 4,
            }} />
          <ul
            ref={listRef}
            id={listboxId}
            role="listbox"
            aria-label={T.organizationTimezone || "Organization Timezone"}
            style={{
              position: "absolute", zIndex: 40, left: 0, right: 0,
              maxHeight: 260, overflowY: "auto", margin: 0, padding: 4,
              listStyle: "none", borderRadius: 8,
              border: `1px solid ${C.border}`, background: C.surface,
              boxShadow: "0 12px 32px rgba(0,0,0,.28)",
            }}>
            {matches.length === 0 && (
              <li style={{ padding: "10px 12px", fontSize: 13, color: C.textLight }}>
                {T.noTimezonesMatch || "No timezones match"}
              </li>
            )}
            {matches.map((zone, idx) => {
              const selected = zone === value;
              const active = idx === activeIndex;
              // Human-readable, but the VALUE committed is always `zone` — the
              // bare IANA identifier the API stores.
              const { name, offset, sameAs } = zoneLabel(zone);
              return (
                <li
                  key={zone}
                  id={`${id}-opt-${idx}`}
                  data-idx={idx}
                  role="option"
                  aria-selected={selected}
                  onMouseEnter={() => setActiveIndex(idx)}
                  onMouseDown={(e) => { e.preventDefault(); commit(zone); }}
                  style={{
                    padding: "8px 12px", borderRadius: 6, fontSize: 13,
                    cursor: "pointer",
                    background: active ? (C.hover || "rgba(127,127,127,.14)") : "transparent",
                    color: C.text,
                    display: "flex", justifyContent: "space-between",
                    alignItems: "baseline", gap: 12,
                  }}>
                  <span style={{ minWidth: 0 }}>
                    <span style={{ fontWeight: selected ? 700 : 500 }}>{zone}</span>
                    {name && (
                      <span style={{ color: C.textLight, fontSize: 12 }}> — {name}</span>
                    )}
                    {sameAs && (
                      <span style={{ color: C.textLight, fontSize: 11, fontStyle: "italic" }}>
                        {" "}· {T.sameZoneAs || "same as"} {sameAs}
                      </span>
                    )}
                  </span>
                  <span style={{ display: "flex", gap: 8, alignItems: "baseline", flexShrink: 0 }}>
                    {offset && (
                      <span style={{ color: C.textLight, fontSize: 11 }}>{offset}</span>
                    )}
                    {selected && <span aria-hidden="true" style={{ opacity: 0.7 }}>✓</span>}
                  </span>
                </li>
              );
            })}
            {hiddenCount > 0 && (
              <li aria-hidden="true"
                  style={{ padding: "8px 12px", fontSize: 11, color: C.textLight }}>
                {(T.timezoneMoreResults || "+{n} more — keep typing to narrow")
                  .replace("{n}", hiddenCount)}
              </li>
            )}
          </ul>
        </>
      )}
    </div>
  );
}

// Re-exported so callers validate against the same list the picker offers.
export { isSelectableZone };

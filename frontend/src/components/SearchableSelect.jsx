/**
 * SearchableSelect — a type-to-filter dropdown over a flat option list.
 *
 * Why not `<select>`: it shows everything on click and scrolls, but it cannot be
 * filtered — the browser only jumps by first letter. On a 60-person workspace
 * that means an admin scrolls a wall of names hunting for one person, and on a
 * few-hundred-person one it stops being usable at all.
 *
 * The interaction contract is deliberately the same as TimezoneCombobox
 * (click shows the whole list, type to narrow, keyboard-navigable, nothing
 * outside the list is selectable). That component stays as it is: it is welded
 * to the IANA zone helpers — per-row `Intl` formatters, offset/alias columns,
 * a pinned common-zones block — and generalising it would have meant reworking
 * the timezone picker and its suite to add a control elsewhere.
 *
 * Dumb by design: the caller formats `label` and supplies `search` text. This
 * file knows nothing about employees, teams, or any other domain.
 *
 * Options: [{ value, label, search? }]  — `search` defaults to `label`.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";

// One stylesheet for every instance. Inline styles cannot express
// ::-webkit-scrollbar, and the point of the exercise is a scrollbar you can
// actually see: Chrome's default overlay bar on a short panel is easy to miss.
const SCROLLBAR_CLASS = "zt-searchable-select-list";
const SCROLLBAR_CSS = `
.${SCROLLBAR_CLASS} { scrollbar-width: thin; }
.${SCROLLBAR_CLASS}::-webkit-scrollbar { width: 10px; }
.${SCROLLBAR_CLASS}::-webkit-scrollbar-track { background: transparent; }
.${SCROLLBAR_CLASS}::-webkit-scrollbar-thumb {
  background: rgba(127,127,127,.45); border-radius: 8px;
  border: 2px solid transparent; background-clip: content-box;
}
.${SCROLLBAR_CLASS}::-webkit-scrollbar-thumb:hover { background: rgba(127,127,127,.7); background-clip: content-box; }
`;

export default function SearchableSelect({
  value,
  onChange,
  options = [],
  C,
  placeholder = "Select…",
  searchPlaceholder = "Search…",
  emptyText = "No matches",
  ariaLabel,
  id = "searchable-select",
  minWidth = 300,
  maxListHeight = 280,
  marginBottom = 16,
  disabled = false,
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const rootRef = useRef(null);
  const inputRef = useRef(null);
  const listRef = useRef(null);

  const selected = useMemo(
    () => options.find(o => String(o.value) === String(value)) || null,
    [options, value],
  );

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return options;
    return options.filter(o =>
      String(o.search ?? o.label ?? "").toLowerCase().includes(q));
  }, [query, options]);

  const close = () => { setOpen(false); setQuery(""); };
  const commit = (opt) => { onChange(opt.value); close(); };

  // Keep the highlight inside the result set as it shrinks.
  useEffect(() => { setActiveIndex(0); }, [query]);

  // Opening lands on the current selection rather than the top of the list, so
  // reopening a picker mid-list does not lose the admin's place.
  useEffect(() => {
    if (!open) return;
    const i = matches.findIndex(o => String(o.value) === String(value));
    setActiveIndex(i >= 0 ? i : 0);
    // Only on open — re-running as `matches` changes would fight the arrow keys.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    if (!open) return undefined;
    const onDocMouseDown = (e) => {
      if (rootRef.current && !rootRef.current.contains(e.target)) close();
    };
    document.addEventListener("mousedown", onDocMouseDown);
    return () => document.removeEventListener("mousedown", onDocMouseDown);
  }, [open]);

  useEffect(() => { if (open) inputRef.current?.focus(); }, [open]);

  // Scroll the active row into view for keyboard users. Optional call:
  // scrollIntoView is absent in jsdom and some older mobile engines, and losing
  // the scroll must never take the whole picker down.
  useEffect(() => {
    if (!open || !listRef.current) return;
    listRef.current.querySelector(`[data-idx="${activeIndex}"]`)
      ?.scrollIntoView?.({ block: "nearest" });
  }, [activeIndex, open]);

  const onKeyDown = (e) => {
    if (!open) {
      if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        setOpen(true);
      }
      return;
    }
    if (e.key === "Escape") { e.preventDefault(); close(); }
    else if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIndex(i => Math.min(i + 1, matches.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIndex(i => Math.max(i - 1, 0));
    } else if (e.key === "Home") { e.preventDefault(); setActiveIndex(0); }
    else if (e.key === "End") { e.preventDefault(); setActiveIndex(Math.max(0, matches.length - 1)); }
    else if (e.key === "Enter") {
      e.preventDefault();
      if (matches[activeIndex]) commit(matches[activeIndex]);
    } else if (e.key === "Tab") close();
  };

  const listboxId = `${id}-listbox`;
  const label = ariaLabel || placeholder;

  return (
    <div ref={rootRef} style={{ position: "relative", minWidth, marginBottom }}>
      <style>{SCROLLBAR_CSS}</style>

      {/* Closed state is a button, so the control is one tab stop and screen
          readers announce the current selection instead of an empty textbox. */}
      {!open && (
        <button
          type="button"
          id={id}
          disabled={disabled}
          onClick={() => setOpen(true)}
          onKeyDown={onKeyDown}
          role="combobox"
          aria-expanded="false"
          aria-controls={listboxId}
          aria-haspopup="listbox"
          aria-label={label}
          style={{
            width: "100%", padding: "10px 12px", borderRadius: 8,
            border: `1px solid ${C.border}`, fontSize: 14, fontFamily: "inherit",
            background: C.surface, color: selected ? C.text : C.textLight,
            outline: "none", boxSizing: "border-box", textAlign: "left",
            cursor: disabled ? "not-allowed" : "pointer", opacity: disabled ? 0.6 : 1,
            display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10,
          }}>
          <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {selected ? selected.label : placeholder}
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
            aria-label={searchPlaceholder}
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder={selected ? selected.label : searchPlaceholder}
            style={{
              width: "100%", padding: "10px 12px", borderRadius: 8,
              border: `1px solid ${C.brand || C.border}`, fontSize: 14,
              fontFamily: "inherit", background: C.surface, color: C.text,
              outline: "none", boxSizing: "border-box",
            }} />
          {/* The popup is a plain container so the scrollable <ul> holds only
              options — the result count lives outside it rather than as a
              non-option child of a listbox. */}
          <div style={{
            position: "absolute", zIndex: 40, left: 0, right: 0, marginTop: 4,
            borderRadius: 8, border: `1px solid ${C.border}`,
            background: C.surface, boxShadow: "0 12px 32px rgba(0,0,0,.28)",
            overflow: "hidden",
          }}>
          <ul
            ref={listRef}
            className={SCROLLBAR_CLASS}
            id={listboxId}
            role="listbox"
            aria-label={label}
            style={{
              margin: 0, maxHeight: maxListHeight, overflowY: "auto",
              padding: 4, listStyle: "none",
            }}>
            {matches.length === 0 && (
              <li style={{ padding: "10px 12px", fontSize: 13, color: C.textLight }}>
                {emptyText}
              </li>
            )}
            {matches.map((opt, idx) => {
              const isSel = String(opt.value) === String(value);
              const active = idx === activeIndex;
              return (
                <li
                  key={opt.value}
                  id={`${id}-opt-${idx}`}
                  data-idx={idx}
                  role="option"
                  aria-selected={isSel}
                  onMouseEnter={() => setActiveIndex(idx)}
                  onMouseDown={(e) => { e.preventDefault(); commit(opt); }}
                  style={{
                    padding: "8px 12px", borderRadius: 6, fontSize: 13,
                    cursor: "pointer", color: C.text,
                    background: active ? (C.hover || "rgba(127,127,127,.14)") : "transparent",
                    fontWeight: isSel ? 700 : 500,
                    display: "flex", justifyContent: "space-between", gap: 12,
                  }}>
                  <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {opt.label}
                  </span>
                  {isSel && <span aria-hidden="true" style={{ opacity: 0.7, flexShrink: 0 }}>✓</span>}
                </li>
              );
            })}
          </ul>
          {/* Live count — with a long roster, "3 of 61" is how you know the
              filter did something rather than the list simply being short. */}
          {options.length > 0 && (
            <div aria-live="polite" style={{
              padding: "6px 12px", fontSize: 11, color: C.textLight,
              borderTop: `1px solid ${C.borderLight || C.border}`,
              background: C.surface, textAlign: "right",
            }}>
              {matches.length} of {options.length}
            </div>
          )}
          </div>
        </>
      )}
    </div>
  );
}

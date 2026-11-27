import React, { useState, useEffect, useRef } from "react";
import { useLanguage } from "../context/LanguageContext";

// Inline icon — keeps this component self-contained
const GlobeIcon = ({ size = 14, color = "#64748B" }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="12" r="10" />
    <line x1="2" y1="12" x2="22" y2="12" />
    <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
  </svg>
);

const ChevronIcon = ({ size = 12, color = "#64748B" }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="6 9 12 15 18 9" />
  </svg>
);

const SearchIcon = ({ size = 13, color = "#94A3B8" }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
  </svg>
);

export default function LangSwitcher({ isDark = false }) {
  const { lang, setLang, LANGUAGES, translating } = useLanguage();
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const ref = useRef(null);
  const searchRef = useRef(null);

  // Close on outside click
  useEffect(() => {
    const handler = (e) => {
      if (ref.current && !ref.current.contains(e.target)) {
        setOpen(false);
        setSearch("");
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  // Focus search when dropdown opens
  useEffect(() => {
    if (open && searchRef.current) {
      setTimeout(() => searchRef.current?.focus(), 50);
    }
  }, [open]);

  const surface  = isDark ? "#1E293B" : "#FFFFFF";
  const border   = isDark ? "#334155" : "#E2E8F0";
  const text     = isDark ? "#F1F5F9" : "#1E293B";
  const textSub  = isDark ? "#94A3B8" : "#64748B";
  const brand    = "#11496A";
  const brandBg  = isDark ? "rgba(17,73,106,0.18)" : "#E7F0F6";
  const hoverBg  = isDark ? "#334155" : "#F8FAFC";
  const inputBg  = isDark ? "#0F172A" : "#F8FAFC";

  // Get current language info from LANGUAGES list
  const curLang = LANGUAGES.find(l => l.code === lang);
  const curFlag = curLang?.flag || "🌐";
  const curName = curLang?.name || "English";

  const filtered = LANGUAGES.filter(l =>
    l.name.toLowerCase().includes(search.toLowerCase()) ||
    l.code.toLowerCase().includes(search.toLowerCase())
  );

  return (
    <div ref={ref} style={{ position: "relative", flexShrink: 0 }}>
      {/* Trigger button */}
      <button
        onClick={() => { setOpen(v => !v); setSearch(""); }}
        title="Change language"
        style={{
          display: "flex", alignItems: "center", gap: 5,
          padding: "5px 10px", borderRadius: 6,
          border: `1px solid ${border}`,
          background: surface, cursor: "pointer",
          fontSize: 12, fontWeight: 500, color: text,
          fontFamily: "inherit", whiteSpace: "nowrap",
        }}
      >
        <GlobeIcon size={14} color={textSub} />
        <span style={{ fontSize: 15 }}>{curFlag}</span>
        <span style={{ maxWidth: 80, overflow: "hidden", textOverflow: "ellipsis" }}>{translating ? "..." : curName}</span>
        <ChevronIcon size={11} color={textSub} />
      </button>

      {/* Dropdown */}
      {open && (
        <div style={{
          position: "absolute", right: 0, top: "calc(100% + 6px)",
          background: surface, border: `1px solid ${border}`,
          borderRadius: 10, boxShadow: "0 12px 40px rgba(0,0,0,0.18)",
          zIndex: 3000, width: 240,
          display: "flex", flexDirection: "column",
          overflow: "hidden",
        }}>
          {/* Search bar */}
          <div style={{ padding: "10px 10px 6px", borderBottom: `1px solid ${border}` }}>
            <div style={{ position: "relative" }}>
              <div style={{ position: "absolute", left: 8, top: "50%", transform: "translateY(-50%)" }}>
                <SearchIcon size={13} color={textSub} />
              </div>
              <input
                ref={searchRef}
                value={search}
                onChange={e => setSearch(e.target.value)}
                placeholder="Search languages..."
                style={{
                  width: "100%", boxSizing: "border-box",
                  padding: "6px 8px 6px 28px",
                  border: `1px solid ${border}`, borderRadius: 6,
                  background: inputBg, color: text,
                  fontSize: 12, fontFamily: "inherit", outline: "none",
                }}
              />
            </div>
          </div>

          {/* Language list */}
          <div style={{ maxHeight: 280, overflowY: "auto", padding: "6px 6px" }}>
            {filtered.length === 0 ? (
              <div style={{ padding: "12px 8px", textAlign: "center", fontSize: 12, color: textSub }}>
                No languages found
              </div>
            ) : (
              filtered.map(l => {
                const isActive = lang === l.code;
                return (
                  <button
                    key={l.code}
                    onClick={() => { setLang(l.code); setOpen(false); setSearch(""); }}
                    style={{
                      width: "100%", display: "flex", alignItems: "center", gap: 8,
                      padding: "7px 8px", border: "none", borderRadius: 6,
                      background: isActive ? brandBg : "transparent",
                      color: isActive ? brand : text,
                      fontWeight: isActive ? 600 : 400, fontSize: 12,
                      cursor: "pointer", fontFamily: "inherit",
                      textAlign: "left", marginBottom: 1,
                      transition: "background 0.1s",
                    }}
                    onMouseEnter={e => { if (!isActive) e.currentTarget.style.background = hoverBg; }}
                    onMouseLeave={e => { if (!isActive) e.currentTarget.style.background = "transparent"; }}
                  >
                    <span style={{ fontSize: 16, lineHeight: 1 }}>{l.flag}</span>
                    <span style={{ flex: 1 }}>{l.name}</span>
                    {isActive && (
                      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke={brand} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                        <polyline points="20 6 9 17 4 12" />
                      </svg>
                    )}
                  </button>
                );
              })
            )}
          </div>
        </div>
      )}
    </div>
  );
}

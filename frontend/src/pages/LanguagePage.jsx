import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useLanguage, LANGUAGES } from "../context/LanguageContext";
import { useTheme } from "../context/ThemeContext";
import { useAuth } from "../context/AuthContext";
import TopNavbar from "../components/TopNavbar";
import AdminSidebar from "../components/AdminSidebar";

// ── Icons ────────────────────────────────────────────────────────────────────
const Icon = ({ n, s = 16, c = "#64748B" }) => {
  const icons = {
    globe: <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke={c} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>,
    search: <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke={c} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>,
    check: <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke={c} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12"/></svg>,
    back: <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke={c} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="15 18 9 12 15 6"/></svg>,
    x: <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke={c} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>,
    moon: <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke={c} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>,
    sun: <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke={c} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>,
    star: <svg width={s} height={s} viewBox="0 0 24 24" fill={c} stroke={c} strokeWidth="1"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>,
  };
  return icons[n] || null;
};

// Popular / featured language codes shown at the top
const FEATURED = ["en", "hi", "es", "fr", "de", "ar", "zh", "ja", "pt", "ru", "ko", "it"];

export default function LanguagePage() {
  const navigate = useNavigate();
  const { lang, setLang, T } = useLanguage();
  const { isDark, toggleDark } = useTheme();
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState(lang);

  const C = {
    bg:        isDark ? "#0F172A" : "#F8FAFC",
    surface:   isDark ? "#1E293B" : "#FFFFFF",
    surface2:  isDark ? "#1A2234" : "#F8FAFC",
    border:    isDark ? "#334155" : "#E2E8F0",
    text:      isDark ? "#F1F5F9" : "#1E293B",
    textSub:   isDark ? "#94A3B8" : "#64748B",
    textLight: isDark ? "#64748B" : "#94A3B8",
    brand:     "#11496A",
    brandBg:   isDark ? "rgba(17,73,106,0.18)" : "#E7F0F6",
    brandBdr:  isDark ? "rgba(17,73,106,0.4)"  : "#A6C7DC",
    green:     "#44B97B",
    greenBg:   isDark ? "rgba(68,185,123,0.14)" : "#E8F7EE",
    input:     isDark ? "#0F172A" : "#FFFFFF",
    hover:     isDark ? "#263045" : "#F1F5F9",
  };

  const filteredAll = LANGUAGES.filter(l =>
    l.name.toLowerCase().includes(search.toLowerCase()) ||
    l.code.toLowerCase().includes(search.toLowerCase())
  );

  const featuredList = search ? [] : LANGUAGES.filter(l => FEATURED.includes(l.code));
  const otherList    = search
    ? filteredAll
    : filteredAll.filter(l => !FEATURED.includes(l.code));

  const handleSelect = (code) => {
    setSelected(code);
    setLang(code);
  };

  const { user } = useAuth();
  const role = (user?.role || localStorage.getItem("role") || "").toLowerCase();
  const isAdminRole = ["admin", "hr"].includes(role);
  const backPath = isAdminRole ? "/admin" : "/dashboard";

  const LanguageCard = ({ lang: l }) => {
    const isActive = selected === l.code;
    const hasTr    = true; // All languages supported via dynamic translation
    return (
      <button
        onClick={() => handleSelect(l.code)}
        style={{
          display: "flex", alignItems: "center", gap: 12,
          padding: "12px 16px", border: `1.5px solid ${isActive ? C.brand : C.border}`,
          borderRadius: 10, background: isActive ? C.brandBg : C.surface,
          cursor: "pointer", textAlign: "left", width: "100%",
          transition: "all 0.15s", fontFamily: "inherit",
          boxShadow: isActive ? `0 0 0 3px ${C.brandBdr}` : "none",
        }}
        onMouseEnter={e => { if (!isActive) e.currentTarget.style.background = C.hover; }}
        onMouseLeave={e => { if (!isActive) e.currentTarget.style.background = C.surface; }}
      >
        <span style={{ fontSize: 26, lineHeight: 1, flexShrink: 0 }}>{l.flag}</span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <span style={{ fontSize: 13, fontWeight: 600, color: isActive ? C.brand : C.text, lineHeight: 1.3 }}>
              {l.name}
            </span>
            {hasTr && (
              <span style={{ fontSize: 9, fontWeight: 700, color: C.green, background: C.greenBg, padding: "1px 5px", borderRadius: 4, textTransform: "uppercase", letterSpacing: 0.5 }}>
                {T.fullBadge || "Full"}
              </span>
            )}
          </div>
          <div style={{ fontSize: 11, color: C.textSub, marginTop: 1 }}>
            {l.code.toUpperCase()} · {l.dir === "rtl" ? (T.rightToLeft || "Right-to-left") : (T.leftToRight || "Left-to-right")}
          </div>
        </div>
        {isActive && (
          <div style={{ width: 20, height: 20, borderRadius: "50%", background: C.brand, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
            <Icon n="check" s={11} c="#fff" />
          </div>
        )}
      </button>
    );
  };

  const SectionLabel = ({ label, count }) => (
    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10, marginTop: 4 }}>
      <span style={{ fontSize: 11, fontWeight: 700, color: C.textSub, textTransform: "uppercase", letterSpacing: 0.8 }}>
        {label}
      </span>
      <span style={{ fontSize: 11, color: C.textLight, background: C.surface2, border: `1px solid ${C.border}`, borderRadius: 99, padding: "1px 7px" }}>
        {count}
      </span>
    </div>
  );

  const Body = (
    <div style={{ maxWidth: 900, margin: "0 auto", padding: "32px 24px" }}>

        {/* Hero */}
        <div style={{ marginBottom: 28, textAlign: "center" }}>
          <div style={{ width: 56, height: 56, borderRadius: 16, background: C.brandBg, border: `1px solid ${C.brandBdr}`, display: "flex", alignItems: "center", justifyContent: "center", margin: "0 auto 14px" }}>
            <Icon n="globe" s={26} c={C.brand} />
          </div>
          <h1 style={{ fontSize: 22, fontWeight: 800, color: C.text, margin: 0 }}>{T.chooseYourLanguage || "Choose your language"}</h1>
          <p style={{ fontSize: 13, color: C.textSub, marginTop: 6 }}>
            {LANGUAGES.length} {T.languagesAvailable || "languages available · Changes apply instantly across the whole app"}
          </p>
        </div>

        {/* Current language badge */}
        {(() => {
          const cur = LANGUAGES.find(l => l.code === selected);
          const hasTr = true; // All languages supported via dynamic translation
          return (
            <div style={{ display: "flex", alignItems: "center", gap: 10, background: C.brandBg, border: `1px solid ${C.brandBdr}`, borderRadius: 10, padding: "10px 16px", marginBottom: 24 }}>
              <span style={{ fontSize: 22 }}>{cur?.flag}</span>
              <div>
                <div style={{ fontSize: 13, fontWeight: 700, color: C.brand }}>{cur?.name}</div>
                <div style={{ fontSize: 11, color: C.textSub }}>
                  {hasTr ? (T.fullTranslationAvailable || "Full translation available") : (T.englishFallbackNote || "Interface will show in English (translation coming soon)")}
                </div>
              </div>
              <div style={{ marginLeft: "auto" }}>
                <Icon n="check" s={18} c={C.brand} />
              </div>
            </div>
          );
        })()}

        {/* Search */}
        <div style={{ position: "relative", marginBottom: 24 }}>
          <div style={{ position: "absolute", left: 12, top: "50%", transform: "translateY(-50%)" }}>
            <Icon n="search" s={15} c={C.textSub} />
          </div>
          <input
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder={T.searchLanguagesPlaceholder || "Search languages by name or code..."}
            style={{
              width: "100%", boxSizing: "border-box",
              padding: "11px 36px 11px 38px",
              border: `1px solid ${C.border}`, borderRadius: 10,
              background: C.input, color: C.text,
              fontSize: 13, fontFamily: "inherit", outline: "none",
            }}
          />
          {search && (
            <button
              onClick={() => setSearch("")}
              style={{ position: "absolute", right: 10, top: "50%", transform: "translateY(-50%)", border: "none", background: "transparent", cursor: "pointer", padding: 2 }}
            >
              <Icon n="x" s={14} c={C.textSub} />
            </button>
          )}
        </div>

        {/* Featured languages */}
        {!search && (
          <div style={{ marginBottom: 28 }}>
            <SectionLabel label={T.popularLanguages || "Popular languages"} count={featuredList.length} />
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))", gap: 8 }}>
              {featuredList.map(l => <LanguageCard key={l.code} lang={l} />)}
            </div>
          </div>
        )}

        {/* All / filtered languages */}
        <div>
          <SectionLabel
            label={search ? `${T.resultsFor || "Results for"} "${search}"` : (T.allLanguages || "All languages")}
            count={otherList.length}
          />
          {otherList.length === 0 ? (
            <div style={{ textAlign: "center", padding: "32px 0", color: C.textSub, fontSize: 13 }}>
              {T.noLanguagesMatch || "No languages match your search."}
            </div>
          ) : (
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))", gap: 8 }}>
              {otherList.map(l => <LanguageCard key={l.code} lang={l} />)}
            </div>
          )}
        </div>

        {/* Footer note */}
        <div style={{ marginTop: 40, textAlign: "center", fontSize: 11, color: C.textLight }}>
          <strong style={{ color: C.textSub }}>{T.fullBadge || "Full"}</strong> {T.fullBadgeNote || "badge means the app is fully translated into that language. Others display in English while translation is in progress."}
        </div>
    </div>
  );

  return (
    <div style={{ display: "flex", height: "100vh", background: C.bg, fontFamily: "'DM Sans', system-ui, sans-serif" }}>
      {isAdminRole && <AdminSidebar />}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, overflow: "hidden" }}>
        <TopNavbar
          title={T.languageSettings || "Language Settings"}
          onBack={() => navigate(backPath)}
        />
        <main style={{ flex: 1, overflow: "auto" }}>
          {Body}
        </main>
      </div>
    </div>
  );
}

import React, { useState } from "react";

const languages = [
  { code: "en",    name: "English",             flag: "🇬🇧" },
  { code: "es",    name: "Spanish",             flag: "🇪🇸" },
  { code: "zh",    name: "Chinese (Simplified)", flag: "🇨🇳" },
  { code: "zh-TW", name: "Chinese (Traditional)", flag: "🇹🇼" },
  { code: "fr",    name: "French",              flag: "🇫🇷" },
  { code: "de",    name: "German",              flag: "🇩🇪" },
  { code: "pt",    name: "Portuguese",          flag: "🇧🇷" },
  { code: "ja",    name: "Japanese",            flag: "🇯🇵" },
  { code: "ko",    name: "Korean",              flag: "🇰🇷" },
  { code: "ru",    name: "Russian",             flag: "🇷🇺" },
  { code: "ar",    name: "Arabic",              flag: "🇸🇦" },
  { code: "id",    name: "Indonesian",          flag: "🇮🇩" },
  { code: "tr",    name: "Turkish",             flag: "🇹🇷" },
  { code: "it",    name: "Italian",             flag: "🇮🇹" },
  { code: "pl",    name: "Polish",              flag: "🇵🇱" },
  { code: "nl",    name: "Dutch",               flag: "🇳🇱" },
  { code: "sv",    name: "Swedish",             flag: "🇸🇪" },
  { code: "no",    name: "Norwegian",           flag: "🇳🇴" },
  { code: "vi",    name: "Vietnamese",          flag: "🇻🇳" },
  { code: "th",    name: "Thai",                flag: "🇹🇭" },
  { code: "bn",    name: "Bengali",             flag: "🇧🇩" },
  { code: "ur",    name: "Urdu",                flag: "🇵🇰" },
  { code: "fi",    name: "Finnish",             flag: "🇫🇮" },
  { code: "da",    name: "Danish",              flag: "🇩🇰" },
  { code: "uk",    name: "Ukrainian",           flag: "🇺🇦" },
  { code: "ro",    name: "Romanian",            flag: "🇷🇴" },
  { code: "el",    name: "Greek",               flag: "🇬🇷" },
  { code: "ka",    name: "Georgian",            flag: "🇬🇪" },
  { code: "lv",    name: "Latvian",             flag: "🇱🇻" },
  { code: "hi",    name: "Hindi",               flag: "🇮🇳" },
];

export default function AdminLanguageSelector({ currentLang, setLang, onClose }) {
  const [search, setSearch] = useState("");

  const filtered = languages.filter(
    l =>
      l.name.toLowerCase().includes(search.toLowerCase()) ||
      l.code.toLowerCase().includes(search.toLowerCase())
  );

  return (
    <div style={{
      position: "absolute", top: "100%", right: 0, zIndex: 9999,
      background: "#fff", border: "1px solid #E4E7EC", borderRadius: 10,
      boxShadow: "0 8px 32px rgba(0,0,0,.14)", width: 240, maxHeight: 320,
      display: "flex", flexDirection: "column", overflow: "hidden",
    }}>
      <div style={{ padding: "8px 10px", borderBottom: "1px solid #F2F4F7" }}>
        <input
          autoFocus
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="Search language..."
          style={{
            width: "100%", padding: "5px 8px", fontSize: 13,
            border: "1px solid #E4E7EC", borderRadius: 6, outline: "none",
          }}
        />
      </div>
      <div style={{ overflowY: "auto", flex: 1 }}>
        {filtered.map(lang => (
          <button
            key={lang.code}
            onClick={() => { setLang(lang.code); onClose && onClose(); }}
            style={{
              width: "100%", display: "flex", alignItems: "center", gap: 8,
              padding: "8px 12px", border: "none", background: currentLang === lang.code ? "#E7F0F6" : "transparent",
              color: currentLang === lang.code ? "#11496A" : "#101828",
              fontWeight: currentLang === lang.code ? 700 : 400,
              fontSize: 13, cursor: "pointer", textAlign: "left", fontFamily: "inherit",
            }}
          >
            <span style={{ fontSize: 18 }}>{lang.flag}</span>
            <span>{lang.name}</span>
            {currentLang === lang.code && (
              <span style={{ marginLeft: "auto", color: "#11496A", fontWeight: 700 }}>✓</span>
            )}
          </button>
        ))}
        {filtered.length === 0 && (
          <p style={{ padding: "12px", textAlign: "center", fontSize: 12, color: "#98A2B3" }}>No language found</p>
        )}
      </div>
    </div>
  );
}

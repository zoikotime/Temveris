import React, { createContext, useContext, useState, useEffect, useCallback, useMemo, useRef } from "react";
import { API_BASE } from "../config";

// ── ALL GLOBAL LANGUAGES ────────────────────────────────────────────────────
export const LANGUAGES = [
  { code: "af", name: "Afrikaans",          flag: "🇿🇦", dir: "ltr" },
  { code: "sq", name: "Albanian",           flag: "🇦🇱", dir: "ltr" },
  { code: "am", name: "Amharic",            flag: "🇪🇹", dir: "ltr" },
  { code: "ar", name: "Arabic",             flag: "🇸🇦", dir: "rtl" },
  { code: "hy", name: "Armenian",           flag: "🇦🇲", dir: "ltr" },
  { code: "az", name: "Azerbaijani",        flag: "🇦🇿", dir: "ltr" },
  { code: "eu", name: "Basque",             flag: "🏴", dir: "ltr" },
  { code: "be", name: "Belarusian",         flag: "🇧🇾", dir: "ltr" },
  { code: "bn", name: "Bengali",            flag: "🇧🇩", dir: "ltr" },
  { code: "bs", name: "Bosnian",            flag: "🇧🇦", dir: "ltr" },
  { code: "bg", name: "Bulgarian",          flag: "🇧🇬", dir: "ltr" },
  { code: "ca", name: "Catalan",            flag: "🏴󠁥󠁳󠁣󠁴󠁿", dir: "ltr" },
  { code: "zh", name: "Chinese (Simplified)", flag: "🇨🇳", dir: "ltr" },
  { code: "zh-TW", name: "Chinese (Traditional)", flag: "🇹🇼", dir: "ltr" },
  { code: "hr", name: "Croatian",           flag: "🇭🇷", dir: "ltr" },
  { code: "cs", name: "Czech",              flag: "🇨🇿", dir: "ltr" },
  { code: "da", name: "Danish",             flag: "🇩🇰", dir: "ltr" },
  { code: "nl", name: "Dutch",              flag: "🇳🇱", dir: "ltr" },
  { code: "en", name: "English (US)",       flag: "🇺🇸", dir: "ltr" },
  { code: "en-GB", name: "English (UK)",    flag: "🇬🇧", dir: "ltr" },
  { code: "et", name: "Estonian",           flag: "🇪🇪", dir: "ltr" },
  { code: "fi", name: "Finnish",            flag: "🇫🇮", dir: "ltr" },
  { code: "fr", name: "French",             flag: "🇫🇷", dir: "ltr" },
  { code: "gl", name: "Galician",           flag: "🏴󠁥󠁳󠁧󠁡󠁿", dir: "ltr" },
  { code: "ka", name: "Georgian",           flag: "🇬🇪", dir: "ltr" },
  { code: "de", name: "German",             flag: "🇩🇪", dir: "ltr" },
  { code: "el", name: "Greek",              flag: "🇬🇷", dir: "ltr" },
  { code: "gu", name: "Gujarati",           flag: "🇮🇳", dir: "ltr" },
  { code: "ht", name: "Haitian Creole",     flag: "🇭🇹", dir: "ltr" },
  { code: "ha", name: "Hausa",              flag: "🇳🇬", dir: "ltr" },
  { code: "he", name: "Hebrew",             flag: "🇮🇱", dir: "rtl" },
  { code: "hi", name: "Hindi",              flag: "🇮🇳", dir: "ltr" },
  { code: "hu", name: "Hungarian",          flag: "🇭🇺", dir: "ltr" },
  { code: "is", name: "Icelandic",          flag: "🇮🇸", dir: "ltr" },
  { code: "ig", name: "Igbo",               flag: "🇳🇬", dir: "ltr" },
  { code: "id", name: "Indonesian",         flag: "🇮🇩", dir: "ltr" },
  { code: "ga", name: "Irish",              flag: "🇮🇪", dir: "ltr" },
  { code: "it", name: "Italian",            flag: "🇮🇹", dir: "ltr" },
  { code: "ja", name: "Japanese",           flag: "🇯🇵", dir: "ltr" },
  { code: "kn", name: "Kannada",            flag: "🇮🇳", dir: "ltr" },
  { code: "kk", name: "Kazakh",             flag: "🇰🇿", dir: "ltr" },
  { code: "km", name: "Khmer",              flag: "🇰🇭", dir: "ltr" },
  { code: "ko", name: "Korean",             flag: "🇰🇷", dir: "ltr" },
  { code: "ku", name: "Kurdish",            flag: "🏴", dir: "rtl" },
  { code: "ky", name: "Kyrgyz",             flag: "🇰🇬", dir: "ltr" },
  { code: "lo", name: "Lao",                flag: "🇱🇦", dir: "ltr" },
  { code: "lv", name: "Latvian",            flag: "🇱🇻", dir: "ltr" },
  { code: "lt", name: "Lithuanian",         flag: "🇱🇹", dir: "ltr" },
  { code: "lb", name: "Luxembourgish",      flag: "🇱🇺", dir: "ltr" },
  { code: "mk", name: "Macedonian",         flag: "🇲🇰", dir: "ltr" },
  { code: "mg", name: "Malagasy",           flag: "🇲🇬", dir: "ltr" },
  { code: "ms", name: "Malay",              flag: "🇲🇾", dir: "ltr" },
  { code: "ml", name: "Malayalam",          flag: "🇮🇳", dir: "ltr" },
  { code: "mt", name: "Maltese",            flag: "🇲🇹", dir: "ltr" },
  { code: "mi", name: "Maori",              flag: "🇳🇿", dir: "ltr" },
  { code: "mr", name: "Marathi",            flag: "🇮🇳", dir: "ltr" },
  { code: "mn", name: "Mongolian",          flag: "🇲🇳", dir: "ltr" },
  { code: "my", name: "Myanmar (Burmese)",  flag: "🇲🇲", dir: "ltr" },
  { code: "ne", name: "Nepali",             flag: "🇳🇵", dir: "ltr" },
  { code: "no", name: "Norwegian",          flag: "🇳🇴", dir: "ltr" },
  { code: "ps", name: "Pashto",             flag: "🇦🇫", dir: "rtl" },
  { code: "fa", name: "Persian",            flag: "🇮🇷", dir: "rtl" },
  { code: "pl", name: "Polish",             flag: "🇵🇱", dir: "ltr" },
  { code: "pt", name: "Portuguese",         flag: "🇵🇹", dir: "ltr" },
  { code: "pa", name: "Punjabi",            flag: "🇮🇳", dir: "ltr" },
  { code: "ro", name: "Romanian",           flag: "🇷🇴", dir: "ltr" },
  { code: "ru", name: "Russian",            flag: "🇷🇺", dir: "ltr" },
  { code: "sm", name: "Samoan",             flag: "🇼🇸", dir: "ltr" },
  { code: "sr", name: "Serbian",            flag: "🇷🇸", dir: "ltr" },
  { code: "si", name: "Sinhala",            flag: "🇱🇰", dir: "ltr" },
  { code: "sk", name: "Slovak",             flag: "🇸🇰", dir: "ltr" },
  { code: "sl", name: "Slovenian",          flag: "🇸🇮", dir: "ltr" },
  { code: "so", name: "Somali",             flag: "🇸🇴", dir: "ltr" },
  { code: "es", name: "Spanish",            flag: "🇪🇸", dir: "ltr" },
  { code: "su", name: "Sundanese",          flag: "🇮🇩", dir: "ltr" },
  { code: "sw", name: "Swahili",            flag: "🇰🇪", dir: "ltr" },
  { code: "sv", name: "Swedish",            flag: "🇸🇪", dir: "ltr" },
  { code: "tl", name: "Tagalog (Filipino)", flag: "🇵🇭", dir: "ltr" },
  { code: "tg", name: "Tajik",              flag: "🇹🇯", dir: "ltr" },
  { code: "ta", name: "Tamil",              flag: "🇮🇳", dir: "ltr" },
  { code: "tt", name: "Tatar",              flag: "🇷🇺", dir: "ltr" },
  { code: "te", name: "Telugu",             flag: "🇮🇳", dir: "ltr" },
  { code: "th", name: "Thai",               flag: "🇹🇭", dir: "ltr" },
  { code: "tr", name: "Turkish",            flag: "🇹🇷", dir: "ltr" },
  { code: "tk", name: "Turkmen",            flag: "🇹🇲", dir: "ltr" },
  { code: "uk", name: "Ukrainian",          flag: "🇺🇦", dir: "ltr" },
  { code: "ur", name: "Urdu",               flag: "🇵🇰", dir: "rtl" },
  { code: "ug", name: "Uyghur",             flag: "🇨🇳", dir: "rtl" },
  { code: "uz", name: "Uzbek",              flag: "🇺🇿", dir: "ltr" },
  { code: "vi", name: "Vietnamese",         flag: "🇻🇳", dir: "ltr" },
  { code: "cy", name: "Welsh",              flag: "🏴󠁧󠁢󠁷󠁬󠁳󠁿", dir: "ltr" },
  { code: "xh", name: "Xhosa",             flag: "🇿🇦", dir: "ltr" },
  { code: "yi", name: "Yiddish",            flag: "🇮🇱", dir: "rtl" },
  { code: "yo", name: "Yoruba",             flag: "🇳🇬", dir: "ltr" },
  { code: "zu", name: "Zulu",               flag: "🇿🇦", dir: "ltr" },
];

// ── ENGLISH BASE TRANSLATIONS (source of truth) ─────────────────────────────
// Every translatable key in the app. Dynamic translation translates these values.
// H6: the ~1.9k-line English dictionary now lives in ./enStrings and is
// dynamically imported by LanguageProvider so it stays OUT of the entry chunk.
// This module-level holder is populated before any consumer reads it (the
// provider gates render until the import resolves), so getTranslatedStrings
// and the provider body below keep referencing EN_STRINGS unchanged.
let EN_STRINGS = {};

// English variants (US/UK) share the base English strings — no translation fetch.
const isEnglishBase = (code) => code === "en" || code === "en-GB";

// ── DYNAMIC TRANSLATION VIA GOOGLE TRANSLATE (free) ─────────────────────────
const TRANSLATE_CACHE_KEY = "zt_translate_cache";

function loadTranslateCache() {
  try {
    return JSON.parse(localStorage.getItem(TRANSLATE_CACHE_KEY) || "{}");
  } catch { return {}; }
}

function saveTranslateCache(cache) {
  try {
    // Keep cache size reasonable (max 50 languages)
    const keys = Object.keys(cache);
    if (keys.length > 50) {
      const keep = keys.slice(-40);
      const trimmed = {};
      keep.forEach(k => { trimmed[k] = cache[k]; });
      localStorage.setItem(TRANSLATE_CACHE_KEY, JSON.stringify(trimmed));
    } else {
      localStorage.setItem(TRANSLATE_CACHE_KEY, JSON.stringify(cache));
    }
  } catch {}
}

async function translateBatch(texts, targetLang) {
  // Use Google Translate free endpoint (unofficial but widely used)
  // Translate in chunks of 20 to avoid URL length limits
  const results = [];
  const chunkSize = 20;
  for (let i = 0; i < texts.length; i += chunkSize) {
    const chunk = texts.slice(i, i + chunkSize);
    const query = chunk.map(t => `q=${encodeURIComponent(t)}`).join("&");
    try {
      const url = `https://translate.googleapis.com/translate_a/t?client=gtx&sl=en&tl=${targetLang}&${query}`;
      const resp = await fetch(url);
      if (!resp.ok) throw new Error("Translation API failed");
      const data = await resp.json();
      // Response is array of [translatedText, ...] or array of arrays
      if (Array.isArray(data)) {
        data.forEach(item => {
          if (Array.isArray(item)) results.push(item[0] || chunk[results.length - i] || "");
          else if (typeof item === "string") results.push(item);
          else results.push(chunk[results.length - i] || "");
        });
      }
    } catch {
      // On failure, push original English text
      chunk.forEach(t => results.push(t));
    }
  }
  return results;
}

async function getTranslatedStrings(langCode) {
  if (isEnglishBase(langCode)) return EN_STRINGS;

  const keys = Object.keys(EN_STRINGS);
  const cache = loadTranslateCache();
  const cached = cache[langCode];

  // Reuse cache only when it covers every current key (prevents stale cache
  // from masking keys added after the user's last translation fetch).
  if (cached && keys.every(k => k in cached)) return cached;

  const values = keys.map(k => EN_STRINGS[k]);

  try {
    const translated = await translateBatch(values, langCode);
    const result = {};
    keys.forEach((k, i) => { result[k] = translated[i] || EN_STRINGS[k]; });

    cache[langCode] = result;
    saveTranslateCache(cache);

    return result;
  } catch {
    // Fall back to partial cache if we have one, otherwise English.
    return cached || EN_STRINGS;
  }
}

// ── LANGUAGE CONTEXT ─────────────────────────────────────────────────────────
const LanguageContext = createContext(null);

// Minimal boot fallback, shown ONLY on the very first load while the English
// dictionary chunk (./enStrings) downloads. Subsequent mounts are instant —
// the module holder is already populated. Mirrors the app's lazy-route spinner
// so there is no flash of un-stringified content.
function LangBootFallback() {
  return (
    <div style={{ position: "fixed", inset: 0, display: "flex", alignItems: "center", justifyContent: "center", background: "#fff" }}>
      <div style={{ width: 36, height: 36, borderRadius: "50%", border: "3px solid #E7F0F6", borderTopColor: "#11496A", animation: "zt-spin 0.8s linear infinite" }} />
      <style>{`@keyframes zt-spin{to{transform:rotate(360deg)}}`}</style>
    </div>
  );
}

// H6: lazy-load the English dictionary out of the entry chunk. We gate the inner
// provider until the import resolves so every consumer (and the module-level
// getTranslatedStrings) sees a populated EN_STRINGS — no flash of missing
// strings, identical behaviour once loaded. A failed import still releases the
// gate (the .finally) rather than hanging, and language switching is unaffected.
export function LanguageProvider({ children }) {
  const [ready, setReady] = useState(() => Object.keys(EN_STRINGS).length > 0);
  useEffect(() => {
    if (ready) return;
    let alive = true;
    import("./enStrings")
      .then((m) => { EN_STRINGS = m.EN_STRINGS; })
      .catch(() => { /* boot with whatever loaded; guarded T.x || "…" fallbacks apply */ })
      .finally(() => { if (alive) setReady(true); });
    return () => { alive = false; };
  }, [ready]);
  if (!ready) return <LangBootFallback />;
  return <LanguageProviderInner>{children}</LanguageProviderInner>;
}

function LanguageProviderInner({ children }) {
  const [lang, setLangState] = useState(() => localStorage.getItem("zt_lang") || "en");
  const [T, setT] = useState(EN_STRINGS);
  const [translating, setTranslating] = useState(false);
  const [showLangPopup, setShowLangPopup] = useState(false);
  const [detectedLang, setDetectedLang] = useState(null);
  const [detectedLangName, setDetectedLangName] = useState("");
  const detectionDone = useRef(false);

  // Load translations when language changes
  const loadTranslations = useCallback(async (code) => {
    if (isEnglishBase(code)) { setT(EN_STRINGS); return; }
    setTranslating(true);
    try {
      const strings = await getTranslatedStrings(code);
      setT(strings);
    } catch {
      setT(EN_STRINGS);
    }
    setTranslating(false);
  }, []);

  // Set language: persist + load translations + update direction
  const setLang = useCallback(async (code) => {
    localStorage.setItem("zt_lang", code);
    localStorage.setItem("zt_lang_chosen", "true"); // Mark that user has chosen
    setLangState(code);
    const langDef = LANGUAGES.find(l => l.code === code);
    document.documentElement.dir = langDef?.dir || "ltr";
    await loadTranslations(code);
  }, [loadTranslations]);

  // On mount: apply saved language, then detect if needed
  useEffect(() => {
    const saved = localStorage.getItem("zt_lang") || "en";
    const langDef = LANGUAGES.find(l => l.code === saved);
    document.documentElement.dir = langDef?.dir || "ltr";
    if (!isEnglishBase(saved)) {
      loadTranslations(saved);
    }
  }, [loadTranslations]);

  // IP-based detection (only on first ever visit)
  useEffect(() => {
    if (detectionDone.current) return;
    detectionDone.current = true;

    const hasChosen = localStorage.getItem("zt_lang_chosen");
    if (hasChosen) return; // User already picked a language before

    // Try browser language first
    const browserLang = (navigator.language || "en").split("-")[0];
    const browserMatch = LANGUAGES.find(l => l.code === browserLang);

    // Call backend IP detection
    const detect = async () => {
      try {
        const res = await fetch(`${API_BASE}/detect-language`);
        if (!res.ok) throw new Error();
        const data = await res.json();
        const ipLang = data.language || "en";

        // Determine final suggestion: prefer IP result, fall back to browser
        const suggestion = ipLang !== "en" ? ipLang : (browserLang !== "en" && browserMatch ? browserLang : null);

        if (suggestion && suggestion !== "en") {
          const langInfo = LANGUAGES.find(l => l.code === suggestion);
          setDetectedLang(suggestion);
          setDetectedLangName(langInfo?.name || suggestion);
          setShowLangPopup(true);
        } else {
          // No suggestion needed, mark as chosen
          localStorage.setItem("zt_lang_chosen", "true");
        }
      } catch {
        // API failed - try browser language only
        if (browserLang !== "en" && browserMatch) {
          setDetectedLang(browserLang);
          setDetectedLangName(browserMatch.name);
          setShowLangPopup(true);
        } else {
          localStorage.setItem("zt_lang_chosen", "true");
        }
      }
    };
    detect();
  }, []);

  // Accept detected language
  const acceptDetectedLang = useCallback(() => {
    if (detectedLang) {
      setLang(detectedLang);
    }
    setShowLangPopup(false);
  }, [detectedLang, setLang]);

  // Reject detected language (stay in English)
  const rejectDetectedLang = useCallback(() => {
    localStorage.setItem("zt_lang_chosen", "true");
    setShowLangPopup(false);
  }, []);

  // Backward compat: TRANSLATIONS object for old code.
  // Memoized so the inner reference stays stable until `lang` or `T` change
  // — this is what every `useLanguage()` consumer sees in its dependency
  // arrays, so stale-identity churn here would invalidate downstream
  // useEffects / useMemos all over the app.
  const TRANSLATIONS = useMemo(() => {
    const m = { en: EN_STRINGS };
    m[lang] = T;
    return m;
  }, [lang, T]);

  // Memoize the provider value so consumers of useLanguage() only re-render
  // when the language layer actually changes. Previously the inline
  // `value={{ ... }}` allocated a new object every render of the provider,
  // forcing every page that calls useLanguage() (most of the app) to
  // re-render on any parent re-render — a silent fan-out hot spot.
  const value = useMemo(() => ({
    lang, setLang, T, LANGUAGES, TRANSLATIONS, EN_STRINGS,
    translating, showLangPopup, detectedLang, detectedLangName,
    acceptDetectedLang, rejectDetectedLang,
  }), [
    lang, setLang, T, TRANSLATIONS,
    translating, showLangPopup, detectedLang, detectedLangName,
    acceptDetectedLang, rejectDetectedLang,
  ]);

  return (
    <LanguageContext.Provider value={value}>
      {children}
      {/* ── First-time translation overlay ──────────────────────────────────
          A non-English switch machine-translates the whole dictionary over
          dozens of serial Google Translate calls (seconds on the first pick;
          instant afterwards from the localStorage cache). Without feedback the
          page just sits in the previous language / half-translated, which read
          as a bug. Surface the existing `translating` flag as a blocking
          overlay so the switch shows clear progress. Cached switches flip the
          flag within a frame, so this barely renders on repeat picks. */}
      {translating && (
        <div style={{
          position: "fixed", inset: 0, zIndex: 99998,
          background: "rgba(16,24,40,0.45)", backdropFilter: "blur(3px)",
          display: "flex", flexDirection: "column", alignItems: "center",
          justifyContent: "center", gap: 14,
        }}>
          <div style={{ width: 40, height: 40, borderRadius: "50%", border: "3px solid rgba(255,255,255,0.35)", borderTopColor: "#fff", animation: "zt-spin 0.8s linear infinite" }} />
          <p style={{ color: "#fff", fontWeight: 600, fontSize: 14 }}>{T.translatingLabel || "Translating…"}</p>
          <style>{`@keyframes zt-spin{to{transform:rotate(360deg)}}`}</style>
        </div>
      )}
      {/* ── Language Detection Popup ── */}
      {showLangPopup && (
        <div style={{
          position: "fixed", inset: 0, zIndex: 99999,
          background: "rgba(16,24,40,0.5)", backdropFilter: "blur(4px)",
          display: "flex", alignItems: "center", justifyContent: "center",
          animation: "zt_fadeIn .25s ease",
        }}>
          <div style={{
            background: "#fff", borderRadius: 16, padding: "32px 36px",
            maxWidth: 420, width: "90%", textAlign: "center",
            boxShadow: "0 24px 80px rgba(0,0,0,0.25)",
            animation: "zt_slideUp .3s ease",
          }}>
            <div style={{
              width: 56, height: 56, borderRadius: "50%",
              background: "#E7F0F6", display: "flex", alignItems: "center",
              justifyContent: "center", margin: "0 auto 16px",
              fontSize: 28,
            }}>
              {LANGUAGES.find(l => l.code === detectedLang)?.flag || "🌐"}
            </div>
            <p style={{ fontSize: 18, fontWeight: 700, color: "#101828", marginBottom: 6 }}>
              {EN_STRINGS.langDetected}
            </p>
            <p style={{ fontSize: 22, fontWeight: 800, color: "#11496A", marginBottom: 8 }}>
              {detectedLangName}
            </p>
            <p style={{ fontSize: 14, color: "#475467", marginBottom: 24 }}>
              {EN_STRINGS.langSwitch}
            </p>
            <div style={{ display: "flex", gap: 12, justifyContent: "center" }}>
              <button onClick={rejectDetectedLang} style={{
                padding: "10px 28px", borderRadius: 8,
                border: "1px solid #D0D5DD", background: "#fff",
                color: "#344054", fontWeight: 600, fontSize: 14,
                cursor: "pointer", fontFamily: "inherit",
              }}>
                {EN_STRINGS.langNo}
              </button>
              <button onClick={acceptDetectedLang} style={{
                padding: "10px 28px", borderRadius: 8,
                border: "none", background: "linear-gradient(135deg,#11496A 0%,#44B97B 100%)",
                color: "#fff", fontWeight: 700, fontSize: 14,
                cursor: "pointer", fontFamily: "inherit",
                boxShadow: "0 4px 14px rgba(17,73,106,0.35)",
              }}>
                {EN_STRINGS.langYes}
              </button>
            </div>
          </div>
        </div>
      )}
      {/* React 19 reconciles `<style>{templateString}</style>` children differently
          than React 18 — under some load orders the CSS text flashes out as visible
          body content before the element's default `display: none` applies (this
          was the `@keyframes zt_fadeIn { … }` line visibly leaking at the bottom
          of every screen inside the Electron shell). Using
          `dangerouslySetInnerHTML` writes the CSS straight into the element's
          innerHTML without going through React's children pipeline, which avoids
          the flash entirely. Functionally identical output. */}
      <style dangerouslySetInnerHTML={{ __html: `
        @keyframes zt_fadeIn { from { opacity: 0; } to { opacity: 1; } }
        @keyframes zt_slideUp { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: translateY(0); } }
      ` }} />
    </LanguageContext.Provider>
  );
}

export function useLanguage() {
  const ctx = useContext(LanguageContext);
  if (!ctx) throw new Error("useLanguage must be used within LanguageProvider");
  return ctx;
}

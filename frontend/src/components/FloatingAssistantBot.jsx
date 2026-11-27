import { useState, useEffect, lazy, Suspense } from "react";
// PERF: the KAIROS chat tree (~47 kB) used to be a static import, so it loaded
// on every dashboard even though the panel only opens on click. Lazy-load it so
// the chunk is fetched the first time the assistant is actually opened.
const ChatBotRoot = lazy(() => import("../KAIROS/ChatBotRoot"));

// Brand mark — navy rounded square with a thin white inner border, four
// cardinal clock ticks, and a stylized emerald "Z" with two white highlight
// slashes. Matches the official Temveris logo.
const KairosMark = ({ size = 60 }) => (
  <svg
    width={size}
    height={size}
    viewBox="0 0 64 64"
    xmlns="http://www.w3.org/2000/svg"
    aria-hidden="true"
  >
    {/* Navy rounded-square plate */}
    <rect x="3" y="3" width="58" height="58" rx="12" fill="#11496A" />
    {/* Thin white inner border */}
    <rect
      x="7" y="7" width="50" height="50" rx="9"
      fill="none" stroke="#FFFFFF" strokeWidth="1.6"
    />
    {/* Four cardinal tick marks (12 / 3 / 6 / 9) */}
    <g stroke="#FFFFFF" strokeWidth="2.2" strokeLinecap="round">
      <line x1="32" y1="11" x2="32" y2="15" />
      <line x1="53" y1="32" x2="49" y2="32" />
      <line x1="32" y1="53" x2="32" y2="49" />
      <line x1="11" y1="32" x2="15" y2="32" />
    </g>
    {/* Emerald stylized "Z" */}
    <path
      d="M20 20 L44 20 L44 26 L30 38 L44 38 L44 44 L20 44 L20 38 L34 26 L20 26 Z"
      fill="#44B97B"
    />
    {/* White diagonal highlight slashes — top-left and bottom-right of the Z */}
    <g stroke="#FFFFFF" strokeWidth="1.6" strokeLinecap="round" opacity="0.95">
      <line x1="22.5" y1="22" x2="26" y2="25.5" />
      <line x1="38" y1="38.5" x2="41.5" y2="42" />
    </g>
  </svg>
);

const CloseIcon = () => (
  <svg
    width="20"
    height="20"
    viewBox="0 0 20 20"
    fill="none"
    aria-hidden="true"
  >
    <path
      d="M5 5L15 15M15 5L5 15"
      stroke="#fff"
      strokeWidth="2.2"
      strokeLinecap="round"
    />
  </svg>
);

export default function FloatingAssistantBot({ right = 24, bottom = 24 } = {}) {
  const [isOpen, setIsOpen] = useState(false);

  // Close on Escape key
  useEffect(() => {
    if (!isOpen) return;
    const handleKey = (e) => {
      if (e.key === "Escape") setIsOpen(false);
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [isOpen]);

  return (
    <>
      <style>{`
        /* One subtle pulse — a single emerald ring breathing once every 2.4s.
           Just enough motion to signal "live"; the rounded-square FAB casts
           a soft navy drop shadow with an emerald halo on the breath crest. */
        @keyframes ztFabPulse {
          0%   { box-shadow: 0 6px 18px rgba(17,73,106,0.30),
                              0 0 0 0 rgba(68,185,123,0.45); }
          70%  { box-shadow: 0 10px 24px rgba(17,73,106,0.34),
                              0 0 0 8px rgba(68,185,123,0); }
          100% { box-shadow: 0 6px 18px rgba(17,73,106,0.30),
                              0 0 0 0 rgba(68,185,123,0); }
        }
        @keyframes ztFadeIn {
          from { opacity: 0; }
          to   { opacity: 1; }
        }
        @keyframes ztPopUp {
          from { opacity: 0; transform: scale(0.96) translateY(10px); }
          to   { opacity: 1; transform: scale(1)    translateY(0); }
        }

        .zt-fab {
          position: fixed;
          animation: ztFabPulse 2.4s ease-out infinite;
          transition: transform .18s ease, box-shadow .18s ease;
        }
        .zt-fab:hover  { transform: translateY(-2px); }
        .zt-fab:active { transform: translateY(0) scale(.97); }
        .zt-fab-open {
          animation: none !important;
          box-shadow: 0 8px 22px rgba(17,73,106,0.35) !important;
        }

        .zt-backdrop   { animation: ztFadeIn .18s ease-out; }
        .zt-chat-panel { animation: ztPopUp  .22s cubic-bezier(.22,1,.36,1); }

        @media (prefers-reduced-motion: reduce) {
          .zt-fab { animation: none !important; }
        }
      `}</style>

      {/* Backdrop — click outside to close */}
      {isOpen && (
        <div
          className="zt-backdrop"
          onClick={() => setIsOpen(false)}
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(15, 23, 42, 0.42)",
            backdropFilter: "blur(2px)",
            WebkitBackdropFilter: "blur(2px)",
            zIndex: 8999,
          }}
        />
      )}

      {/* Floating chat panel — single soft shadow, no nested rings. */}
      {isOpen && (
        <div
          className="zt-chat-panel"
          style={{
            position: "fixed",
            bottom: bottom + 72,
            right,
            width: "min(420px, calc(100vw - 32px))",
            maxHeight: `calc(100vh - ${bottom + 96}px)`,
            height: `calc(100vh - ${bottom + 96}px)`,
            borderRadius: 16,
            boxShadow: "0 18px 48px rgba(15, 23, 42, 0.28)",
            border: "1px solid rgba(15, 23, 42, 0.08)",
            background: "#fff",
            zIndex: 9001,
            overflow: "hidden",
            display: "flex",
            flexDirection: "column",
          }}
        >
          <Suspense fallback={
            <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: "#64748B", fontSize: 13 }}>
              Loading assistant…
            </div>
          }>
            <ChatBotRoot />
          </Suspense>
        </div>
      )}

      {/* FAB — rounded-square plate that IS the brand mark. Logo fills the
          button edge-to-edge; no extra background needed because the SVG
          already draws the navy plate and white border. */}
      <button
        type="button"
        onClick={() => setIsOpen((prev) => !prev)}
        aria-label={isOpen ? "Close Temveris Assistant" : "Open Temveris Assistant"}
        title="Temveris Assistant"
        className={`zt-fab${isOpen ? " zt-fab-open" : ""}`}
        style={{
          bottom,
          right,
          width: 56,
          height: 56,
          borderRadius: 14,
          border: "none",
          cursor: "pointer",
          background: isOpen ? "#11496A" : "transparent",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          padding: 0,
          zIndex: 8000,
        }}
      >
        {isOpen ? <CloseIcon /> : <KairosMark size={56} />}
      </button>
    </>
  );
}

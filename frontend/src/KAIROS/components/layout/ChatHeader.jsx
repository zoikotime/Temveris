import { useEffect, useRef, useState } from "react";
import {
  HiOutlineClock,
  HiOutlineMoon,
  HiOutlineSun,
} from "react-icons/hi2";
import {
  HiOutlineMail,
  HiOutlineChatAlt2,
} from "react-icons/hi";
import { TiThMenu } from "react-icons/ti";

import LangPanel from "./LangPanel";
import HistoryPanel from "../panels/HistoryPanel";

export default function ChatHeader({
  theme,
  onToggleTheme,
  lang,
  onLangChange,
  openPanel,
  onTogglePanel,
  onClosePanel,
  sessions,
  onSelectSession,
  onNewChat,
  onMailClick,
}) {
  const isDark = theme === "dark";
  const panelRef = useRef(null);
  const [actionOpen, setActionOpen] = useState(false);

  useEffect(() => {
    function handleOutsideClick(event) {
      if (!panelRef.current) return;
      if (!panelRef.current.contains(event.target)) {
        onClosePanel();
        setActionOpen(false);
      }
    }
    setTimeout(() => {
      document.addEventListener("click", handleOutsideClick);
    }, 0);
    return () => {
      document.removeEventListener("click", handleOutsideClick);
    };
  }, [onClosePanel]);

  // Aligned with Temveris sidebar buttons: navy-tinted surface, emerald
  // accent on hover (matches `.ztsb-btn` palette in AdminSidebar).
  const iconButton = `flex h-9 min-w-9 items-center justify-center rounded-[10px] border transition-all duration-200 ${
    isDark
      ? "border-[#1F3D55] bg-[#0E2B3D] text-[#A6C7DC] hover:border-[#5FCC93] hover:bg-[#0D2A1A] hover:text-[#5FCC93]"
      : "border-[#A6C7DC] bg-[#E7F0F6] text-[#11496A] hover:border-[#44B97B] hover:bg-[#E8F7EE] hover:text-[#2F8F5C]"
  }`;

  const wideButton = `flex h-9 items-center gap-1.5 rounded-[10px] border px-2.5 text-[0.65rem] font-bold transition-all duration-200 ${
    isDark
      ? "border-[#1F3D55] bg-[#0E2B3D] text-[#A6C7DC] hover:border-[#5FCC93] hover:bg-[#0D2A1A] hover:text-[#5FCC93]"
      : "border-[#A6C7DC] bg-[#E7F0F6] text-[#11496A] hover:border-[#44B97B] hover:bg-[#E8F7EE] hover:text-[#2F8F5C]"
  }`;

  return (
    <header
      className={`relative flex items-center gap-3 border-b px-1 py-1 sm:px-2 sm:py-2 ${
        isDark
          ? "border-[#2D3748] bg-[#0F1117]"
          : "border-[#E4E7EC] bg-[#F8FAFC]"
      }`}
    >
      <style>{`
        @keyframes ztKairosShimmer {
          0%   { background-position: 0% 50%; }
          100% { background-position: 200% 50%; }
        }
        @keyframes ztKairosDotPulse {
          0%, 100% { box-shadow: 0 0 0 0 rgba(68,185,123,0.55); }
          50%      { box-shadow: 0 0 0 5px rgba(68,185,123,0); }
        }
        @keyframes ztBadgeSheen {
          0%   { transform: translateX(-120%); }
          60%, 100% { transform: translateX(220%); }
        }
        .zt-kairos-title {
          font-family: 'Sora', 'DM Sans', 'Inter', system-ui, sans-serif;
          font-weight: 800;
          font-feature-settings: "ss01", "cv11";
          /* Temveris brand gradient: navy → emerald */
          background: linear-gradient(90deg, #11496A 0%, #2F8F5C 50%, #44B97B 100%, #11496A 150%);
          background-size: 200% 100%;
          -webkit-background-clip: text;
          background-clip: text;
          color: transparent;
          animation: ztKairosShimmer 6s linear infinite;
          letter-spacing: -0.025em;
        }
        .zt-kairos-badge-text {
          font-family: 'Space Grotesk', 'DM Sans', 'Inter', system-ui, sans-serif;
          font-weight: 700;
        }
        .zt-kairos-title-light {
          filter: drop-shadow(0 1px 0 rgba(17,73,106,0.10));
        }
        .zt-kairos-title-dark {
          filter: drop-shadow(0 1px 6px rgba(68,185,123,0.28));
        }
        .zt-kairos-logo-ring::before {
          content: "";
          position: absolute;
          inset: -2px;
          border-radius: 14px;
          padding: 2px;
          background: conic-gradient(from 180deg at 50% 50%, #11496A, #44B97B, #3D8AB5, #11496A);
          -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
          -webkit-mask-composite: xor;
                  mask-composite: exclude;
          opacity: 0.9;
          pointer-events: none;
        }
        .zt-kairos-dot {
          animation: ztKairosDotPulse 2.2s ease-out infinite;
        }
        .zt-kairos-badge {
          position: relative;
          overflow: hidden;
          isolation: isolate;
        }
        .zt-kairos-badge::after {
          content: "";
          position: absolute;
          inset: 0;
          background: linear-gradient(110deg, transparent 30%, rgba(255,255,255,0.35) 50%, transparent 70%);
          transform: translateX(-120%);
          animation: ztBadgeSheen 4.5s ease-in-out infinite;
          pointer-events: none;
        }
      `}</style>

      {/* ── Logo + Title + Badge ── */}
      <div className="flex flex-1 items-center gap-3 min-w-0">
        {/* Logo with conic-gradient ring + pulsing online dot */}
        <div className="zt-kairos-logo-ring relative w-11 h-11 rounded-[10px] flex-shrink-0 overflow-hidden">
          <img
            src="./favicon.svg"
            alt="Kairos logo"
            style={{
              width: "100%",
              height: "100%",
              objectFit: "cover",
              display: "block",
              borderRadius: "10px",
            }}
          />
          <span
            className={`zt-kairos-dot absolute bottom-1 right-1 h-2 w-2 rounded-full bg-[#44B97B] ring-2 z-10 ${
              isDark ? "ring-[#0F1117]" : "ring-white"
            }`}
            aria-hidden="true"
          ></span>
        </div>

        {/* Name + badge */}
        <div className="flex min-w-0 flex-col gap-[6px]">
          <span
            className={`zt-kairos-title truncate font-extrabold text-[1.5rem] leading-none ${
              isDark ? "zt-kairos-title-dark" : "zt-kairos-title-light"
            }`}
          >
            Kairos
          </span>
          <span
            className={`zt-kairos-badge zt-kairos-badge-text inline-flex w-fit items-center gap-1 rounded-full px-2.5 py-[3px] text-[0.6rem] uppercase tracking-[0.18em] leading-none shadow-sm ${
              isDark
                ? "bg-gradient-to-r from-[rgba(17,73,106,0.45)] via-[rgba(47,143,92,0.35)] to-[rgba(68,185,123,0.35)] text-[#A6C7DC] ring-1 ring-[#1F3D55]"
                : "bg-gradient-to-r from-[#E7F0F6] via-[#E8F7EE] to-[#E7F0F6] text-[#11496A] ring-1 ring-[#A6C7DC]"
            }`}
          >
            <svg
              viewBox="0 0 24 24"
              fill="currentColor"
              aria-hidden="true"
              className="h-[10px] w-[10px] drop-shadow-[0_0_4px_currentColor]"
            >
              <path d="M13 2 4 14h6l-1 8 9-12h-6l1-8z" />
            </svg>
            Temveris Assistant
          </span>
        </div>
      </div>

      {/* ── Right actions ── */}
      <div
        className="relative flex items-center gap-1.5 shrink-0"
        ref={panelRef}
      >
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onTogglePanel("history");
          }}
          className={iconButton}
        >
          <HiOutlineClock className="h-[17px] w-[17px]" />
        </button>

        <button type="button" onClick={onToggleTheme} className={iconButton}>
          {isDark ? (
            <HiOutlineSun className="h-[17px] w-[17px]" />
          ) : (
            <HiOutlineMoon className="h-[17px] w-[17px]" />
          )}
        </button>

        {/* <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onTogglePanel("lang");
          }}
          className={`${wideButton} hidden sm:flex`}
        >
          <HiOutlineGlobeAlt className="h-[15px] w-[15px]" />
          {currentLanguage.short}
        </button> */}

        <div className="relative">
          <button
            type="button"
            onClick={() => setActionOpen((p) => !p)}
            className={wideButton}
          >
            <TiThMenu className="h-[15px] w-[15px]" />
            <span className="hidden sm:inline">MENU</span>
          </button>

          {actionOpen && (
            <div
              className={`absolute right-0 mt-2 w-44 rounded-xl border shadow-xl z-50 overflow-hidden ${
                isDark
                  ? "border-[#2D3748] bg-[#1A1F2E]"
                  : "border-[#E4E7EC] bg-white"
              }`}
            >
              <button
                type="button"
                onClick={() => {
                  setActionOpen(false);
                  onNewChat();
                }}
                className={`flex items-center gap-2 w-full px-4 py-2.5 text-sm transition-all ${
                  isDark
                    ? "text-[#A0ADB9] hover:bg-[#0D2A1A] hover:text-[#5FCC93]"
                    : "text-[#475467] hover:bg-[#E8F7EE] hover:text-[#2F8F5C]"
                }`}
              >
                <HiOutlineChatAlt2 className="h-4 w-4 shrink-0" /> New
                Conversation
              </button>

              <button
                type="button"
                onClick={() => {
                  setActionOpen(false);
                  onMailClick();
                }}
                className={`flex items-center gap-2 w-full px-4 py-2.5 text-sm transition-all ${
                  isDark
                    ? "text-[#A0ADB9] hover:bg-[#0D2A1A] hover:text-[#5FCC93]"
                    : "text-[#475467] hover:bg-[#E8F7EE] hover:text-[#2F8F5C]"
                }`}
              >
                <HiOutlineMail className="h-4 w-4 shrink-0" /> Mail
              </button>
            </div>
          )}
        </div>

        {openPanel === "history" && (
          <HistoryPanel
            sessions={sessions}
            onClose={onClosePanel}
            onSelect={onSelectSession}
            onNewChat={onNewChat}
            theme={theme}
          />
        )}

        {openPanel === "lang" && (
          <LangPanel
            current={lang}
            onChange={onLangChange}
            onClose={onClosePanel}
            theme={theme}
          />
        )}
      </div>
    </header>
  );
}

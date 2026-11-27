import { useRef } from "react";
import { HiOutlinePaperAirplane, HiOutlineTrash } from "react-icons/hi2";

const FOOTER_TAGS = ["Kairos", "Temveris AI", "Source-grounded", "Governed"];

export default function Composer({
  input,
  setInput,
  isTyping,
  onSend,
  onClear,
  theme,
}) {
  const textareaRef = useRef(null);
  const isDark = theme === "dark";
  const canSend = Boolean(input.trim()) && !isTyping;

  const handleSubmit = (event) => {
    event.preventDefault();
    if (!canSend) return;
    onSend();
  };

  const handleKeyDown = (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      handleSubmit(event);
    }
  };

  return (
    <form
      onSubmit={handleSubmit}
      className={`flex-shrink-0 border-t px-3 pt-2.5 pb-2 ${
        isDark
          ? "border-[#2D3748] bg-[#1A1F2E]"
          : "border-[#E4E7EC] bg-white"
      }`}
    >
      <style>{`
        @keyframes ztComposerDot {
          0%, 100% { opacity: 0.55; transform: scale(1); }
          50%      { opacity: 1;    transform: scale(1.25); }
        }
        .zt-composer-shell {
          transition: border-color .18s ease, box-shadow .18s ease, background-color .18s ease;
        }
        .zt-composer-shell:focus-within {
          box-shadow: 0 0 0 3px rgba(17,73,106,0.18);
        }
        /* Temveris brand gradient: navy → emerald */
        .zt-send-btn {
          background: linear-gradient(135deg, #11496A 0%, #2F8F5C 55%, #44B97B 100%);
          color: #FFFFFF;
          transition: transform .15s ease, box-shadow .2s ease, opacity .2s ease, filter .2s ease;
        }
        .zt-send-btn:not(:disabled) {
          box-shadow: 0 4px 14px rgba(17,73,106,0.4), inset 0 1px 0 rgba(255,255,255,0.25);
        }
        .zt-send-btn:not(:disabled):hover {
          transform: translateY(-1px);
          filter: brightness(1.06);
        }
        .zt-send-btn:not(:disabled):active {
          transform: translateY(0) scale(0.96);
        }
        .zt-send-btn:disabled {
          filter: grayscale(0.4) brightness(0.85);
          opacity: 0.55;
          cursor: not-allowed;
          box-shadow: none;
        }
        .zt-ghost-btn {
          transition: background-color .15s ease, color .15s ease, transform .15s ease;
        }
        .zt-ghost-btn:hover { transform: translateY(-1px); }
        .zt-ghost-btn:active { transform: translateY(0) scale(0.96); }
        .zt-footer-dot {
          font-family: 'Space Grotesk','Inter',system-ui,sans-serif;
        }
        .zt-footer-pulse {
          animation: ztComposerDot 2.4s ease-in-out infinite;
        }
      `}</style>

      <div
        className={`zt-composer-shell flex items-end gap-2 rounded-2xl border px-3 py-2 ${
          isDark
            ? "border-[#2D3748] bg-[#0F172A] focus-within:border-[#3D8AB5]"
            : "border-[#E4E7EC] bg-[#F8FAFC] focus-within:border-[#11496A] shadow-sm"
        }`}
      >
        <textarea
          ref={textareaRef}
          value={input}
          onChange={(event) => {
            setInput(event.target.value);
            event.target.style.height = "auto";
            event.target.style.height = `${Math.min(
              event.target.scrollHeight,
              130,
            )}px`;
          }}
          onKeyDown={handleKeyDown}
          placeholder="Ask Kairos anything about Temveris…"
          rows={1}
          disabled={isTyping}
          className={`flex-1 resize-none self-center bg-transparent text-sm leading-relaxed outline-none disabled:opacity-50 ${
            isDark
              ? "text-[#F0F4F8] placeholder-[#6B7A8A]"
              : "text-[#101828] placeholder-[#98A2B3]"
          }`}
          style={{ maxHeight: "130px", minHeight: "20px" }}
        />

        <button
          type="button"
          onClick={onClear}
          title="Clear chat"
          aria-label="Clear chat"
          className={`zt-ghost-btn flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-xl ${
            isDark
              ? "text-[#A0ADB9] hover:text-[#F87171] hover:bg-[rgba(248,113,113,0.1)]"
              : "text-[#475467] hover:text-[#B42318] hover:bg-[#FEF3F2]"
          }`}
        >
          <HiOutlineTrash className="h-[18px] w-[18px]" />
        </button>

        <button
          type="submit"
          disabled={!canSend}
          title={canSend ? "Send (Enter)" : "Type a message to send"}
          aria-label="Send message"
          className="zt-send-btn flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-xl"
        >
          <HiOutlinePaperAirplane className="h-[18px] w-[18px] -rotate-0" />
        </button>
      </div>

      <div
        className={`zt-footer-dot mt-2 flex select-none items-center justify-center gap-2 text-[0.6rem] uppercase tracking-[0.18em] ${
          isDark ? "text-[#6B7A8A]" : "text-[#98A2B3]"
        }`}
      >
        <span
          className="zt-footer-pulse inline-block h-1.5 w-1.5 rounded-full bg-[#44B97B]"
          aria-hidden="true"
        />
        {FOOTER_TAGS.map((tag, i) => (
          <span key={tag} className="flex items-center gap-2">
            <span className="font-semibold">{tag}</span>
            {i < FOOTER_TAGS.length - 1 && (
              <span
                aria-hidden="true"
                className={isDark ? "text-[#2D3748]" : "text-[#E4E7EC]"}
              >
                ·
              </span>
            )}
          </span>
        ))}
      </div>
    </form>
  );
}

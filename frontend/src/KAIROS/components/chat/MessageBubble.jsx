import { useEffect, useRef, useState } from "react";
import TypingDots from "./TypingDots";

const animatedIds = new Set();

// ─── Module-level audio context singleton ────────────────────────────────────
let audioCtx = null;

function getAudioContext() {
  if (!audioCtx) {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  }
  return audioCtx;
}

// ─── Notify sound ─────────────────────────────────────────────────────────────
async function playDoneSound() {
  try {
    const ac = getAudioContext();

    if (ac.state !== "running") {
      await ac.resume();
    }

    const t = ac.currentTime;

    [
      [880, 0],
      [660, 0.15],
    ].forEach(([freq, offset]) => {
      const osc = ac.createOscillator();
      const gain = ac.createGain();

      osc.connect(gain);
      gain.connect(ac.destination);

      osc.type = "sine";

      osc.frequency.setValueAtTime(freq, t + offset);

      gain.gain.setValueAtTime(0, t + offset);
      gain.gain.linearRampToValueAtTime(0.05, t + offset + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.001, t + offset + 0.2);

      osc.start(t + offset);
      osc.stop(t + offset + 0.2);

      osc.onended = () => {
        osc.disconnect();
        gain.disconnect();
      };
    });
  } catch {
    // audio is optional
  }
}

export default function MessageBubble({
  msg,
  onSuggestion,
  theme,
  isNew,
  bottomRef,
}) {
  const isUser = msg.role === "user";
  const isDark = theme === "dark";
  const body = msg.text ?? msg.content ?? "";
  const isStringBody = typeof body === "string";

  const shouldAnimate =
    !isUser && isStringBody && !msg.typing && isNew && !animatedIds.has(msg.id);

  const [displayed, setDisplayed] = useState(
    shouldAnimate ? "" : isStringBody ? body : "",
  );
  const [animating, setAnimating] = useState(shouldAnimate);
  const rafRef = useRef(null);

  useEffect(() => {
    if (!shouldAnimate) return;

    animatedIds.add(msg.id);

    const words = body.split(" ");
    const totalWords = words.length;
    // Snappier reveal: 18 ms/word, hard cap 900 ms. Long pricing/onboarding
    // answers previously took up to 3 s to finish — that read as the bot
    // being slow even when the API replied in <50 ms.
    const DURATION_MS = Math.min(900, Math.max(160, totalWords * 18));
    const intervalMs = DURATION_MS / totalWords;

    let wordIndex = 0;
    let lastTime = null;
    let accumulated = 0;

    function step(timestamp) {
      if (!lastTime) lastTime = timestamp;
      accumulated += timestamp - lastTime;
      lastTime = timestamp;

      const target = Math.min(
        totalWords,
        Math.floor(accumulated / intervalMs) + 1,
      );

      if (target > wordIndex) {
        wordIndex = target;
        setDisplayed(words.slice(0, wordIndex).join(" "));
        bottomRef?.current?.scrollIntoView({ behavior: "smooth" });
      }

      if (wordIndex < totalWords) {
        rafRef.current = requestAnimationFrame(step);
      } else {
        setDisplayed(body);
        setAnimating(false);
        bottomRef?.current?.scrollIntoView({ behavior: "smooth" });
        setTimeout(playDoneSound, 80);
      }
    }

    rafRef.current = requestAnimationFrame(step);

    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const renderedBody = isStringBody ? (animating ? displayed : body) : body;

  // Lightweight inline formatter — **bold**, *italic*, and auto-links for
  // emails, phone numbers and http(s) URLs. We deliberately avoid pulling in
  // a full markdown library; the knowledge base only ever uses these handful
  // of inline patterns, so a hand-rolled tokenizer keeps the bundle tiny.
  const INLINE_PATTERN =
    /(\*\*[^*\n]+\*\*)|(\*[^*\n]+\*)|(https?:\/\/[^\s)]+)|([\w.+-]+@[\w-]+\.[\w.-]+)|(\+?\d[\d\s().-]{6,}\d)/g;

  function renderInline(text, keyPrefix = "i") {
    const out = [];
    let lastIndex = 0;
    let m;
    let i = 0;
    INLINE_PATTERN.lastIndex = 0;
    while ((m = INLINE_PATTERN.exec(text)) !== null) {
      if (m.index > lastIndex) {
        out.push(text.slice(lastIndex, m.index));
      }
      const token = m[0];
      const linkColor = isDark ? "#5FCC93" : "#2F8F5C";
      if (m[1]) {
        out.push(
          <strong key={`${keyPrefix}-b-${i}`} style={{ fontWeight: 700 }}>
            {token.slice(2, -2)}
          </strong>,
        );
      } else if (m[2]) {
        out.push(
          <em key={`${keyPrefix}-i-${i}`} style={{ fontStyle: "italic" }}>
            {token.slice(1, -1)}
          </em>,
        );
      } else if (m[3]) {
        out.push(
          <a key={`${keyPrefix}-u-${i}`} href={token} target="_blank" rel="noreferrer"
             style={{ color: linkColor, fontWeight: 600, textDecoration: "underline" }}>
            {token}
          </a>,
        );
      } else if (m[4]) {
        out.push(
          <a key={`${keyPrefix}-m-${i}`} href={`mailto:${token}`}
             style={{ color: linkColor, fontWeight: 600, textDecoration: "underline" }}>
            {token}
          </a>,
        );
      } else if (m[5]) {
        const tel = token.replace(/[^\d+]/g, "");
        out.push(
          <a key={`${keyPrefix}-t-${i}`} href={`tel:${tel}`}
             style={{ color: linkColor, fontWeight: 600, textDecoration: "underline" }}>
            {token}
          </a>,
        );
      }
      lastIndex = m.index + token.length;
      i += 1;
    }
    if (lastIndex < text.length) {
      out.push(text.slice(lastIndex));
    }
    return out.length ? out : [text];
  }

  function renderText(text) {
    const paragraphs = text.split("\n\n");
    // During the typewriter reveal we strip the inline markers rather than
    // try to format partial strings — an unclosed `**` would otherwise show
    // the raw asterisks until the closing marker arrives a few words later.
    const stripMarkers = (s) => s.replace(/\*\*/g, "").replace(/(^|\s)\*(\S[^*\n]*?)\*(?=\s|$)/g, "$1$2");
    return paragraphs.map((para, pi) => {
      const lines = para.split("\n");
      return (
        <span key={pi}>
          {lines.map((line, li) => (
            <span key={li}>
              {animating ? stripMarkers(line) : renderInline(line, `p${pi}l${li}`)}
              {li < lines.length - 1 && <br />}
            </span>
          ))}
          {pi < paragraphs.length - 1 && (
            <span style={{ display: "block", height: "8px" }} />
          )}
        </span>
      );
    });
  }

  const timeLabel = (() => {
    const raw = msg.timestamp || msg.createdAt;
    if (!raw) return null;
    const d = new Date(raw);
    if (Number.isNaN(d.getTime())) return null;
    return d.toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
    });
  })();

  return (
    <div
      className={`zt-msg flex w-full gap-2 ${isUser ? "justify-end" : "justify-start"} mb-5`}
      style={{ animation: "msgIn 0.28s cubic-bezier(.22,1,.36,1) both" }}
    >
      <style>{`
        .zt-msg .zt-msg-time { opacity: 0; transition: opacity .18s ease; }
        .zt-msg:hover .zt-msg-time { opacity: 1; }
        @keyframes ztSugIn {
          from { opacity: 0; transform: translateY(4px); }
          to   { opacity: 1; transform: none; }
        }
        .zt-msg .zt-sug { animation: ztSugIn .22s ease both; }
      `}</style>

      {!isUser && (
        <div
          className="relative h-8 w-8 flex-shrink-0 rounded-full p-[1.5px]"
          style={{
            background:
              "conic-gradient(from 180deg at 50% 50%, #11496A, #44B97B, #3D8AB5, #11496A)",
          }}
        >
          <div
            className={`h-full w-full rounded-full flex items-center justify-center overflow-hidden ${
              isDark ? "bg-[#0E2B3D]" : "bg-[#E7F0F6]"
            }`}
          >
            <img
              src="./favicon.svg"
              alt=""
              className="h-full w-full object-cover"
            />
          </div>
        </div>
      )}

      <div
        className={`flex flex-col gap-1.5 ${isUser ? "items-end" : "items-start"} max-w-[82%]`}
      >
        {!isUser && (
          <div
            className={`flex items-center gap-1.5 px-1 text-[0.6rem] font-bold uppercase tracking-[0.16em] ${
              isDark ? "text-[#5FCC93]" : "text-[#2F8F5C]"
            }`}
            style={{ fontFamily: "'Space Grotesk','DM Sans',sans-serif" }}
          >
            <span>Kairos</span>
            {timeLabel && (
              <span
                className={`zt-msg-time font-medium normal-case tracking-normal ${
                  isDark ? "text-[#6B7A8A]" : "text-[#98A2B3]"
                }`}
              >
                · {timeLabel}
              </span>
            )}
          </div>
        )}

        <div
          className={
            isUser
              ? "rounded-2xl rounded-tr-sm px-4 py-2.5 text-white font-semibold text-sm shadow-[0_4px_14px_rgba(17,73,106,0.28)] bg-gradient-to-br from-[#11496A] via-[#2F8F5C] to-[#44B97B]"
              : isDark
                ? "rounded-2xl rounded-tl-sm border border-[#2D3748] bg-[#0F172A] px-4 py-3 text-[#F0F4F8] text-sm shadow-[0_2px_10px_rgba(0,0,0,0.25)]"
                : "rounded-2xl rounded-tl-sm border border-[#E4E7EC] bg-[#F8FAFC] px-4 py-3 text-[#101828] text-sm shadow-[0_2px_10px_rgba(17,73,106,0.06)]"
          }
        >
          {msg.typing ? (
            <TypingDots />
          ) : isStringBody ? (
            <span style={{ lineHeight: "1.5", display: "block" }}>
              {renderText(renderedBody)}
            </span>
          ) : (
            body
          )}
        </div>

        {isUser && timeLabel && (
          <span
            className={`zt-msg-time px-1 text-[0.6rem] font-medium ${
              isDark ? "text-[#6B7A8A]" : "text-[#98A2B3]"
            }`}
          >
            {timeLabel}
          </span>
        )}

        {!isUser && !msg.typing && msg.citations?.length > 0 && (
          <div className="flex flex-wrap gap-1 mt-0.5">
            {msg.citations.map((c, i) => (
              <span
                key={i}
                className={`rounded-full border px-2 py-0.5 text-[0.55rem] font-medium ${
                  isDark
                    ? "border-[#1F3D55] bg-[#0E2B3D] text-[#A6C7DC]"
                    : "border-[#A6C7DC] bg-[#E7F0F6] text-[#11496A]"
                }`}
              >
                {c.title}
              </span>
            ))}
          </div>
        )}

        {/* Suggestions are NOT gated on `animating` — users tap them mid-reveal
            (especially "Back to Main Menu"), so blocking interaction until the
            word-by-word reveal finishes felt like the bot wasn't responding. */}
        {!isUser &&
          !msg.typing &&
          msg.suggestions?.length > 0 && (
            <div className="grid grid-cols-2 gap-1.5 w-full mt-1">
              {msg.suggestions.map((s, i) => (
                <button
                  key={i}
                  onClick={() => onSuggestion(s)}
                  style={{ animationDelay: `${i * 60}ms` }}
                  className={`zt-sug rounded-xl border px-2.5 py-1.5 text-[0.68rem] font-semibold text-left leading-snug transition-all active:scale-95 hover:translate-y-[-1px] ${
                    isDark
                      ? "border-[#1F3D55] bg-[#0E2B3D] text-[#A6C7DC] hover:border-[#5FCC93] hover:bg-[#0D2A1A] hover:text-[#5FCC93] hover:shadow-[0_4px_12px_rgba(95,204,147,0.18)]"
                      : "border-[#A6C7DC] bg-[#E7F0F6] text-[#11496A] hover:border-[#44B97B] hover:bg-[#E8F7EE] hover:text-[#2F8F5C] hover:shadow-[0_4px_12px_rgba(68,185,123,0.18)]"
                  }`}
                >
                  {s}
                </button>
              ))}
            </div>
          )}
      </div>

      {/* {isUser && (
        <div className="flex-shrink-0 mt-0.5">
          <div
            className={`h-8 w-8 rounded-[13px] flex items-center justify-center text-[0.68rem] font-bold bg-green-300 ${
              isDark
                ? "border border-[rgba(255,255,255,0.1)] bg-[rgba(255,255,255,0.07)] text-[#8bc8d5]"
                : "border border-[rgba(26,199,191,0.3)] bg-[rgba(26,199,191,0.1)] text-[#1a7a75]"
            }`}
          >
            <img
              src="/avatar.svg"
              alt="User avatar"
              className="h-full w-full object-contain"
            />
          </div>
        </div>
      )} */}
    </div>
  );
}

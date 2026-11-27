import { HiOutlineChatBubbleLeft, HiOutlineXMark, HiOutlinePlus } from "react-icons/hi2";

export default function HistoryPanel({ sessions, onClose, onSelect, onNewChat, theme }) {
  const isDark = theme === "dark";

  const formatDate = (raw) => {
    if (!raw) return "--";
    const value = new Date(raw);
    if (Number.isNaN(value.getTime())) return raw;
    return value.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  };

  return (
    <div
      className={`absolute right-0 top-12 z-50 w-80 overflow-hidden rounded-2xl border shadow-2xl ${
        isDark
          ? "border-[#2D3748] bg-[#1A1F2E]"
          : "border-[#E4E7EC] bg-white"
      }`}
      style={{ animation: "panelIn 0.18s cubic-bezier(.22,1,.36,1) both" }}
    >
      {/* Header */}
      <div
        className={`flex items-center justify-between border-b px-4 py-3 ${
          isDark ? "border-[#2D3748]" : "border-[#E4E7EC]"
        }`}
      >
        <span
          className={`text-[0.62rem] font-black uppercase tracking-[0.18em] ${
            isDark ? "text-[#5FCC93]" : "text-[#2F8F5C]"
          }`}
          style={{ fontFamily: "'Space Grotesk','DM Sans',sans-serif" }}
        >
          Chat History
        </span>
        <button
          onClick={onClose}
          aria-label="Close history"
          className={`h-7 w-7 rounded-[10px] flex items-center justify-center border transition-all ${
            isDark
              ? "border-[#2D3748] text-[#A0ADB9] hover:border-[#F87171] hover:text-[#F87171] hover:bg-[rgba(248,113,113,0.08)]"
              : "border-[#E4E7EC] text-[#475467] hover:border-[#B42318] hover:text-[#B42318] hover:bg-[#FEF3F2]"
          }`}
        >
          <HiOutlineXMark className="h-4 w-4" />
        </button>
      </div>

      {/* "Start new" CTA — uses the brand gradient like the send button */}
      <div
        className={`border-b px-3 py-3 ${
          isDark ? "border-[#2D3748]" : "border-[#E4E7EC]"
        }`}
      >
        <button
          onClick={() => {
            onNewChat();
            onClose();
          }}
          className="flex w-full items-center justify-center gap-2 rounded-xl px-3 py-2 text-sm font-semibold text-white transition-all hover:brightness-110 active:scale-[0.98]"
          style={{
            background:
              "linear-gradient(135deg, #11496A 0%, #2F8F5C 55%, #44B97B 100%)",
            boxShadow:
              "0 4px 14px rgba(17,73,106,0.32), inset 0 1px 0 rgba(255,255,255,0.18)",
          }}
        >
          <HiOutlinePlus className="h-4 w-4" />
          Start a new conversation
        </button>
      </div>

      {/* Sessions */}
      <div
        className="zt-kairos-scroll max-h-72 overflow-y-auto p-2"
      >
        {sessions?.length ? (
          sessions.map((session, index) => (
            <button
              key={session.sessionId || index}
              onClick={() => {
                onSelect(session);
                onClose();
              }}
              className={`mb-1 flex w-full items-start gap-2.5 rounded-xl px-3 py-2.5 text-left text-sm transition-all ${
                isDark
                  ? "text-[#F0F4F8] hover:bg-[#0E2B3D]"
                  : "text-[#101828] hover:bg-[#E7F0F6]"
              }`}
            >
              <HiOutlineChatBubbleLeft
                className={`mt-0.5 h-4 w-4 flex-shrink-0 ${
                  isDark ? "text-[#5FCC93]" : "text-[#44B97B]"
                }`}
              />
              <div className="min-w-0 flex-1">
                <div className="truncate font-semibold">
                  {session.title || `Session ${index + 1}`}
                </div>
                {session.preview ? (
                  <div
                    className={`mt-0.5 line-clamp-2 text-[0.7rem] ${
                      isDark ? "text-[#A0ADB9]" : "text-[#475467]"
                    }`}
                  >
                    {session.preview}
                  </div>
                ) : null}
                <div
                  className={`mt-1 text-[0.65rem] uppercase tracking-wider ${
                    isDark ? "text-[#6B7A8A]" : "text-[#98A2B3]"
                  }`}
                  style={{ fontFamily: "'Space Grotesk','DM Sans',sans-serif" }}
                >
                  {formatDate(
                    session.lastMessageAt || session.createdAt || session.date,
                  )}
                </div>
              </div>
            </button>
          ))
        ) : (
          <div
            className={`flex flex-col items-center gap-2 py-10 text-center text-xs ${
              isDark ? "text-[#6B7A8A]" : "text-[#98A2B3]"
            }`}
          >
            <HiOutlineChatBubbleLeft className="h-7 w-7 opacity-60" />
            <div className="font-semibold">No history yet</div>
            <div className="px-6 text-[0.68rem] leading-relaxed">
              Conversations you start will appear here for 24 hours.
            </div>
          </div>
        )}
      </div>

      {/* Footer hint */}
      <div
        className={`border-t px-4 py-2 text-[0.62rem] ${
          isDark
            ? "border-[#2D3748] text-[#6B7A8A]"
            : "border-[#E4E7EC] text-[#98A2B3]"
        }`}
      >
        Chats stay available for 24 hours, then expire automatically.
      </div>
    </div>
  );
}

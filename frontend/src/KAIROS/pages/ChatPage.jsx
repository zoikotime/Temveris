import { useCallback, useEffect, useRef, useState } from "react";
import toast from "react-hot-toast";
import { useStore } from "../store/useStore";
import {
  createSession,
  describeApiError,
  endChatSession,
  fetchMailStatus,
  fetchHistory,
  fetchUserSessions,
  sendMessage as sendChatMessage,
} from "../services/api";

import ChatHeader from "../components/layout/ChatHeader";
import MessageBubble from "../components/chat/MessageBubble";
import TypingDots from "../components/chat/TypingDots";
import Composer from "../components/chat/Composer";
import MailResponse from "../components/chat/MailResponse";

const WELCOME_TEXT =
  "Hi there! I'm Kairos, your Temveris assistant.\n\nI'm here to help with clock-in/out, activity, leave, pay, privacy, and anything else about Temveris.\n\nWhat can I help you with today?";

function createWelcomeMessage(text = WELCOME_TEXT) {
  return {
    id: `welcome-${Date.now()}`,
    role: "assistant",
    content: text,
    suggestions: [],
    citations: [],
    timestamp: new Date().toISOString(),
  };
}

function normalizeMessage(message, index = 0) {
  return {
    id: message.id || message._id || `${message.role || "assistant"}-${index}`,
    role: message.role || "assistant",
    content: message.content ?? message.text ?? "",
    suggestions:
      message.suggestions ||
      message.quickReplies ||
      message.meta?.suggestions ||
      message.metadata?.suggestions ||
      [],
    citations: message.citations || [],
    timestamp: message.timestamp || new Date().toISOString(),
    nextAction: message.nextAction || null,
  };
}

function formatWaitTime(ms) {
  const totalMinutes = Math.ceil(ms / 60000);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;

  if (hours > 0 && minutes > 0) return `${hours}h ${minutes}m`;
  if (hours > 0) return `${hours}h`;
  return `${minutes}m`;
}

export default function ChatPage({ onEditDetails }) {
  const user = useStore((state) => state.user);
  const sessionId = useStore((state) => state.sessionId);
  const setSessionId = useStore((state) => state.setSessionId);
  const messages = useStore((state) => state.messages);
  const replaceMessages = useStore((state) => state.replaceMessages);
  const sessions = useStore((state) => state.sessions);
  const setSessions = useStore((state) => state.setSessions);
  const lang = useStore((state) => state.language);
  const setLang = useStore((state) => state.setLanguage);
  const theme = useStore((state) => state.theme);
  const toggleTheme = useStore((state) => state.toggleTheme);

  const [input, setInput] = useState("");
  const [isTyping, setIsTyping] = useState(false);
  const [openPanel, setOpenPanel] = useState(null);
  const [latestBotId, setLatestBotId] = useState(null);

  const bottomRef = useRef(null);
  // Skip the next sessionId-driven history fetch — set when we just
  // created the session ourselves (sendMessage) or just hydrated it from a
  // user-picked session, so we don't refetch the same messages we already
  // have in state and race-overwrite the optimistic update.
  const skipNextHistoryFetch = useRef(false);
  const isDark = theme === "dark";

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, isTyping]);

  useEffect(() => {
    if (!sessionId) return;
    if (skipNextHistoryFetch.current) {
      skipNextHistoryFetch.current = false;
      return;
    }

    fetchHistory(sessionId)
      .then((response) => {
        const historyMessages = (response.messages || []).map(normalizeMessage);
        setLatestBotId(null);
        replaceMessages(
          historyMessages.length ? historyMessages : [createWelcomeMessage()],
        );
      })
      .catch(() => {});
  }, [replaceMessages, sessionId]);

  useEffect(() => {
    if (!user?.email) return;

    fetchUserSessions()
      .then((response) => setSessions(response.sessions || []))
      .catch(() => {});
  }, [setSessions, user?.email, sessionId]);

  const sendMessage = useCallback(
    async (overrideText) => {
      const text = (overrideText ?? input).trim();
      if (!text || isTyping) return;
      if (!user?.email) {
        toast.error("User session missing. Please log in again.");
        return;
      }

      let currentSessionId = sessionId;

      const nextMessages = [
        ...messages,
        normalizeMessage({
          id: `${Date.now()}-user`,
          role: "user",
          content: text,
        }),
      ];

      replaceMessages(nextMessages);
      setInput("");
      setIsTyping(true);

      try {
        const response = await sendChatMessage({
          sessionId: currentSessionId || null,
          message: text,
          language: lang,
        });

        if (!currentSessionId && response.sessionId) {
          // We already have the user/bot messages in state — don't let the
          // sessionId-change effect refetch and overwrite them.
          skipNextHistoryFetch.current = true;
          await setSessionId(response.sessionId);
          currentSessionId = response.sessionId;
        }

        const botMsg = normalizeMessage({
          id: `${Date.now()}-assistant`,
          role: "assistant",
          content:
            response.message?.answer ||
            "I'm here to help. Could you clarify what you need?",
          suggestions: response.message?.suggestions || [],
          nextAction: response.message?.nextAction || null,
        });

        setLatestBotId(botMsg.id);
        replaceMessages([...nextMessages, botMsg]);
      } catch (err) {
        const info = describeApiError(err);
        console.error("[Kairos] sendMessage failed:", info, err);

        let toastMsg;
        let bubbleText;
        let suggestions = ["Try again", "Speak to a human agent"];

        switch (info.code) {
          case "UNAUTHORIZED":
            toastMsg = "Your session expired. Please sign in again.";
            bubbleText =
              "Your Temveris session has expired.\n\nSign in again and reopen Kairos to continue the chat.";
            suggestions = ["Try again"];
            break;
          case "FORBIDDEN":
            toastMsg = info.message || "Access denied.";
            bubbleText =
              "I can't access that conversation right now. Try starting a new chat.";
            suggestions = ["Try again"];
            break;
          case "RATE_LIMITED":
            toastMsg = "Slow down — too many messages.";
            bubbleText =
              "You're sending messages a bit too fast. Please wait a moment and try again.";
            break;
          case "TIMEOUT":
            toastMsg = "Kairos is taking too long to respond.";
            bubbleText =
              "That request timed out. Please try again in a moment.";
            break;
          case "NETWORK":
            toastMsg = "Can't reach Kairos. Check your connection.";
            bubbleText =
              "I couldn't reach the server. Check your connection and try again.";
            break;
          default:
            toastMsg = `Kairos error (${info.status || "network"}).`;
            bubbleText = `Something went wrong while processing your message.\n\nIf this keeps happening, contact support:\nEmail: support@temveris.example`;
        }

        toast.error(toastMsg);

        const errMsg = normalizeMessage({
          id: `${Date.now()}-error`,
          role: "assistant",
          content: bubbleText,
          suggestions,
        });

        setLatestBotId(errMsg.id);
        replaceMessages([...nextMessages, errMsg]);
      } finally {
        setIsTyping(false);
      }
    },
    [
      input,
      isTyping,
      lang,
      messages,
      replaceMessages,
      sessionId,
      user,
      setSessionId,
    ],
  );

  const handleMailClick = useCallback(async () => {
    if (!user?.email) {
      toast.error("User session missing. Please log in again.");
      return;
    }

    try {
      const status = await fetchMailStatus();
      if (status?.allowed === false) {
        toast.error(
          `Support mail limit reached. Please try again in ${formatWaitTime(
            status.msBeforeNextReset || 86400 * 1000,
          )}.`,
          { duration: 6000 },
        );
        return;
      }
    } catch {
      // If status check fails, still allow the user to open the panel.
    }

    const alreadyOpen = messages.some((m) => m.isMail === true);
    if (alreadyOpen) return;

    // The backend's /mail/send authorises against a real conversation row,
    // so if the user opens the panel without chatting first we have to
    // mint a session here. skipNextHistoryFetch suppresses the sessionId
    // effect, otherwise it would replace the messages array (and unmount
    // the mail panel we're about to add) with the empty-session welcome.
    if (!sessionId) {
      try {
        const created = await createSession();
        const newSid = created?.session?.sessionId;
        if (newSid) {
          skipNextHistoryFetch.current = true;
          await setSessionId(newSid, created?.session?.expiresAt || null);
        }
      } catch (err) {
        console.error("[Kairos] createSession (mail panel) failed:", err);
        toast.error("Couldn't start a chat session. Please try again.");
        return;
      }
    }

    const mailId = `mail-${Date.now()}`;

    const closeMail = () => {
      const current = useStore.getState().messages;
      replaceMessages(current.filter((m) => m.id !== mailId));
    };

    replaceMessages([
      ...useStore.getState().messages,
      {
        id: mailId,
        role: "assistant",
        isMail: true,
        content: <MailResponse theme={theme} onClose={closeMail} />,
      },
    ]);
  }, [messages, replaceMessages, sessionId, setSessionId, theme, user?.email]);

  const handleNewChat = useCallback(async () => {
    // Fire-and-forget — closing the prior session shouldn't block the new
    // one from opening; the user already mentally moved on.
    if (sessionId) {
      endChatSession(sessionId).catch(() => {});
    }

    skipNextHistoryFetch.current = true;
    await setSessionId(null);
    setLatestBotId(null);

    replaceMessages([
      createWelcomeMessage(
        "Starting a new conversation. What can I help you with?",
      ),
    ]);

    setInput("");
    setOpenPanel(null);
  }, [replaceMessages, sessionId, setSessionId]);

  const handleSelectSession = useCallback(
    async (session) => {
      if (!session?.sessionId) return;

      try {
        const response = await fetchHistory(session.sessionId);
        const historyMessages = (response.messages || []).map(normalizeMessage);
        skipNextHistoryFetch.current = true;
        await setSessionId(session.sessionId, session.expiresAt || null);
        setLatestBotId(null);
        replaceMessages(
          historyMessages.length ? historyMessages : [createWelcomeMessage()],
        );
        setInput("");
        setOpenPanel(null);
      } catch {
        toast.error("Couldn't load that session.");
      }
    },
    [replaceMessages, setSessionId],
  );

  const clearChat = useCallback(() => {
    setLatestBotId(null);
    replaceMessages([
      createWelcomeMessage("Chat cleared. What can I help you with?"),
    ]);
  }, [replaceMessages]);

  return (
    <div
      className={`flex flex-col h-full w-full transition-colors duration-300 ${
        isDark ? "bg-[#0F1117]" : "bg-[#F5F7FA]"
      }`}
      style={{
        fontFamily:
          "'DM Sans','Inter',system-ui,-apple-system,'Segoe UI',sans-serif",
      }}
    >
      <div
        className={`w-full flex flex-col flex-1 min-h-0 border rounded-2xl overflow-hidden shadow-2xl transition-all duration-300 ${
          isDark
            ? "border-[#2D3748] bg-[#1A1F2E] shadow-[0_8px_40px_rgba(14,43,61,0.6)]"
            : "border-[#E4E7EC] bg-white shadow-[0_8px_40px_rgba(17,73,106,0.1)]"
        }`}
      >
        <ChatHeader
          theme={theme}
          onToggleTheme={toggleTheme}
          lang={lang}
          onLangChange={setLang}
          openPanel={openPanel}
          onTogglePanel={(panel) =>
            setOpenPanel((prev) => (prev === panel ? null : panel))
          }
          onClosePanel={() => setOpenPanel(null)}
          sessions={sessions}
          onSelectSession={handleSelectSession}
          onNewChat={handleNewChat}
          onMailClick={handleMailClick}
          onEditDetails={onEditDetails}
        />

        <div
          className={`zt-kairos-scroll flex-1 overflow-y-auto p-3 sm:p-4 transition-colors duration-300 ${
            isDark
              ? "bg-[#1A1F2E] zt-kairos-bg-dots-dark"
              : "bg-white zt-kairos-bg-dots"
          }`}
        >
          {messages.map((message) => (
            <MessageBubble
              key={message.id}
              msg={message}
              onSuggestion={sendMessage}
              theme={theme}
              isNew={message.id === latestBotId}
              bottomRef={bottomRef}
            />
          ))}

          {isTyping && <TypingDots />}
          <div ref={bottomRef} />
        </div>

        <Composer
          input={input}
          setInput={setInput}
          isTyping={isTyping}
          onSend={sendMessage}
          onClear={clearChat}
          theme={theme}
        />
      </div>
    </div>
  );
}

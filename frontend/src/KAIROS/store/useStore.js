import { create } from "zustand";
import {
  clearDraft,
  clearSession,
  loadDraft,
  loadPrefs,
  loadSession,
  saveDraft,
  savePrefs,
  saveSession,
} from "../services/desktop";
import { fetchHistory } from "../services/api";

const defaultAssistantContext = {
  assistantName: "Kairos",
  productName: "Temveris Desktop Application",
  assistantBadge: "AI WORK ASSISTANT",
  statusText: "Live knowledge base active",
  welcomeMessage:
    "Hey there! I'm Kairos, your Temveris support assistant.\n\nI can help with attendance, activity tracking, screenshots, leave, pay, privacy, settings, and technical issues.\n\nWhat would you like help with today?",
  welcomeMessageHi:
    "Namaste! Main Kairos hoon, aapka Temveris support assistant.\n\nMain attendance, activity tracking, screenshots, leave, pay, privacy, settings aur technical issues mein help kar sakta hoon.\n\nAaj main kis cheez mein help karun?",
  quickActions: [],
  defaultSuggestions: [],
  retentionHours: 24,
};

function getWelcomeContent(
  language,
  assistantContext = defaultAssistantContext,
) {
  return language === "hi"
    ? assistantContext.welcomeMessageHi || assistantContext.welcomeMessage
    : assistantContext.welcomeMessage;
}

function createWelcomeMessage(
  language,
  assistantContext = defaultAssistantContext,
) {
  return [
    {
      id: "welcome",
      role: "assistant",
      content: getWelcomeContent(language, assistantContext),
      timestamp: new Date().toISOString(),
    },
  ];
}

// ─── Fetch chat history from backend ─────────────────────────────────────────
async function fetchSessionHistory(sessionId) {
  try {
    const data = await fetchHistory(sessionId);
    if (
      data.success &&
      Array.isArray(data.messages) &&
      data.messages.length > 0
    ) {
      return data.messages.map((m) => ({
        id: m.id,
        role: m.role,
        content: m.content,
        timestamp: m.timestamp,
        metadata: m.meta || {},
      }));
    }
    return null;
  } catch {
    return null;
  }
}

// ─── FIX: persist mailSent in localStorage so it survives reload ──────────────
function loadMailSent() {
  try {
    return localStorage.getItem("mailSent") === "true";
  } catch {
    return false;
  }
}

function persistMailSent(value) {
  try {
    localStorage.setItem("mailSent", String(value));
  } catch {
    // ignore
  }
}

function loadMainAppUser() {
  try {
    const email = (
      localStorage.getItem("email") ||
      localStorage.getItem("user_email") ||
      ""
    )
      .trim()
      .toLowerCase();
    if (!email) return null;

    return {
      name: (
        localStorage.getItem("name") ||
        localStorage.getItem("user_name") ||
        localStorage.getItem("full_name") ||
        ""
      ).trim(),
      email,
      company:
        (
          localStorage.getItem("company") ||
          localStorage.getItem("organization_name") ||
          localStorage.getItem("organization") ||
          ""
        ).trim(),
    };
  } catch {
    return null;
  }
}

export const useStore = create((set, get) => ({
  user: null,
  token: null,
  sessionId: null,
  expiresAt: null,

  isEditing: false,

  // ─── FIX Bug 1: load mailSent from localStorage on init ──────────────────
  mailSent: loadMailSent(),

  // ─── FIX Bug 1: also persist to localStorage whenever it changes ─────────
  setMailSent: (v) => {
    persistMailSent(v);
    set({ mailSent: v });
  },

  assistantContext: defaultAssistantContext,
  sessions: [],
  onboardingDraft: loadDraft(),
  messages: createWelcomeMessage("en", defaultAssistantContext),
  loading: false,
  hydrated: false,
  language: "en",
  theme: "dark",
  historyOpen: false,

  startEditing: () => set({ isEditing: true, user: null }),
  stopEditing: () => set({ isEditing: false }),

  setLoading: (loading) => set({ loading }),

  setAssistantContext: (assistantContext) =>
    set((state) => ({
      assistantContext,
      messages:
        state.messages.length === 1 && state.messages[0].id === "welcome"
          ? createWelcomeMessage(state.language, assistantContext)
          : state.messages,
    })),

  setSessions: (sessions) => set({ sessions }),

  setSessionId: async (sessionId, expiresAt = null) => {
    const current = get();
    if (current.user) {
      await saveSession({
        token: current.token,
        user: current.user,
        sessionId,
        expiresAt: expiresAt || current.expiresAt,
      });
    }
    set({
      sessionId,
      expiresAt: expiresAt || current.expiresAt,
    });
  },

  // ─── FIX Bug 3: fetch history from Supabase after login too ──────────────
  setUserSession: async (payload) => {
    await saveSession(payload);
    saveDraft(payload.user);

    // Try to restore existing chat history for this session
    let messages = null;
    if (payload.sessionId) {
      messages = await fetchSessionHistory(payload.sessionId);
    }

    set({
      token: payload.token || null,
      user: payload.user,
      sessionId: payload.sessionId,
      expiresAt: payload.expiresAt || null,
      onboardingDraft: payload.user,
      // Restore history if found, else show welcome
      messages:
        messages && messages.length > 0
          ? messages
          : createWelcomeMessage(get().language, get().assistantContext),
      hydrated: true,
      isEditing: false,
      // ─── FIX Bug 2: restore mailSent from localStorage, don't reset ──────
      mailSent: loadMailSent(),
    });
  },

  hydrateSession: async () => {
    if (get().hydrated) return;

    try {
      const session = await loadSession();
      const mainAppUser = loadMainAppUser();
      const prefs = loadPrefs();

      const language = prefs?.language || "en";
      const theme = prefs?.theme || "dark";

      if (!session && !mainAppUser) {
        set({
          hydrated: true,
          language,
          theme,
          onboardingDraft: loadDraft(),
          messages: createWelcomeMessage(language, get().assistantContext),
          // ─── FIX Bug 2: restore mailSent from localStorage ────────────────
          mailSent: loadMailSent(),
        });
        return;
      }

      const resolvedSession = session || {
        token: null,
        user: mainAppUser,
        sessionId: null,
        expiresAt: null,
      };

      // ─── Try to restore chat history from Supabase ────────────────────────
      let messages = null;
      if (resolvedSession.sessionId) {
        messages = await fetchSessionHistory(resolvedSession.sessionId);
      }

      set({
        token: resolvedSession.token || null,
        user: resolvedSession.user,
        sessionId: resolvedSession.sessionId,
        expiresAt: resolvedSession.expiresAt || null,
        language,
        theme,
        onboardingDraft: loadDraft() || resolvedSession.user,
        hydrated: true,
        // Restore history if found, else show welcome
        messages:
          messages && messages.length > 0
            ? messages
            : createWelcomeMessage(language, get().assistantContext),
        // ─── FIX Bug 2: restore mailSent from localStorage ────────────────
        mailSent: loadMailSent(),
      });
    } catch (err) {
      console.error("Session hydration failed:", err);
      set({ hydrated: true });
    }
  },

  appendMessage: (message) =>
    set((state) => ({
      messages: [...state.messages, message],
    })),

  replaceMessages: (messages) => set({ messages }),

  setLanguage: (language) => {
    savePrefs({ language, theme: get().theme });
    set((state) => ({
      language,
      messages:
        state.messages.length === 1 && state.messages[0].id === "welcome"
          ? createWelcomeMessage(language, state.assistantContext)
          : state.messages,
    }));
  },

  toggleTheme: () => {
    const nextTheme = get().theme === "dark" ? "light" : "dark";
    savePrefs({ language: get().language, theme: nextTheme });
    set({ theme: nextTheme });
  },

  saveOnboardingDraft: (draft) => {
    saveDraft(draft);
    set({ onboardingDraft: draft });
  },

  clearOnboardingDraft: () => {
    clearDraft();
    set({ onboardingDraft: null });
  },

  toggleHistory: () => set((state) => ({ historyOpen: !state.historyOpen })),

  closeHistory: () => set({ historyOpen: false }),

  logout: async () => {
    if (get().user) {
      saveDraft(get().user);
    }
    await clearSession();
    // ─── FIX: clear mailSent from localStorage on logout ─────────────────
    persistMailSent(false);
    set({
      user: null,
      token: null,
      sessionId: null,
      expiresAt: null,
      messages: createWelcomeMessage(get().language, get().assistantContext),
      hydrated: true,
      isEditing: false,
      mailSent: false,
    });
  },
}));

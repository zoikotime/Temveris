import { useEffect } from "react";
import ChatPage from "./pages/ChatPage";
import { useStore } from "./store/useStore";

function BootstrapState({ title, description }) {
  return (
    <div className="flex h-full w-full items-center justify-center bg-[rgba(5,11,6,0.98)] p-5 text-white">
      <div className="w-full max-w-sm rounded-2xl border border-white/10 bg-white/5 p-5 shadow-2xl">
        <p className="text-sm font-semibold uppercase tracking-[0.24em] text-emerald-300">
          Kairos
        </p>
        <h2 className="mt-2 text-xl font-semibold">{title}</h2>
        <p className="mt-3 text-sm leading-6 text-white/75">{description}</p>
      </div>
    </div>
  );
}

export default function ChatBotRoot() {
  const hydrateSession = useStore((state) => state.hydrateSession);
  const hydrated = useStore((state) => state.hydrated);
  const user = useStore((state) => state.user);

  useEffect(() => {
    hydrateSession();
  }, [hydrateSession]);

  if (!hydrated) {
    return (
      <BootstrapState
        title="Starting Kairos"
        description="Checking your Temveris session and preparing the assistant."
      />
    );
  }
  if (!user?.email) return null;

  return <ChatPage />;
}

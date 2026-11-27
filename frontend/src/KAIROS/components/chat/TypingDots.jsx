export default function TypingDots() {
  return (
    <div
      className="zt-typing-dots flex items-center gap-1.5 px-1 py-1"
      role="status"
      aria-label="Kairos is typing"
    >
      <style>{`
        @keyframes ztTypingWave {
          0%, 80%, 100% { transform: translateY(0);   opacity: 0.45; }
          40%           { transform: translateY(-4px); opacity: 1;    }
        }
        .zt-typing-dots span {
          background: linear-gradient(135deg, #11496A 0%, #44B97B 100%);
          animation: ztTypingWave 1.1s ease-in-out infinite;
        }
      `}</style>
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          style={{ animationDelay: `${i * 0.16}s` }}
          className="h-2 w-2 rounded-full shadow-[0_0_6px_rgba(68,185,123,0.45)]"
        />
      ))}
    </div>
  );
}

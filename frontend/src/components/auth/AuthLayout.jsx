import logo from "../../assets/Temveris_Logo_PNG.png";

export default function AuthLayout({
  children,
  footer,
  maxWidth = "max-w-[400px]",
  showLogo = true,
  aside = null,
}) {
  const card = (
    <div className="relative min-h-screen w-full flex items-center justify-center px-4 py-10 overflow-hidden bg-[#F4FAF7]">
      <div
        aria-hidden
        className="absolute inset-0 -z-30 bg-gradient-to-br from-brand-green-light via-white to-brand-blue-light bg-[length:200%_200%] animate-[gradientMove_14s_ease_infinite]"
      />
      <div
        aria-hidden
        className="absolute -z-20 w-[520px] h-[520px] -top-32 -left-32 rounded-full bg-brand-green/20 blur-[140px]"
      />
      <div
        aria-hidden
        className="absolute -z-20 w-[460px] h-[460px] -bottom-40 -right-32 rounded-full bg-brand-blue/15 blur-[150px]"
      />

      <div className={`w-full ${maxWidth} page-enter-active`}>
        <div className="rounded-[28px] p-[1.5px] bg-gradient-to-b from-brand-green/35 via-brand-green/10 to-brand-blue/25 shadow-[0_30px_80px_-30px_rgba(17,73,106,0.25)]">
          <div className="rounded-[22px] bg-white px-6 sm:px-7 py-7 sm:py-8">
            {showLogo && (
              <div className="flex justify-center mb-5">
                <img
                  src={logo}
                  alt="Temveris"
                  className="h-9 sm:h-10 w-auto logo-glow select-none"
                  draggable={false}
                />
              </div>
            )}
            {children}
          </div>
        </div>

        {footer && (
          <div className="mt-5 text-center text-xs text-slate-500">
            {footer}
          </div>
        )}
      </div>
    </div>
  );

  // Optional panel on the left at desktop widths (the login shell's marketing
  // panel). Below `lg` it is not rendered visibly and the card stands alone.
  // The panel scrolls with the page at its natural height so the whole
  // artwork can be seen; the form column keeps the card pinned in view.
  if (!aside) return card;
  return (
    <div className="flex min-h-screen w-full items-stretch">
      <aside className="hidden lg:block lg:w-[52%] xl:w-1/2 shrink-0">
        {aside}
      </aside>
      <div className="flex-1 min-w-0 bg-[#F4FAF7]">
        <div className="lg:sticky lg:top-0">{card}</div>
      </div>
    </div>
  );
}

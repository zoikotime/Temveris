/**
 * Marketing panel on the left of the auth shell (desktop widths only — the
 * layout hides it below `lg`, where the form card stands alone as before).
 *
 * The panel is the approved design artwork itself, so it matches the Figma
 * frame exactly. It renders at its natural height and scrolls with the page,
 * so the whole frame (down to the desk scene) is visible; it never shrinks
 * below the screen height.
 */
import hero from "../../assets/login-hero.jpg";

export default function AuthHeroPanel() {
  return (
    <div className="relative w-full bg-[#06281c]">
      <img
        src={hero}
        alt="Temveris. Time, verified. Trust, built in. Smarter time tracking. Healthier teams. A stronger tomorrow. Accurate time tracking, team productivity insights, projects and billing-ready data, enterprise-grade security and privacy."
        className="block w-full h-auto min-h-screen object-cover object-top select-none"
        draggable={false}
        decoding="async"
      />
    </div>
  );
}

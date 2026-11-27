"""Derive every logo/icon file in the repo from the masters in ./brand/.

Masters (each optional; a missing one falls back to a single-colour
"Temveris" text placeholder):
  brand/icon.png        square app icon, >= 1024 px, transparent background
  brand/logo.png        horizontal wordmark, >= 1600 px wide, transparent
  brand/icon.svg        copied to frontend/public/favicon.svg
  brand/logo.svg        copied to frontend/src/assets/Temveris_Logo.svg
  brand/logo-white.svg  copied to frontend/src/assets/Temveris_Logo_White.svg

Run from the repo root:  backend/venv/Scripts/python brand/build_assets.py
(Pillow is the only dependency; it is already in backend/requirements.txt.)
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
BRAND = ROOT / "brand"
COLOR = "#11496A"  # current brand-blue in frontend/tailwind.config.js
NAME = "Temveris"

ICON_SVG = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="13" fill="{COLOR}"/>
  <path d="M16 16.64h32v8.32H16zM27.84 16.64h8.32v32H27.84z" fill="#FFFFFF"/>
</svg>
"""


def wordmark_svg(fill):
    # Same 582x92 proportions as the previous wordmark, so width-sized layouts
    # (e.g. the admin sidebar) keep their height.
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 582 92">
  <text x="4" y="89" textLength="574" lengthAdjust="spacing"
        font-family="'Segoe UI', Helvetica, Arial, sans-serif"
        font-size="120" font-weight="700" fill="{fill}">{NAME}</text>
</svg>
"""


def placeholder_icon(px=1024):
    s = px * 4  # draw large, downsample for clean edges
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.2), fill=COLOR)
    bar, top = int(s * 0.13), int(s * 0.26)
    d.rectangle([s // 4, top, s - s // 4, top + bar], fill="white")              # crossbar
    d.rectangle([(s - bar) // 2, top, (s + bar) // 2, int(s * 0.76)], fill="white")  # stem
    return im.resize((px, px), Image.LANCZOS)


def placeholder_wordmark(fill, height=273):
    # Pillow's bundled scalable font: no system font files are read.
    font = ImageFont.load_default(size=height * 3)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    stroke = height // 12  # thickens the regular face toward a bold weight
    l, t, r, b = probe.textbbox((0, 0), NAME, font=font, stroke_width=stroke)
    im = Image.new("RGBA", (r - l, b - t), (0, 0, 0, 0))
    ImageDraw.Draw(im).text((-l, -t), NAME, font=font, fill=fill,
                            stroke_width=stroke, stroke_fill=fill)
    scale = height / im.height
    return im.resize((round(im.width * scale), height), Image.LANCZOS)


def fit(im, w, h):
    """Scale im to fit inside w x h, centred on a transparent canvas."""
    k = min(w / im.width, h / im.height)
    small = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.LANCZOS)
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    canvas.paste(small, ((w - small.width) // 2, (h - small.height) // 2), small)
    return canvas


def master(name, fallback):
    p = BRAND / name
    return Image.open(p).convert("RGBA") if p.exists() else fallback()


def text(name, fallback):
    p = BRAND / name
    return p.read_text(encoding="utf-8") if p.exists() else fallback


def main():
    # Raster and vector masters come in groups: one real file next to a generated
    # placeholder twin would ship two different logos (e.g. real PNG icons, but
    # the placeholder SVG favicon that browsers prefer).
    for group in (("icon.png", "icon.svg"), ("logo.png", "logo.svg", "logo-white.svg")):
        have = [n for n in group if (BRAND / n).exists()]
        if have and len(have) != len(group):
            raise SystemExit(f"brand/: supply {', '.join(group)} together (found only {', '.join(have)})")

    icon = master("icon.png", placeholder_icon)
    logo = master("logo.png", lambda: placeholder_wordmark(COLOR))
    out = lambda rel: ROOT / rel  # noqa: E731

    # Web app
    logo.save(out("frontend/src/assets/Temveris_Logo_PNG.png"))
    fit(icon, 512, 512).save(out("frontend/src/assets/Temveris_Icon_PNG.png"))
    out("frontend/src/assets/Temveris_Logo.svg").write_text(text("logo.svg", wordmark_svg(COLOR)), encoding="utf-8")
    out("frontend/src/assets/Temveris_Logo_White.svg").write_text(
        text("logo-white.svg", wordmark_svg("#FFFFFF")), encoding="utf-8")
    out("frontend/public/favicon.svg").write_text(text("icon.svg", ICON_SVG), encoding="utf-8")
    og = Image.new("RGB", (1000, 1000), "white")  # favicon.jpg doubles as og:image
    og.paste(fit(icon, 1000, 1000), (0, 0), fit(icon, 1000, 1000))
    og.save(out("frontend/public/favicon.jpg"), quality=92)

    # Email header (rendered at 180x28, so 2x)
    fit(logo, 360, 56).save(out("backend/app/email_assets/temveris_logo.png"))

    # Desktop app
    square = fit(icon, 1024, 1024)
    square.save(out("desktop-tracker/assets/icon.png"))
    square.save(out("desktop-tracker/assets/icon.ico"),
                sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
    square.save(out("desktop-tracker/assets/icon.icns"))
    for n in (16, 32, 48, 64, 128, 256, 512):
        fit(icon, n, n).save(out(f"desktop-tracker/assets/icons/{n}x{n}.png"))

    # Browser extension
    for n in (16, 48, 128):
        fit(icon, n, n).save(out(f"browser-extension/icons/icon{n}.png"))

    used = [n for n in ("icon.png", "logo.png", "icon.svg", "logo.svg", "logo-white.svg") if (BRAND / n).exists()]
    print("masters used:", used or "none (placeholder)")


if __name__ == "__main__":
    main()

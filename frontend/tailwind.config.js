/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,jsx,ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Temveris brand palette (logo-aligned)
        // Logo dark blue: #11496A   |   Logo green: #44B97B
        "brand-blue":        "#11496A",
        "brand-blue-dark":   "#0E3B57",
        "brand-blue-light":  "#E7F0F6",
        "brand-blue-mid":    "#A6C7DC",
        "brand-green":       "#44B97B",
        "brand-green-dark":  "#2F8F5C",
        "brand-green-light": "#E8F7EE",
        "brand-green-mid":   "#A8E0BD",
        // Primary call-to-action teal. The same #1FB389 the Log in button uses
        // inline (pages/Login.jsx), named here so the activation flow references
        // the colour rather than repeating the hex a third time.
        "brand-teal":        "#1FB389",
        "brand-teal-dark":   "#179973",
        // Page ground for the activation flow: a near-white with a hint of the
        // brand green, so the white cards read as raised without a heavy shadow.
        "brand-canvas":      "#F5F8F8",
      },
      boxShadow: {
        brand:      "0 4px 14px rgba(17,73,106,0.30)",
        brandSoft:  "0 2px 8px rgba(17,73,106,0.20)",
        brandGreen: "0 4px 14px rgba(68,185,123,0.30)",
      },
      backgroundImage: {
        "brand-gradient":      "linear-gradient(135deg,#11496A 0%,#44B97B 100%)",
        "brand-gradient-soft": "linear-gradient(135deg,#E7F0F6 0%,#E8F7EE 100%)",
      },
    },
  },
  plugins: [],
};

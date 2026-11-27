import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// Backend the dev server proxies to. Override with VITE_DEV_API_TARGET (shell or
// frontend/.env) when port 8000 is already taken on this machine.
const API =
  loadEnv("development", process.cwd(), "").VITE_DEV_API_TARGET ||
  "http://127.0.0.1:8000";

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // Strip console.log/debug/info (and debugger) from PRODUCTION bundles only.
  // `pure` lets esbuild drop these unused-result calls during minification;
  // dev (unminified) keeps them. console.warn / console.error are preserved
  // for legitimate error reporting.
  esbuild: {
    pure: ["console.log", "console.debug", "console.info"],
    drop: ["debugger"],
  },
  server: {
    proxy: {
      "/auth": API,
      "/api": API,
      "/leave": API,
      "/uploads": API,
      "/employees": API,
      "/tracking": API,
      "/browser": API,
      // A browser navigating directly to a /settings/* SPA route — e.g. the
      // Stripe Checkout redirect target — must receive the app shell, not the
      // /settings API. fetch() calls don't send Accept: text/html, so they
      // still proxy through untouched. Mirrors spa_collision_guard in main.py.
      "/settings": {
        target: API,
        bypass: (req) =>
          (req.headers.accept || "").includes("text/html") ? "/index.html" : undefined,
      },
      "/break": API,
      "/risk-alert": API,
      "/org": API,
      "/static/org": API,
      "/payroll": API,
      "/ai": API,
      "/pow": API,
      "/reports": API,
      "/holidays": API,
      "/schedule": API,
      "/profile": API,
      "/my-data": API,
      "/notifications": API,
      // Same split as /settings: opening an /admin page directly (refresh, a
      // new-tab link such as Time Playback ?tab=timelapse&user=&date=) gets the
      // app shell; the app's own /admin/* API fetches still reach the backend.
      "/admin": {
        target: API,
        bypass: (req) =>
          (req.headers.accept || "").includes("text/html") ? "/index.html" : undefined,
      },
      "/worker": API,
      "/v1": API,
      "/ws": { target: API.replace(/^http/, "ws"), ws: true },
    },
  },
  build: {
    target: "es2020",
    cssCodeSplit: true,
    sourcemap: false,
    reportCompressedSize: false,
    chunkSizeWarningLimit: 1500,
    rollupOptions: {
      output: {
        // Split heavy vendor libraries into their own chunks so the initial
        // login screen does not have to download recharts/icons/etc.
        manualChunks(id) {
          if (!id.includes("node_modules")) return;
          // xlsx is ~250 KB and only used by the Export-to-Excel button on
          // the Reports tab — keep it in its own chunk so route chunks
          // never accidentally bundle it.
          if (id.includes("xlsx")) return "vendor-xlsx";
          // exceljs is ~1 MB and, like xlsx, is only reached from the
          // Export-to-Excel path (`await import("exceljs")` in
          // utils/musterReport.js). Without its own chunk the catch-all below
          // put it in the shared `vendor` bundle — which the entry statically
          // imports — so the dynamic import bought nothing and every page,
          // including login, downloaded it.
          if (id.includes("exceljs")) return "vendor-exceljs";
          // Supabase is only reached from the lazy SSO-callback route; give it
          // its own chunk so it never rides along in the shared `vendor` bundle
          // that loads on every page.
          if (id.includes("@supabase")) return "vendor-supabase";
          if (id.includes("recharts") || id.includes("d3-"))
            return "vendor-charts";
          if (id.includes("react-router")) return "vendor-router";
          if (id.includes("react-dom") || id.includes("scheduler"))
            return "vendor-react";
          // Co-locate React core with react-dom instead of letting it fall into
          // the generic `vendor` catch-all.
          if (id.includes("node_modules/react/") || id.includes("react/jsx-runtime"))
            return "vendor-react";
          if (id.includes("lucide-react")) return "vendor-icons";
          if (id.includes("react-hot-toast")) return "vendor-toast";
          if (id.includes("react-transition-group")) return "vendor-transition";
          if (id.includes("axios")) return "vendor-axios";
          return "vendor";
        },
      },
    },
  },
  optimizeDeps: {
    include: [
      "react",
      "react-dom",
      "react-router-dom",
      "axios",
      "react-hot-toast",
      "lucide-react",
    ],
  },
});

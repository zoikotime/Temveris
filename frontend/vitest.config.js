// Vitest config for the frontend.
// Run with: npm run test     (after `npm i -D vitest @testing-library/react @testing-library/jest-dom jsdom`)
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/__tests__/setup.js"],
    css: false,
    include: ["src/__tests__/**/*.test.{js,jsx}"],
  },
});

"use strict";

/**
 * dev.js — cross-platform launcher for `npm run dev`.
 *
 * Why this exists:
 *   The previous dev script used Unix-style inline env vars
 *   (`NODE_ENV=development electron .`) which break on Windows PowerShell
 *   with: "'NODE_ENV' is not recognized as an internal or external command".
 *
 *   We also need ZOIKO_URL pointed at the local Vite dev server so the
 *   Electron window loads YOUR LOCAL React code with hot-reload, instead
 *   of https://app.temveris.example.
 *
 * Windows path-with-spaces caveat:
 *   Spawning with `shell: true` on Windows breaks when any path in the
 *   command line contains a space (e.g. user folders like
 *   `C:\Users\HARISH REDDY\…`). Instead of building the electron.cmd path
 *   ourselves and shelling out, we use the official `electron` npm
 *   package — `require("electron")` returns the absolute path to the
 *   electron binary as a string, which spawn() can call directly with no
 *   shell and no quoting headaches.
 */

const { spawn } = require("child_process");
const path = require("path");
const electronBin = require("electron"); // string path to electron.exe

const env = {
  ...process.env,
  NODE_ENV: "development",
  ZOIKO_URL: process.env.ZOIKO_URL || "http://localhost:5173",
};

const appRoot = path.join(__dirname, "..");

console.log(`[dev] ZOIKO_URL = ${env.ZOIKO_URL}`);
console.log(`[dev] NODE_ENV  = ${env.NODE_ENV}`);
console.log(`[dev] electron  = ${electronBin}`);
console.log(`[dev] Launching Electron…\n`);

const child = spawn(electronBin, ["."], {
  stdio: "inherit",
  env,
  cwd: appRoot,
  // Critical: no `shell: true` — spawn() will exec the binary directly,
  // which means spaces in the user-profile path don't need escaping.
});

child.on("exit", (code) => process.exit(code ?? 0));
child.on("error", (err) => {
  console.error("[dev] Failed to launch electron:", err.message);
  process.exit(1);
});

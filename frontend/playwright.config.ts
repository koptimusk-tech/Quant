import { defineConfig } from "@playwright/test";

// End-to-end smoke test. Expects the API on :8000 (with the synthetic demo loaded) and
// the Vite dev server on :5173; see README "Frontend tests".
export default defineConfig({
  testDir: "./e2e",
  use: {
    baseURL: process.env.QAL_WEB_URL ?? "http://127.0.0.1:5173",
    launchOptions: process.env.PW_CHROMIUM_PATH ? { executablePath: process.env.PW_CHROMIUM_PATH } : {},
  },
});

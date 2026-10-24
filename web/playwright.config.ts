import { defineConfig } from "@playwright/test";

// In CI, `npx playwright install chromium` provides the browser. In locked-down sandboxes without CDN access,
// set USE_SPARTICUZ=1 to use the Chromium bundled in the @sparticuz/chromium npm package instead.
const sparticuz = process.env.USE_SPARTICUZ === "1" ? (await import("@sparticuz/chromium")).default : null;
const executablePath = sparticuz ? await sparticuz.executablePath() : undefined;

export default defineConfig({
  testDir: "./e2e",
  timeout: 150_000,
  expect: { timeout: 60_000 },
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://127.0.0.1:3000",
    viewport: { width: 1360, height: 900 },
    trace: "retain-on-failure",
    launchOptions: sparticuz ? { executablePath, args: sparticuz.args.filter((a: string) => !["--single-process", "--no-zygote"].includes(a)), headless: true } : {},
  },
});

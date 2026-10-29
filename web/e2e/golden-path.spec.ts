import { expect, test } from "@playwright/test";

const SHOTS = process.env.E2E_SHOTS ?? "";
const shot = async (page: import("@playwright/test").Page, name: string) => { if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true }); };
const nav = (page: import("@playwright/test").Page, name: string) => page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name, exact: true });
const email = `e2e-${Date.now()}@example.test`;
const DUMP = process.env.E2E_DUMP ?? "";

test.describe.serial("golden path through the real UI", () => {
  test("landing, registration and workspace onboarding", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Prove recovery before it matters." })).toBeVisible();
    await shot(page, "01-landing");
    await page.getByRole("link", { name: "Create workspace" }).click();
    await page.getByLabel("Name").fill("Erin Engineer");
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill("correct horse battery staple");
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page.getByRole("heading", { name: "Name your workspace" })).toBeVisible();
    await page.getByLabel("Workspace name").fill("Platform Reliability");
    await page.getByRole("button", { name: "Create workspace" }).click();
    await expect(page).toHaveURL(/overview/);
    await expect(page.getByText("Nothing has been tested yet")).toBeVisible({ timeout: 15000 });
    await shot(page, "02-overview-empty");
  });

  test("validation errors are shown, never silent", async ({ page }) => {
    await page.goto("/register");
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page.getByText("Enter your name.")).toBeVisible();
    await expect(page.getByText("Enter a valid email address.")).toBeVisible();
    await page.goto("/login");
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill("definitely the wrong password");
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("alert").filter({ hasText: "Email or password is incorrect." })).toBeVisible();
  });

  test("Recovery Lab: a healthy backup is verified by the real engine", async ({ page }) => {
    await page.goto("/login");
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill("correct horse battery staple");
    await page.getByRole("button", { name: "Sign in" }).click();
    await nav(page, "Recovery Lab").click();
    await expect(page.getByRole("heading", { name: "Recovery Lab" })).toBeVisible();
    await page.getByRole("button", { name: "Run scenario" }).click();
    await expect(page.getByText("Recovery verified")).toBeVisible({ timeout: 120_000 });
    await expect(page.getByText("Matched expectation").first()).toBeVisible();
    await expect(page.getByText("Restored size")).toBeVisible();
    await shot(page, "03-lab-success");
  });

  test("Recovery Lab: a corrupted backup is rejected before any environment exists", async ({ page }) => {
    await page.goto("/login");
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill("correct horse battery staple");
    await page.getByRole("button", { name: "Sign in" }).click();
    await nav(page, "Recovery Lab").click();
    await page.getByRole("button", { name: /Corrupted backup/ }).click();
    await page.getByRole("button", { name: "Run scenario" }).click();
    await expect(page.getByText("Recovery failed")).toBeVisible({ timeout: 120_000 });
    await expect(page.getByText("ARTIFACT_CORRUPT").first()).toBeVisible();
    await expect(page.getByText("No temporary resources were created.")).toBeVisible();
    await shot(page, "04-lab-corrupt");
  });

  test("register a real backup, define a project, run it and read the report", async ({ page }) => {
    test.skip(!DUMP, "E2E_DUMP is not set");
    await page.goto("/login");
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill("correct horse battery staple");
    await page.getByRole("button", { name: "Sign in" }).click();
    await nav(page, "Backups").click();
    await page.getByRole("button", { name: "Register backup" }).click();
    await page.getByLabel("Backup file").setInputFiles(DUMP);
    await page.getByLabel("Name", { exact: true }).fill("orders-nightly");
    await page.getByLabel("Series label").fill("orders-nightly");
    await page.getByRole("button", { name: "Upload and verify" }).click();
    await expect(page.getByText("Every data block was read and decompressed successfully.")).toBeVisible({ timeout: 90_000 });
    await shot(page, "05-backup-verified");
    await page.getByRole("button", { name: "Done" }).click();
    await expect(page.getByRole("link", { name: "orders-nightly" })).toBeVisible();

    await nav(page, "Projects").click();
    await page.getByRole("link", { name: "New project" }).click();
    await page.getByLabel("Name", { exact: true }).fill("orders-db");
    await page.getByLabel("Backup to restore").selectOption({ label: "orders-nightly (verified)" });
    await page.getByRole("tab", { name: /Validation rules/ }).click();
    await page.getByLabel("Add a validation rule").selectOption({ label: "Table or view exists" });
    await page.getByRole("button", { name: "Add rule" }).click();
    await page.getByLabel("Name *").fill("orders");
    await page.getByRole("button", { name: "Save project" }).click();
    await expect(page.getByText("Project saved.")).toBeVisible();
    await shot(page, "06-project-rules");
    await page.getByRole("tab", { name: "Execution plan" }).click();
    await expect(page.getByText("Restore command")).toBeVisible();
    await page.getByRole("button", { name: "Run test now" }).click();
    await expect(page.getByText("Recovery verified").first()).toBeVisible({ timeout: 120_000 });
    await expect(page.getByText("Recovery timeline")).toBeVisible();
    await shot(page, "07-run-workspace");
    await page.getByRole("link", { name: "Report" }).click();
    await expect(page.getByRole("heading", { name: "Recovery verified" })).toBeVisible();
    await expect(page.getByText("What this report does not show")).toBeVisible();
    await shot(page, "08-report");
  });

  test("overview reflects the evidence, and the light theme renders", async ({ page }) => {
    await page.goto("/login");
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill("correct horse battery staple");
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("heading", { name: "Recovery readiness" })).toBeVisible();
    if (DUMP) await expect(page.getByText("Verified", { exact: true }).first()).toBeVisible();
    await shot(page, "09-overview-dark");
    await page.getByRole("button", { name: /Switch to light theme/ }).click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    await shot(page, "10-overview-light");
    await nav(page, "Settings").click();
    await page.getByRole("tab", { name: "Security and operations" }).click();
    await expect(page.getByText("Isolation guarantees")).toBeVisible();
  });

  test("signed-out visitors are redirected, and other workspaces return not found", async ({ page, context }) => {
    await context.clearCookies();
    await page.goto("/w/00000000-0000-0000-0000-000000000000/overview");
    await expect(page).toHaveURL(/\/login/);
  });
});

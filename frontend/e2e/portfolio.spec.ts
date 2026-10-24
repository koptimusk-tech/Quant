import { expect, test } from "@playwright/test";

// Run a portfolio backtest on the labelled synthetic strategies and read the risk manager's decisions.
test("run a synthetic portfolio and inspect risk", async ({ page }) => {
  test.setTimeout(180_000);
  await page.goto("/portfolio");
  await expect(page.getByRole("heading", { name: "Portfolio & Risk" })).toBeVisible();
  await page.getByLabel("include synthetic strategies (engine testing only)").check();
  await expect(page.getByText("SYN weak (fails eligibility)")).toBeVisible();
  await expect(page.getByLabel("select SYN:weak")).toBeDisabled();
  await page.getByLabel("inverse_vol").uncheck();
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(page.getByText("Portfolio run queued")).toBeVisible();
  const row = page.locator("tbody tr", { hasText: "risk_budget, equal_weight" }).first();
  await expect(row.getByText("succeeded")).toBeVisible({ timeout: 150_000 });
  await row.getByRole("button", { name: "open" }).click();
  await expect(page.getByText(/generated from a seeded factor model/).first()).toBeVisible();
  await expect(page.getByRole("heading", { name: "Construction methods compared" })).toBeVisible();
  await expect(page.getByText("estimate · not a forecast").first()).toBeVisible();
  await expect(page.getByText("truncation_invariance", { exact: false })).toBeVisible();
  await expect(page.getByRole("cell", { name: "max_weight" }).first()).toBeVisible();
});

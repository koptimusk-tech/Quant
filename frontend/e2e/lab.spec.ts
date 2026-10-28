import { expect, test } from "@playwright/test";

// Generate momentum hypotheses on the demo's synthetic data, run one through the pipeline and read its evidence.
test("generate, run and inspect a lab experiment", async ({ page }) => {
  test.setTimeout(120_000);
  await page.goto("/lab");
  await expect(page.getByRole("heading", { name: "Experiment Lab" })).toBeVisible();
  for (const f of ["mean_reversion", "factor", "anomaly", "pairs", "regime"]) {
    await page.getByLabel(f, { exact: true }).uncheck();
  }
  await page.getByRole("button", { name: "Generate proposals" }).click();
  await expect(page.getByRole("button", { name: /Queue \d+ selected/ })).toBeVisible();
  const picks = page.getByRole("checkbox", { name: /^Pick / });
  const n = await picks.count();
  for (let i = 1; i < n; i++) await picks.nth(i).uncheck(); // run one proposal only
  await page.getByRole("button", { name: /Queue 1 selected/ }).click();
  await expect(page.getByText("1 experiments queued")).toBeVisible();
  const row = page.locator("tbody tr", { hasText: "time-series momentum" }).first();
  await expect(row.getByText("COMPLETED")).toBeVisible({ timeout: 90_000 });
  await row.getByRole("button", { name: "Open" }).click();
  await expect(page.getByRole("tab", { name: "B Baselines" })).toBeVisible();
  await page.getByRole("tab", { name: "B Baselines" }).click();
  // The ranking can also show "permuted_signal" (baselines beaten): check the row only the baselines tab has.
  await expect(page.getByRole("cell", { name: /circularly shifted/ })).toBeVisible();
  await page.getByRole("tab", { name: "Versions" }).click();
  await expect(page.getByText("this one")).toBeVisible();
  await expect(page.getByRole("link", { name: "Export JSON" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Metrics" }).first()).toBeVisible();
  await expect(page.getByRole("link", { name: "Export ranking CSV" })).toBeVisible();
});

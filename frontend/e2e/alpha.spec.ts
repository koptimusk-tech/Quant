import { expect, test } from "@playwright/test";

// One bounded research cycle from the UI: hypotheses appear with their origin, objections and evidence.
test("run a research cycle and inspect a hypothesis", async ({ page }) => {
  test.setTimeout(120_000);
  await page.goto("/alpha");
  await expect(page.getByRole("heading", { name: "Alpha Research" })).toBeVisible();
  await page.getByLabel("include synthetic data").check();
  await page.getByLabel("Max experiments").fill("1");
  await page.getByRole("button", { name: "Run research cycle" }).click();
  await expect(page.getByText("Research cycle queued")).toBeVisible();
  await expect(page.getByRole("cell", { name: "succeeded" }).first()).toBeVisible({ timeout: 90_000 });
  await expect(page.locator("th", { hasText: "Validated net profitability" })).toBeVisible();
  await expect(page.getByText("No point-in-time fundamentals", { exact: false }).first()).toBeVisible();
  const tested = page
    .locator("tbody tr", { hasText: /no OOS evidence|passed walk-forward/ })
    .filter({ hasNotText: "manual" })
    .first();
  await expect(tested).toBeVisible({ timeout: 30_000 });
  await tested.getByRole("link").click();
  await expect(page.getByText("Adversarial reviewer objections")).toBeVisible();
  await expect(page.getByText("default_costs", { exact: true })).toBeVisible();
  await expect(page.getByText("Experiments compared", { exact: false })).toBeVisible();
  await expect(page.getByText("holdout_tested_once", { exact: true })).toBeVisible();
});

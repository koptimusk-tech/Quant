import { expect, test } from "@playwright/test";

// From an anomaly to a validated (or rejected) hypothesis, entirely through the UI.
test("investigate an anomaly, register a hypothesis and validate it", async ({ page }) => {
  test.setTimeout(90_000);
  await page.goto("/");
  await page.selectOption('select[aria-label="Detector"]', "pair_divergence");
  await page.locator("tbody tr").first().click();
  await page.getByRole("link", { name: "Investigate → hypothesis" }).click();
  await expect(page.getByRole("heading", { name: "Quant Research Lab" })).toBeVisible();
  // The form is prefilled from the anomaly once it loads (slower while a background job runs); wait for it.
  await expect(page.getByLabel("Title", { exact: true })).toHaveValue(/follow-up/, { timeout: 20_000 });
  await page.locator("textarea").nth(0).fill("Two assets driven by a common factor should not drift apart persistently after costs.");
  await page.locator("textarea").nth(1).fill("Relative-value traders close temporary deviations of the spread around its rolling fit.");
  await page.getByLabel("Entry rule").fill("Enter when |z| >= entry_z on the rich leg");
  await page.getByLabel("Exit rule").fill("Exit when |z| <= exit_z or after max_hold bars");
  await page.getByLabel("Horizon (bars)").fill("5");
  await page.getByLabel("Risks (one per line)").fill("relationship break");
  await page.getByLabel("Invalidation: what would falsify it (one per line)").fill("OOS Sharpe below threshold");
  await page.getByLabel("train_bars").fill("400");
  await page.getByLabel("test_bars").fill("100");
  await page.getByLabel("embargo_bars").fill("10");
  await page.getByLabel("Holdout starts (final test, isolated)").fill("2024-01-01");
  await page.getByRole("button", { name: "Register hypothesis" }).click();
  await expect(page).toHaveURL(/\/research\/[0-9a-f]{8,}/);
  await expect(page.getByText("HYPOTHESIS_FORMULATED").first()).toBeVisible();
  await page.getByRole("button", { name: "Run validation" }).click();
  await expect(page.getByRole("link", { name: "report" })).toBeVisible({ timeout: 60_000 });
  await page.getByRole("link", { name: "report" }).click();
  await expect(page.getByRole("heading", { name: "Backtesting & Validation" })).toBeVisible();
  await expect(page.getByText("Gate D")).toBeVisible();
  await expect(page.getByText("strategy_causality")).toBeVisible();
});

test("tests page lists detectors and can queue a scan", async ({ page }) => {
  await page.goto("/tests");
  await page.getByLabel("select SYN-A").check();
  await page.getByRole("button", { name: "Run now" }).click();
  await expect(page.getByText("Scan queued")).toBeVisible();
});

import { expect, test } from "@playwright/test";

// Compare Isolation Forest with the return-shock baseline on one synthetic dataset, then read the comparison.
test("run a model comparison and inspect it", async ({ page }) => {
  test.setTimeout(120_000);
  await page.goto("/models");
  await expect(page.getByRole("heading", { name: "Model Lab" })).toBeVisible();
  await page.getByLabel("synthetic", { exact: true }).check();
  await page.getByLabel(/SYN-A 1d/).check();
  await page.getByLabel(/local_outlier_factor/).uncheck();
  await page.getByLabel(/volatility_regime/).uncheck();
  await page.getByLabel("Seeds").fill("0,1");
  await page.getByLabel("Label").fill("e2e comparison");
  await page.getByRole("button", { name: "Run comparison" }).click();
  await expect(page.getByText("Comparison queued")).toBeVisible();
  const row = page.locator("tbody tr", { hasText: "e2e comparison" });
  await expect(row.getByText("succeeded")).toBeVisible({ timeout: 90_000 });
  await row.getByRole("button", { name: "Open" }).click();
  await expect(page.locator("th", { hasText: "Out-of-sample evidence" })).toBeVisible();
  await expect(page.locator("th", { hasText: "Seed stability" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Export CSV" })).toBeVisible();
  await expect(page.getByText("Limitations", { exact: true })).toBeVisible();
  await page.getByText(/Feature catalog/).click();
  await expect(page.getByRole("cell", { name: "ret_robust_z" })).toBeVisible();
});

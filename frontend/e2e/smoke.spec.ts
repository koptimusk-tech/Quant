import { expect, test } from "@playwright/test";

// Requires: API with the synthetic demo loaded (`python -m qal.cli demo`) and `npm run dev`.
test("scanner lists synthetic anomalies and opens a reproduced detail", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Market Scanner" })).toBeVisible();
  const first = page.locator("tbody tr").first();
  await expect(first).toContainText("SYNTHETIC");
  await first.click();
  await expect(page.getByText("reproduced from stored data")).toBeVisible();
  await page.getByRole("link", { name: "Data Health" }).click();
  await expect(page.getByRole("heading", { name: "Data Health" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "synthetic", exact: true }).first()).toBeVisible();
});

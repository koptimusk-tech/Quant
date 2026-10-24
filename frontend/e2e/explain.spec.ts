import { expect, test } from "@playwright/test";

// Selecting an anomaly explains every score separately and lets the user run the predictive evaluation.
test("scores are explained and the predictive evaluation runs from the detail", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("thead").getByText("Investigation priority")).toBeVisible();
  await page.getByRole("button", { name: /Explain scores for/ }).first().click();
  await expect(page.getByLabel("Score explanations").first()).toContainText("Why it was detected");

  await page.selectOption('select[aria-label="Detector"]', "return_shock");
  await page.locator("tbody tr").first().click();
  const panel = page.getByLabel("Score explanations");
  for (const t of ["Statistical rarity", "Statistical significance", "Economic relevance", "Predictive capacity", "Investigation priority"]) {
    await expect(panel.getByRole("heading", { name: new RegExp(t) })).toBeVisible();
  }
  await expect(page.getByLabel("Economic score components").or(panel.getByText("No economic score"))).toBeVisible();
  await expect(page.getByLabel("Priority components")).toBeVisible();
  await page.getByRole("button", { name: /Run predictive evaluation|Re-run predictive evaluation/ }).click();
  await expect(page.getByLabel("Forward returns by horizon")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByLabel("Event study")).toContainText(/Insufficient sample|No evidence|In-sample only|Supported out of sample/);
});

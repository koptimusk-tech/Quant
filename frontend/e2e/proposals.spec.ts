import { expect, test } from "@playwright/test";

// The Research Lab suggests hypotheses from the loaded data; one click pre-fills the contract form.
test("use a suggested hypothesis and register it", async ({ page }) => {
  await page.goto("/research");
  const list = page.getByLabel("Suggested hypotheses");
  const item = list.locator("li").filter({ hasText: "SYN-C 1d: time-series momentum" });
  await expect(item).toContainText("SYNTHETIC");
  await item.getByRole("button", { name: "Use this proposal" }).click();
  await expect(page.getByText("Pre-filled from a suggested hypothesis")).toBeVisible();
  await expect(page.getByLabel("Title")).toHaveValue("SYNTHETIC SYN-C 1d: time-series momentum");
  await page.getByRole("button", { name: "Register hypothesis" }).click();
  // Registration waits for the database while other specs run long simulations in parallel.
  await expect(page).toHaveURL(/\/research\/[0-9a-f]{8,}/, { timeout: 20_000 });
  await expect(page.getByText("HYPOTHESIS_FORMULATED").first()).toBeVisible();
});

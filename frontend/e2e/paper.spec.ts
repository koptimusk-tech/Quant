import { expect, test } from "@playwright/test";

// Create a paper session with an unvalidated demo rule on synthetic data, replay bars, evaluate, kill switch.
test("paper trading session on synthetic data", async ({ page }) => {
  test.setTimeout(180_000);
  page.on("dialog", (d) => d.accept());
  await page.goto("/paper");
  await expect(page.getByText(/No order is sent to any broker/)).toBeVisible();
  await page.getByLabel("acting person").fill("e2e tester");
  await page.getByRole("button", { name: "New session" }).click();
  await page.getByLabel("demo strategy").selectOption("DEMO:ts_momentum");
  const syn = await page.getByLabel("demo datasets").locator("option", { hasText: /^SYN-A/ }).first().getAttribute("value");
  await page.getByLabel("demo datasets").selectOption(syn!);
  await page.getByLabel("allow unvalidated demo").check();
  await page.getByRole("button", { name: "Create session" }).click();
  await expect(page.getByText(/It is not real-time trading/)).toBeVisible();
  await expect(page.getByText("UNVALIDATED DEMO strategies included.")).toBeVisible();
  await page.getByRole("button", { name: "Start" }).click();
  await page.getByRole("button", { name: "Step 20 bars" }).click();
  await expect(page.getByText(/last bar/)).toBeVisible();
  await page.getByRole("button", { name: "Step 20 bars" }).click();
  await expect(page.getByRole("cell", { name: "rebalance" }).first()).toBeVisible({ timeout: 30_000 });
  await page.getByRole("button", { name: "Evaluate vs backtest" }).click();
  await page.getByRole("button", { name: "evaluation" }).click();
  await expect(page.getByText(/It is not an approval to trade/)).toBeVisible();
  await page.getByRole("button", { name: "Kill switch" }).click();
  await expect(page.getByText("HALTED").first()).toBeVisible();
  await page.getByRole("button", { name: "events" }).click();
  await expect(page.getByText(/kill switch activated by e2e tester/)).toBeVisible();
});

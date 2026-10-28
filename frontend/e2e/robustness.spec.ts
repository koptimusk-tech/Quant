import { expect, test } from "@playwright/test";

// Run one demo lab experiment, evaluate its robustness and read every section of the report.
test("evaluate a lab candidate in the Robustness Lab", async ({ page, request }) => {
  test.setTimeout(270_000);
  const gen = await (await request.post("/api/v1/lab/generate", { data: { families: ["momentum"], limit: 1 } })).json();
  const sub = await request.post("/api/v1/lab/experiments", { data: { spec: gen.proposed[0].spec, run_async: false } });
  expect(sub.ok()).toBeTruthy();
  const title = (await sub.json()).experiment.title as string;

  await page.goto("/robustness");
  await expect(page.getByRole("heading", { name: "Robustness Lab" })).toBeVisible();
  await expect(page.getByText(/not a forecast or a probability of success/)).toBeVisible();
  const row = page.locator("tbody tr", { hasText: title }).first();
  await row.getByRole("button", { name: "Evaluate" }).click();
  await expect(page.getByText(`robustness run of ${title}`)).toBeVisible();
  await expect(row.getByRole("button", { name: "View" })).toBeVisible({ timeout: 150_000 });
  await row.getByRole("button", { name: "View" }).click();
  // The run detail is large and other specs share the server (CPU-heavy simulations run in parallel): wait for it.
  await expect(page.getByText("1 · Assets and markets")).toBeVisible({ timeout: 90_000 });
  for (const h of ["1 · Assets and markets", "2 · Walk-forward windows, years and regimes", "4 · Costs and execution: base, adverse, severe",
    "6 · Stress tests", "8 · Incremental portfolio contribution", "10 · Limitations and missing data"]) {
    await expect(page.getByText(h)).toBeVisible();
  }
  await expect(page.getByText(/3 · Parameter sensitivity \(\d+ configurations\)/)).toBeVisible();
  await expect(page.getByText(/7 · Capacity: (APPROXIMATE|UNKNOWN)/)).toBeVisible();
  await expect(page.getByText("Synthetic data: these results test the procedure, not markets.", { exact: false })).toBeVisible();
  await expect(page.getByRole("link", { name: "Matrix CSV" })).toBeVisible();
  const matrix = page.locator("section", { hasText: "9 · Robustness matrix" });
  await expect(matrix.getByRole("button", { name: title })).toBeVisible();
});

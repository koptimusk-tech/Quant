import { expect, test } from "@playwright/test";

// Vertical slice through the UI with a non-synthetic file: load CSV -> validate -> scan -> list -> detail.
function csv(): string {
  const rows = ["date,open,high,low,close,volume"];
  let px = 100;
  let seed = 42;
  const rand = () => ((seed = (seed * 16807) % 2147483647) / 2147483647 - 0.5) * 0.02;
  for (let i = 0; i < 600; i++) {
    const o = px;
    px = px * Math.exp(rand() + (i === 450 ? 0.15 : 0));
    const d = new Date(Date.UTC(2020, 0, 1 + i)).toISOString().slice(0, 10);
    rows.push(`${d},${o.toFixed(4)},${(Math.max(o, px) * 1.002).toFixed(4)},${(Math.min(o, px) * 0.998).toFixed(4)},${px.toFixed(4)},${1000 + (i % 50)}`);
  }
  return rows.join("\n");
}

test("load a CSV in Data Health, scan it and open the anomaly", async ({ page, request }) => {
  const symbol = `CSV${Date.now() % 100000}`;
  await page.goto("/data");
  await page.getByLabel("Data file").setInputFiles({ name: "prices.csv", mimeType: "text/csv", buffer: Buffer.from(csv()) });
  await page.getByLabel("Upload symbol").fill(symbol);
  await page.getByRole("button", { name: "Load file" }).click();
  await expect(page.getByText(new RegExp(`Loaded ${symbol}: 600 bars`))).toBeVisible();

  // Scan only this dataset (the Tests page does the same through POST /scans).
  const datasets = await (await request.get("/api/v1/datasets?latest_only=true")).json();
  const ds = datasets.find((d: { symbol: string }) => d.symbol === symbol);
  const scan = await request.post("/api/v1/scans", { data: { dataset_ids: [ds.id], detectors: { return_shock: {} } } });
  expect(scan.ok()).toBeTruthy();

  await page.goto("/");
  await page.getByPlaceholder("symbol, type, detector").fill(symbol);
  const row = page.locator("tbody tr", { hasText: symbol }).filter({ hasText: "2021-03-27" });
  await expect(row).toBeVisible();
  await expect(row).not.toContainText("SYNTHETIC");
  await row.click();
  const summary = page.getByLabel("Detection summary");
  await expect(summary).toContainText(symbol);
  await expect(summary).toContainText("robust σ");
  await expect(page.getByText("reproduced from stored data")).toBeVisible();
});

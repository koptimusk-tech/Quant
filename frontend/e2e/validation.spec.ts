import { expect, test } from "@playwright/test";

// Admit a demo lab experiment as an exploratory candidate, freeze a protocol, run the program and read its sections.
test("run a paper validation program on synthetic data", async ({ page, request }) => {
  test.setTimeout(180_000);
  // Other specs load real (non-synthetic) files into the same database: pick a proposal on synthetic data only,
  // because synthetic candidates run in mode C.
  const datasets: { id: string; is_synthetic: boolean }[] = await (await request.get("/api/v1/datasets")).json();
  const synthetic = new Set(datasets.filter((d) => d.is_synthetic).map((d) => d.id));
  const gen = await (await request.post("/api/v1/lab/generate", { data: { families: ["momentum"], limit: 50 } })).json();
  const pick = gen.proposed.find((p: { spec: { contract: { dataset_ids: string[] } } }) => p.spec.contract.dataset_ids.every((d) => synthetic.has(d)));
  expect(pick).toBeTruthy();
  const sub = await request.post("/api/v1/lab/experiments", { data: { spec: pick.spec, run_async: false } });
  expect(sub.ok()).toBeTruthy();
  const exp = (await sub.json()).experiment;
  const cand = await request.post("/api/v1/program/candidates", { data: {
    name: `e2e ${exp.title}`, lab_experiment_id: exp.id, admitted_by: "e2e tester", exploratory: true,
    justification: "End-to-end test of the procedure on synthetic data; not validation evidence." } });
  expect(cand.status()).toBe(201);
  const cid = (await cand.json()).id;
  const proto = await request.post("/api/v1/program/protocols", { data: {
    candidate_id: cid, mode: "C", created_by: "e2e tester", rationale: "Short horizon chosen before the run for the test.",
    min_trades: 3, target_sharpe_se: 3.0, min_observations_floor: 20 } });
  expect(proto.status(), await proto.text()).toBe(201);

  await page.goto("/validation");
  await expect(page.getByRole("heading", { name: "Paper Trading Validation" })).toBeVisible();
  await expect(page.getByText(/no order reaches any broker/)).toBeVisible();
  const prow = page.locator("tbody tr", { hasText: `e2e ${exp.title}` }).filter({ hasText: "hash ok" }).last();
  await page.getByLabel("Created by (a person)").fill("e2e tester");
  await prow.getByRole("button", { name: "Start program" }).click();
  await expect(page.getByText(/3 · Session status/)).toBeVisible();
  await page.getByRole("button", { name: "Process bars" }).click();
  await expect(page.getByText(/Excess over the baseline per bar/)).toBeVisible({ timeout: 120_000 });
  await expect(page.getByText(/5 · P&L and risk \(SYNTHETIC SIMULATION/)).toBeVisible();
  for (const h of ["4 · Mode and data feed", "6 · Comparison with the backtest", "7 · Costs and execution quality", "8 · Alerts and pauses",
    "9 · Versions and history", "10 · Final report"]) {
    await expect(page.getByText(h)).toBeVisible();
  }
  await expect(page.getByText(/It does not authorise real trading/)).toBeVisible({ timeout: 60_000 });
  await expect(page.getByRole("link", { name: "Export JSON" })).toBeVisible();
});

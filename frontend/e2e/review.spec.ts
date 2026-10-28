import { expect, test } from "@playwright/test";

// Run a synthetic validation program through the API, review it in the page and read the decision and its evidence.
test("review a finished paper validation program", async ({ page, request }) => {
  test.setTimeout(240_000);
  const datasets: { id: string; is_synthetic: boolean }[] = await (await request.get("/api/v1/datasets")).json();
  const synthetic = new Set(datasets.filter((d) => d.is_synthetic).map((d) => d.id));
  const gen = await (await request.post("/api/v1/lab/generate", { data: { families: ["momentum"], limit: 50 } })).json();
  const pick = gen.proposed.find((p: { spec: { contract: { dataset_ids: string[] } } }) => p.spec.contract.dataset_ids.every((d) => synthetic.has(d)));
  expect(pick).toBeTruthy();
  const exp = (await (await request.post("/api/v1/lab/experiments", { data: { spec: pick.spec, run_async: false } })).json()).experiment;
  const name = `e2e review ${exp.id.slice(0, 6)}`;
  const cand = await request.post("/api/v1/program/candidates", { data: {
    name, lab_experiment_id: exp.id, admitted_by: "e2e tester", exploratory: true,
    justification: "End-to-end test of the review on synthetic data; not validation evidence." } });
  expect(cand.status(), await cand.text()).toBe(201);
  const proto = await request.post("/api/v1/program/protocols", { data: {
    candidate_id: (await cand.json()).id, mode: "C", created_by: "e2e tester", rationale: "Short horizon chosen before the run for the test.",
    min_trades: 3, target_sharpe_se: 3.0, min_observations_floor: 20 } });
  expect(proto.status(), await proto.text()).toBe(201);
  const prog = await (await request.post("/api/v1/program/programs", { data: { protocol_id: (await proto.json()).id, started_by: "e2e tester" } })).json();
  const ran = await request.post(`/api/v1/program/programs/${prog.id}/run`, { data: { max_bars: 5000, run_async: false }, timeout: 120_000 });
  expect(ran.ok(), await ran.text()).toBeTruthy();

  await page.goto("/review");
  await expect(page.getByRole("heading", { name: "Strategy Review & Readiness" })).toBeVisible();
  await expect(page.getByText(/no state, including/i)).toBeVisible();
  await page.getByLabel("Run by").fill("e2e tester");
  await page.getByRole("button", { name: "Run review" }).click();
  await page.getByLabel("Strategy filter").fill(name);
  const row = page.locator("tbody tr", { hasText: name });
  await expect(row).toBeVisible({ timeout: 120_000 });
  await expect(row.getByText("SYNTHETIC · not market evidence")).toBeVisible();
  await row.getByRole("button", { name: "Open" }).click();
  for (const h of ["Decision and justification", "Quality and integrity of the results", "Comparison with the backtest",
    "Acceptance criteria (fixed before the start)", "Decision history"]) {
    await expect(page.getByRole("heading", { name: h })).toBeVisible({ timeout: 30_000 });
  }
  await expect(page.getByText("What we cannot conclude")).toBeVisible();
  await expect(page.getByText(/will be profitable in the future/)).toBeVisible();
  await expect(page.getByRole("link", { name: "Report Markdown" })).toBeVisible();
});

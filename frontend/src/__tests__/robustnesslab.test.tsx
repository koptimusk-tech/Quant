import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import RobustnessLabPage from "../pages/RobustnessLab";

const dims = ["temporal", "cross_asset", "parameters", "costs", "stress", "uncertainty", "capacity", "diversification"];
const meta = {
  version: "robustness@1.0.0", states: ["NOT_EVALUATED", "INSUFFICIENT_DATA", "FRAGILE", "MIXED_EVIDENCE", "ROBUST_UNDER_TESTED_ASSUMPTIONS"],
  dimensions: dims, essential: ["temporal", "cross_asset", "parameters", "costs", "uncertainty"], core_fragility: ["temporal", "parameters", "costs"],
  criteria: Object.fromEntries(dims.map((d) => [d, `criterion of ${d}`])), rules: "Overall state (first rule that applies)",
  execution_scenarios: {}, defaults: {}, not_a_guarantee: "Not a probability of future success.", live_trading_enabled: false,
};
const cands = [
  { experiment_id: "e1", title: "QQQ 1d: time-series momentum", family: "momentum", strategy: "ts_momentum", universe: ["QQQ"], asset_class: "etf",
    markets: ["etf"], timeframe: "1d", period: ["2010-01-04", "2026-10-23"], is_synthetic: false, decision: "INCONCLUSIVE", adjusted_p: 0.968,
    oos_sharpe_net: 0.35, oos_bars: 2028, n_trades: 57, exploratory: true, robustness_run_id: "r1", robustness_state: "FRAGILE" },
  { experiment_id: "e2", title: "BTC 1h: mean reversion", family: "mean_reversion", strategy: "mean_reversion", universe: ["BTC-USD"], asset_class: "crypto",
    markets: ["crypto"], timeframe: "1h", period: ["2024-01-01", "2026-10-23"], is_synthetic: false, decision: "REJECTED", adjusted_p: 0.9,
    oos_sharpe_net: -0.2, oos_bars: 900, n_trades: 80, exploratory: true, robustness_run_id: null, robustness_state: "NOT_EVALUATED" },
];
const dimStatus = { temporal: "FAIL", cross_asset: "INSUFFICIENT_DATA", parameters: "PASS", costs: "FAIL", stress: "PASS", uncertainty: "INSUFFICIENT_DATA", capacity: "PASS", diversification: "INSUFFICIENT_DATA" };
const summary = { id: "r1", status: "succeeded", created_at: "2026-10-25T14:00:00Z", finished_at: "2026-10-25T14:01:00Z", experiment_id: "e1",
  title: "QQQ 1d: time-series momentum", family: "momentum", strategy: "ts_momentum", state: "FRAGILE", state_reasons: ["temporal: positive windows 0.4"],
  duration_s: 12, error: null, dimensions: dimStatus, exploratory: true, is_synthetic: false, universe: ["QQQ"], asset_class: "etf", timeframe: "1d", decision: "INCONCLUSIVE" };
const m = (x: number) => ({ bars: 500, mean_net: x, sharpe_net: x * 1000, total_return_net: x * 100, max_drawdown: -0.2 });
const run = {
  ...summary, config: {}, data_refs: [], code_version: "c1", algorithm_version: "robustness@1.0.0", config_hash: "k",
  results: {
    state: "FRAGILE", state_reasons: summary.state_reasons, note: "States describe the tests run; they are not a forecast and they promote nothing.",
    lab: { is_synthetic: false, exploratory: true, decision: "INCONCLUSIVE", universe: ["QQQ"], timeframe: "1d" },
    lab_statistics: { dsr: 0.68 },
    dimensions: Object.fromEntries(dims.map((d) => [d, { status: (dimStatus as Record<string, string>)[d], reason: `reason of ${d}` }])),
    cross_asset: { n_effective: 1.4, note: "Target costs are asset-class defaults (estimates).", rows: [
      { category: "design", symbol: "QQQ", asset_class: "etf", market: "", timeframe: "1d", period: ["2010-01-04", "2026-10-23"], provider: "yahoo", quality_status: "pass", costs: {}, cost_source: "asset-class default (estimate)", status: "evaluated", sample: "sufficient", bars: 2028, n_trades: 57, sharpe_gross: 0.5, sharpe_net: 0.35, total_return_net: 0.4, corr_with_design: 1 },
      { category: "same_market", symbol: "SPY", asset_class: "etf", market: "", timeframe: "1d", period: ["2010-01-04", "2026-10-23"], provider: "yahoo", quality_status: "pass", costs: {}, cost_source: "asset-class default (estimate)", status: "evaluated", sample: "sufficient", bars: 2028, n_trades: 60, sharpe_gross: 0.1, sharpe_net: -0.1, total_return_net: -0.05, corr_with_design: 0.92, near_duplicate_of_design: true, transfer_fixed_params: { sharpe_net: -0.2 } },
    ] },
    temporal: { windows: [
      { test_from: "2015-01-02", test_to: "2015-06-30", chosen_params: { lookback: 60 }, mean_net: -0.0002, sharpe_net: -0.3, bars: 120, sharpe_se: 0.9 },
      { test_from: "2022-01-03", test_to: "2022-06-30", chosen_params: { lookback: 120 }, mean_net: 0.0004, sharpe_net: 0.6, bars: 120, sharpe_se: 0.9 },
    ], years: [{ year: 2015, ...m(-0.0001) }, { year: 2022, ...m(0.0003) }], regimes: { volatility: { high_vol: m(-0.0001), low_vol: m(0.0002) } },
      training_length: [], positive_window_frac: 0.4, positive_year_frac: 0.5, regime_dependence: ["volatility=high_vol: mean net -1e-4 over 300 bars"], note: "Every predefined window is shown." },
    parameters: { n_configs: 3, neighbour_positive_frac: 0.5, neighbour_sharpe_median: 0.2, centre_sharpe: 0.35, note: "diagnostics only",
      rows: [{ config: "centre (most often selected)", params: { lookback: 60 }, ...m(0.0002) }, { config: "lookback x0.75", params: { lookback: 45 }, ...m(-0.0001) }, { config: "lookback x1.25", params: { lookback: 75 }, ...m(0.0001) }] },
    execution: { scenarios: { base: { cost_multiplier: 1, delay_bars: 0, participation_factor: 1, gross_mean: 0.0003, cost_mean: 0.00005, partial_fill_events: 0, ...m(0.0002) },
      adverse: { cost_multiplier: 2, delay_bars: 1, participation_factor: 0.5, gross_mean: 0.0002, cost_mean: 0.0001, partial_fill_events: 1, ...m(-0.00001) },
      severe: { cost_multiplier: 3, delay_bars: 2, participation_factor: 0.25, gross_mean: 0.0001, cost_mean: 0.00015, partial_fill_events: 3, ...m(-0.0001) } },
      curves: { ts: ["2015-01-02", "2015-01-05"], base: [1, 1.01], adverse: [1, 1.0], severe: [1, 0.99] },
      cost_sources: ["QQQ: asset-class defaults (estimates) (commission 1 bps)"], not_modelled: ["historical spreads (not in OHLCV)"], note: "Costs are assumptions." },
    capacity: { status: "APPROXIMATE", estimate: 1000000, rule: "largest tested capital keeping half the edge", note: "not a measurement of real market impact.",
      assumptions: { impact_model: "square root (assumed)" }, levels: [{ capital: 1000000, mean_net_after_impact: 0.00019, sharpe_after_impact: 0.3, participation_p95: 0.01, trades_over_limit_frac: 0, notional_in_lowest_liquidity_quintile: 0.2 }] },
    stress: { note: "Synthetic scenarios are stress tests, not predictions.",
      synthetic: [{ scenario: "base", description: "unperturbed", ...m(0.0002) }, { scenario: "price_gaps", description: "five -8% gaps", ...m(-0.0003) }, { scenario: "correlation_spike", description: "not applicable: single instrument", not_applicable: true }],
      historical: [{ from: "2020-02-19", to: "2020-03-23", reference_drawdown: -0.3, candidate_return: -0.05, bars: 23 }],
      source_loss: [{ not_applicable: "single instrument or fixed pair" }],
      risk_controls: [{ check: "price missing blocks simulated orders", passed: true, evidence: "check_bar returns fail" }] },
    diversification: { status_hint: "no_peers", reason: "no other completed, non-rejected lab experiment to combine with" },
    limitations: ["Exploratory: the lab decision is INCONCLUSIVE, below OOS_SUPPORTED.", "Costs of at least one asset are asset-class defaults (estimates), not observed costs."],
  },
};

function mockFetch() {
  const calls: { url: string; method: string; body?: string }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    calls.push({ url, method, body: init?.body as string | undefined });
    let body: unknown = [];
    if (url === "/api/v1/robustness/meta") body = meta;
    else if (url === "/api/v1/robustness/candidates") body = cands;
    else if (url === "/api/v1/robustness/runs" && method === "POST") body = { run: null, job: { id: "j1", kind: "robustness_run", status: "queued", attempts: 0, max_attempts: 3, error: null, result: {}, schedule_id: null, created_at: "", finished_at: null } };
    else if (url === "/api/v1/robustness/runs") body = [summary];
    else if (url === "/api/v1/robustness/runs/r1") body = run;
    else if (url === "/api/v1/jobs/j1/cancel") body = { id: "j1", kind: "robustness_run", status: "cancelled", attempts: 0, max_attempts: 3, error: null, result: {}, schedule_id: null, created_at: "", finished_at: null };
    return new Response(JSON.stringify(body), { status: 200 });
  }));
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe("Robustness Lab", () => {
  it("shows candidates, the robustness matrix and every section of a run with its evidence and limitations", async () => {
    mockFetch();
    render(<MemoryRouter><RobustnessLabPage /></MemoryRouter>);
    expect(await screen.findByText(/not a forecast or a probability of success/)).toBeInTheDocument();
    expect(await screen.findByText("BTC 1h: mean reversion")).toBeInTheDocument();
    expect(screen.getAllByText("exploratory").length).toBe(2);
    const matrix = screen.getByText(/9 · Robustness matrix/).closest("section")!;
    expect(within(matrix).getByText("FRAGILE")).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "View" })[0]);
    expect(await screen.findByText("1 · Assets and markets")).toBeInTheDocument();
    for (const h of ["2 · Walk-forward windows, years and regimes", "3 · Parameter sensitivity (3 configurations)", "4 · Costs and execution: base, adverse, severe",
      "5 · Net equity by scenario (walk-forward test windows)", "6 · Stress tests", "7 · Capacity: APPROXIMATE ≈ 1.000e+6", "8 · Incremental portfolio contribution",
      "10 · Limitations and missing data"]) expect(screen.getByText(h)).toBeInTheDocument();
    expect(screen.getByText("near-duplicate")).toBeInTheDocument();
    expect(screen.getByText(/Effective independent assets: 1.4/)).toBeInTheDocument();
    expect(screen.getByText(/loses in volatility=high_vol/)).toBeInTheDocument();
    expect(screen.getByText(/Synthetic scenarios are stress tests, not predictions\./)).toBeInTheDocument();
    expect(screen.getByText("blocked")).toBeInTheDocument();
    expect(screen.getByText(/not a measurement of real market impact/)).toBeInTheDocument();
    expect(screen.getByText(/no other completed, non-rejected/)).toBeInTheDocument();
    expect(screen.getByText(/asset-class defaults \(estimates\), not observed costs/)).toBeInTheDocument();
    expect(screen.getByText("reason of temporal")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Matrix CSV" })).toHaveAttribute("href", "/api/v1/robustness/runs/r1/matrix.csv");
    // unfavourable windows are shown next to favourable ones
    expect(screen.getByText("2015-01-02 → 2015-06-30")).toBeInTheDocument();
    expect(screen.getByText("2022-01-03 → 2022-06-30")).toBeInTheDocument();
  });

  it("filters by strategy, market, timeframe, asset and period", async () => {
    mockFetch();
    render(<MemoryRouter><RobustnessLabPage /></MemoryRouter>);
    await screen.findByText("BTC 1h: mean reversion");
    fireEvent.change(screen.getByLabelText("Market"), { target: { value: "etf" } });
    expect(screen.queryByText("BTC 1h: mean reversion")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Market"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Timeframe"), { target: { value: "1h" } });
    expect(screen.queryAllByText("QQQ 1d: time-series momentum").length).toBe(0);
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    fireEvent.change(screen.getByLabelText("Strategy"), { target: { value: "ts_momentum" } });
    expect(screen.queryByText("BTC 1h: mean reversion")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    fireEvent.change(screen.getByLabelText("Asset"), { target: { value: "btc" } });
    expect(screen.queryAllByText("QQQ 1d: time-series momentum").length).toBe(0);
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-11-01" } });
    expect(screen.queryByText("BTC 1h: mean reversion")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    // the period filter also narrows the windows of an open run
    fireEvent.click(screen.getAllByRole("button", { name: "View" })[0]);
    await screen.findByText("1 · Assets and markets");
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2020-01-01" } });
    expect(screen.queryByText("2015-01-02 → 2015-06-30")).not.toBeInTheDocument();
    expect(screen.getByText("2022-01-03 → 2022-06-30")).toBeInTheDocument();
  });

  it("queues an evaluation and cancels it", async () => {
    const calls = mockFetch();
    render(<MemoryRouter><RobustnessLabPage /></MemoryRouter>);
    await screen.findByText("BTC 1h: mean reversion");
    const row = screen.getByText("BTC 1h: mean reversion").closest("tr")!;
    fireEvent.click(within(row).getByRole("button", { name: "Evaluate" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/v1/robustness/runs")).toBe(true));
    const body = JSON.parse(calls.find((c) => c.method === "POST" && c.url === "/api/v1/robustness/runs")!.body!);
    expect(body).toEqual({ run_async: true, experiment_id: "e2" });
    expect(await screen.findByText(/robustness run of BTC 1h/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/v1/jobs/j1/cancel")).toBe(true));
    expect(await screen.findByText("cancelled")).toBeInTheDocument();
  });
});

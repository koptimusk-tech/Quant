import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import ExperimentLabPage from "../pages/ExperimentLab";

const meta = {
  spec_version: "lab-exp@1", pipeline_version: "lab-pipeline@1.0.0", selection_version: "lab-selection@1.0.0", generator_version: "lab-generator@1.0.0",
  families: { momentum: { strategies: ["ts_momentum"], label: "Momentum" }, pairs: { strategies: ["pair_mean_reversion"], label: "Pairs" } },
  baselines: ["no_trade"], default_baselines: {}, decisions: ["REJECTED", "INCONCLUSIVE", "NEEDS_RESEARCH", "OOS_SUPPORTED", "PAPER_ELIGIBLE"],
  weights: { methodology: 0.15 }, states: [], min_oos_bars: 120, min_independent_trades: 20,
  not_a_guarantee: "They are not a forecast or a guarantee of future returns, and they approve nothing.", live_trading_enabled: false,
};
const summary = {
  id: "e1", status: "COMPLETED", title: "Momentum SPY", family: "momentum", strategy: "ts_momentum", universe: ["SPY"], is_synthetic: false,
  variants: 3, generated_by: "manual", attempts: 1, stage: null, created_at: "2026-10-25T07:00:00Z", finished_at: null, duration_s: 0.8,
  error: null, decision_at_completion: "INCONCLUSIVE", score_at_completion: 0.6, warnings: [], oos_sharpe_net: 0.4, oos_bars: 600,
  invalid_checks: null, spec_hash: "abc",
};
const running = { ...summary, id: "e2", status: "RUNNING", stage: "D_robustness", title: "Pairs SYN", is_synthetic: true, decision_at_completion: null };
const decision = {
  decision: "INCONCLUSIVE", version: "lab-selection@1.0.0", score: 0.6, weights: { methodology: 0.15 },
  components: [{ name: "methodology", value: 0.8, source: "share of checks passed" }],
  reasons: ["insufficient sample: 10 trades (minimum 30)"],
  warnings: ["Small sample: 600 out-of-sample bars and 10 trades.", "Final test reused: access number 2; it is not an independent test."],
  conditions: { sample: [{ condition: "independent_trades", met: false, evaluable: true, detail: "10 trades (minimum 30)" }] },
  note: "approve nothing",
};
const detail = {
  ...summary, config: { spec: { family: "momentum" } }, data_refs: [{ symbol: "SPY", frequency: "1d", rows: 2000, quality_status: "pass", is_synthetic: false, content_hash: "h1" }],
  code_version: "c1", algorithm_version: "lab-pipeline@1.0.0+lab-exp@1", config_hash: "k1", seed: 42, job: null,
  lineage: [{ id: "e0", created_at: "2026-10-24", status: "COMPLETED", spec_hash: "x", code_version: "c1", algorithm_version: "a", decision_at_completion: "REJECTED", changed_from_previous: [], previous_attempt: null, current: false },
    { id: "e1", created_at: "2026-10-25", status: "COMPLETED", spec_hash: "abc", code_version: "c1", algorithm_version: "a", decision_at_completion: "INCONCLUSIVE", changed_from_previous: ["contract.costs.d1.commission_bps"], previous_attempt: null, current: true }],
  results: {
    attempts: 1,
    pipeline: {
      decision_at_completion: decision,
      A_integrity: { checks: [{ check: "leakage:feature_availability", passed: true, critical: true, detail: "ok" }] },
      B_baselines: [{ baseline: "buy_and_hold", how: "hold the instrument", beaten: false, diff_mean_net: -0.0001, metrics: { mean_net: 0.0003 } }],
      C_temporal: { research_bars: 1500, oos_bars: 600, embargo_bars: 5, holdout_start: "2024-01-01", holdout_bars: 300, windows: [], in_sample_grid: [{ params: { lookback: 20 }, sharpe_net_in_sample: 1.2 }],
        holdout: { independent: false, access_number: 2, note: "This final period was examined before", metrics: { sharpe_net: 0.1 } } },
      D_robustness: { breaks_under: [{ multiplier: 3, mean_net: -0.0001 }], costs: [], delays: [], training_length: [], parameters: { grid: [], positive_frac: 0.67 }, regimes: {}, years: [], universe_subsets: [] },
      E_statistics: { oos: { bars: 600 }, extremes: {}, assumptions: [] },
    },
  },
};
const ranking = {
  version: "lab-selection@1.0.0", n_experiments: 1, n_trials_total: 3, multiple_testing: "Benjamini-Hochberg over 1 completed lab experiments", note: "",
  rows: [{ rank: 1, experiment_id: "e1", title: "Momentum SPY", family: "momentum", strategy: "ts_momentum", universe: ["SPY"], is_synthetic: false,
    decision: "INCONCLUSIVE", score: 0.6, reasons: ["insufficient sample"], warnings: ["Cost data missing: asset-class default costs (estimates) were used for SPY."],
    components: [], adjusted_p: 0.08, raw_p: 0.04, oos_sharpe_net: 0.4, oos_mean_net: 0.0001, oos_bars: 600, n_trades: 10, dsr: 0.7, n_trials: 3, variants: 3, baselines_beaten: ["no_trade"] }],
};
const generated = {
  version: "lab-generator@1.0.0", n_specs: 1, n_variants: 3, note: "Research proposals from deterministic rules, not evidence.", skipped: [],
  proposed: [{ generator_key: "g1", family: "momentum", title: "SYNTHETIC SYN-A 1d: time-series momentum", why: "trend", variants: 3, baselines: ["no_trade"], is_synthetic: true, spec: { family: "momentum" } }],
};

function mockFetch() {
  const calls: { url: string; method: string; body?: string }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    calls.push({ url, method, body: init?.body as string | undefined });
    let body: unknown = [];
    if (url === "/api/v1/lab/meta") body = meta;
    else if (url === "/api/v1/lab/experiments" && method === "POST") body = { experiment: summary, created: true, job: { id: "j1" } };
    else if (url === "/api/v1/lab/experiments") body = [running, summary];
    else if (url === "/api/v1/lab/experiments/e1") body = detail;
    else if (url === "/api/v1/lab/experiments/e2/cancel") body = { ...running, status: "RUNNING" };
    else if (url.startsWith("/api/v1/lab/ranking")) body = ranking;
    else if (url === "/api/v1/lab/generate") body = generated;
    else if (url === "/api/v1/lab/selections") body = { id: "s1234567", n_experiments: 1 };
    return new Response(JSON.stringify(body), { status: 200 });
  }));
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe("Experiment Lab", () => {
  it("shows the queue with progress, the ranking with warnings and the metrics behind a decision", async () => {
    mockFetch();
    render(<MemoryRouter><ExperimentLabPage /></MemoryRouter>);
    expect(await screen.findByText("D_robustness")).toBeInTheDocument(); // stage of the running experiment
    expect(await screen.findByText(/Cost data missing/)).toBeInTheDocument();
    expect(screen.getByText(/approve nothing/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Export ranking CSV" })).toHaveAttribute("href", "/api/v1/lab/ranking/export.csv?include_synthetic=true");
    fireEvent.click(screen.getByRole("button", { name: "Metrics" }));
    expect(await screen.findByText(/insufficient sample: 10 trades/)).toBeInTheDocument();
    expect(screen.getByText(/Small sample: 600/)).toBeInTheDocument();
    expect(screen.getByText(/Final test reused/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Export JSON" })).toHaveAttribute("href", "/api/v1/lab/experiments/e1/export.json");
    fireEvent.click(screen.getByRole("tab", { name: "B Baselines" }));
    expect(screen.getByText("buy_and_hold")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "C Temporal (IS vs OOS)" }));
    expect(screen.getByText(/examined before/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "D Robustness" }));
    expect(screen.getByText("Breaks under")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Versions" }));
    expect(screen.getByText("contract.costs.d1.commission_bps")).toBeInTheDocument();
    expect(screen.getByText("this one")).toBeInTheDocument();
  });

  it("generates proposals, queues the picked ones, cancels a running run and records a selection", async () => {
    const calls = mockFetch();
    render(<MemoryRouter><ExperimentLabPage /></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: "Generate proposals" }));
    expect(await screen.findByText(/not evidence/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Queue 1 selected \(3 variants\)/ }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/v1/lab/experiments")).toBe(true));
    const body = JSON.parse(calls.find((c) => c.method === "POST" && c.url === "/api/v1/lab/experiments")!.body!);
    expect(body).toEqual({ spec: { family: "momentum" }, generator_key: "g1", run_async: true });
    const row = (await screen.findByText("Pairs SYN")).closest("tr")!;
    fireEvent.click(within(row).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/v1/lab/experiments/e2/cancel")).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "Record selection" }));
    expect(await screen.findByText(/Nothing was promoted or allocated/)).toBeInTheDocument();
  });
});

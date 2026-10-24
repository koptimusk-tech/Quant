import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import PortfolioPage from "../pages/Portfolio";

const metrics = { sharpe_net: 0.8, volatility_ann: 0.06, max_drawdown: -0.12, total_return_net: 0.3, avg_leverage: 1.1, rebalance_cost_total: 0.001 };
const method = {
  description: "risk contributions matched to budgets", metrics, stability: { rebalances: 90, mean_abs_change: 0.05 },
  segments: { first: { sharpe_net: 0.9 }, last: { sharpe_net: 0.6 }, note: "" }, blocked: { max_weight: 12 },
  curve: [{ ts: "2016-01-04", equity: 1, drawdown: 0 }, { ts: "2017-01-04", equity: 1.1, drawdown: -0.02 }],
};
const run = {
  id: "r1", status: "succeeded", created_at: "2026-10-24T10:00:00Z", label: "", strategies: ["SYN:a", "SYN:b"], methods: ["risk_budget"],
  policy_version: "default@v1#abcd1234", is_synthetic: true, sharpe_net: 0.8, max_drawdown: -0.12, duration_s: 5, error: null, config: {},
  results: {
    is_synthetic: true, synthetic_note: "SYNTHETIC: generated from a seeded factor model", policy_version: "default@v1#abcd1234",
    primary_method: "risk_budget", strategies: [{ id: "SYN:a", name: "A", is_synthetic: true }, { id: "SYN:b", name: "B", is_synthetic: true }],
    rejected: [{ id: "SYN:weak", reasons: ["OOS net Sharpe -0.3 < 0.0"] }], period: { start: "2015-01-01", end: "2022-08-31", days: 2000 },
    methods: { risk_budget: method }, primary: { metrics, n_rebalances: 90, weights_history: [] },
    benchmarks: { naive_equal_weight: metrics, strategies: {} },
    cost_sensitivity: [{ multiplier: 0, sharpe_net: 0.9, total_return_net: 0.35 }, { multiplier: 2, sharpe_net: 0.6, total_return_net: 0.2 }],
    risk: {
      as_of: "2022-08-31", basis: "estimate", weights: { "SYN:a": 0.4, "SYN:b": 0.3 }, ex_ante_vol: 0.07,
      risk_contributions: { "SYN:a": 0.6, "SYN:b": 0.4 }, marginal_risk: { "SYN:a": 0.1, "SYN:b": 0.09 },
      diversification: { diversification_ratio: 1.3, effective_number_of_bets: 1.8 },
      exposures: { instrument: { "SYN-EURUSD": 0.4 }, currency_net: { USD: -0.7, EUR: 0.4 }, market_gross: { fx: 0.7 }, gross: 0.7, net: 0.1 },
      liquidity: [{ instrument: "SYN-EURUSD", exposure: 0.4, capacity_capital: null, note: "no usable traded volume" }],
      marginal_analysis: [{ strategy: "SYN:b", corr_with_rest: 0.92, vol_with: 0.07, vol_without: 0.06, enb_with: 1.2, enb_without: 1.2, redundant: true, adds_risk_without_diversifying: true, note: "redundant: correlation 0.92 with SYN:a" }],
    },
    historical_risk: { basis: "historical", drawdown: {}, realised_vol: 0.06,
      correlations: { strategies: ["SYN:a", "SYN:b"], observations: 1900, static: [[1, 0.9], [0.9, 1]], static_mean_pairwise: 0.9,
        rolling: { min: 0.5, max: 0.98 }, regime: { high_vol: { mean_pairwise: 0.95 }, low_vol: { mean_pairwise: 0.8 }, rule: "causal" },
        stress: { matrix: [[1, 0.97], [0.97, 1]], mean_pairwise: 0.97, days: 200, rule: "top 10%", note: "Future stress can be worse" } } },
    stress: { disclaimer: "They are not forecasts", historical: [{ name: "worst 5-day window", kind: "historical", pnl: -0.03, start: "2020-05-22", end: "2020-05-28" }],
      hypothetical: [{ name: "diversification disappears (all correlations 0.9)", kind: "hypothetical", pnl: -0.014 }],
      monte_carlo: [{ kind: "synthetic", name: "stationary block bootstrap", paths: 2000, horizon_days: 252, daily: { var95: -0.005, var99: -0.009, es99: -0.012 },
        horizon: { p01: -0.1, p05: -0.05, median: 0.04 }, max_drawdown: { median: -0.05, p99_worst: -0.2 }, note: "Not a forecast." }] },
    leakage_checks: [{ check: "truncation_invariance", passed: true, description: "Weights decided before a date do not change" }],
    notes: ["Research only."], decisions_stored: 4000,
  },
};
const policy = {
  id: "p1", name: "default", version: 1, label: "default@v1#abcd1234", created_by: "system", active: true, note: "", created_at: "2026-10-24T09:00:00Z",
  limits: { strategy: { max_weight: 0.35, max_drawdown: 0.25 }, group: { max_currency_net: 0.6, max_instrument_gross: 0.6 },
    portfolio: { max_leverage: 2, target_vol: 0.08, max_ex_ante_vol: 0.12, max_daily_loss: 0.03, max_drawdown: 0.15 } },
};
const strategies = [
  { id: "SYN:a", version: 1, name: "SYN trend", source: "synthetic", universe: ["SYN-EURUSD"], frequency: "1d", group: "fx", counterparty: "x",
    is_synthetic: true, eligible: true, reasons: [], approval_status: "SYNTHETIC", limitations: [], history: {},
    validation: { status: "SYNTHETIC", experiment_id: null, oos_metrics: { sharpe_net: 0.7, max_drawdown: -0.2, bars: 2000 } } },
  { id: "H:h1", version: 1, name: "EURUSD momentum", source: "hypothesis", universe: ["EURUSD"], frequency: "1d", group: "fx", counterparty: "unassigned",
    is_synthetic: false, eligible: false, reasons: ["status VALIDATION_FAILED is not one of [VALIDATION_PASSED]"], approval_status: "VALIDATION_FAILED",
    limitations: [], history: {}, validation: { status: "VALIDATION_FAILED", experiment_id: "e", oos_metrics: { sharpe_net: 0.31, max_drawdown: -0.3, bars: 1500 } } },
];

function mockFetch() {
  const calls: { url: string; method: string; body?: string }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, method: init?.method ?? "GET", body: init?.body as string | undefined });
    let body: unknown = [];
    let status = 200;
    if (url.startsWith("/api/v1/portfolio/strategies")) body = strategies;
    else if (url === "/api/v1/portfolio/policies/active") body = policy;
    else if (url === "/api/v1/portfolio/policies") { body = { detail: "a new policy must be confirmed explicitly" }; status = 403; }
    else if (url === "/api/v1/portfolio/runs" && init?.method === "POST") body = { job: { id: "j1" } };
    else if (url === "/api/v1/portfolio/runs") body = [run];
    else if (url === "/api/v1/portfolio/runs/r1") body = run;
    else if (url.startsWith("/api/v1/portfolio/runs/r1/decisions")) body = { items: [{ as_of: "2022-08-31 00:00:00+00:00", level: "strategy", scope: "SYN:a", rule: "max_weight",
      observed: "0.5", limit: "0.35", decision: "blocked", action: "capped 0.5 -> 0.35", reason: "allocation above the per-strategy maximum", policy_version: "default@v1#abcd1234" }] };
    else if (url === "/api/v1/portfolio/proposals") body = [];
    return new Response(JSON.stringify(body), { status });
  }));
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe("Portfolio & Risk", () => {
  it("lists eligible and rejected strategies with reasons and runs only eligible ones", async () => {
    const calls = mockFetch();
    render(<MemoryRouter><PortfolioPage /></MemoryRouter>);
    expect(await screen.findByText(/status VALIDATION_FAILED is not one of/)).toBeInTheDocument();
    expect(screen.getByLabelText("select H:h1")).toBeDisabled();
    expect(screen.getByLabelText("select SYN:a")).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/v1/portfolio/runs")).toBe(true));
    const post = calls.find((c) => c.method === "POST" && c.url === "/api/v1/portfolio/runs")!;
    expect(JSON.parse(post.body!).strategy_ids).toEqual(["SYN:a"]);
  });

  it("separates observed history from estimates and shows the risk manager's reasons", async () => {
    mockFetch();
    render(<MemoryRouter><PortfolioPage /></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: "open" }));
    expect(await screen.findByText(/generated from a seeded factor model/)).toBeInTheDocument();
    expect(screen.getAllByText("historical · observed").length).toBeGreaterThan(1);
    expect(screen.getAllByText("estimate · not a forecast").length).toBeGreaterThan(1);
    expect(screen.getByText("redundant: correlation 0.92 with SYN:a")).toBeInTheDocument();
    expect(screen.getByText("They are not forecasts")).toBeInTheDocument();
    expect(await screen.findByText("allocation above the per-strategy maximum")).toBeInTheDocument();
    expect(screen.getByText("capped 0.5 -> 0.35")).toBeInTheDocument();
  });

  it("requires a named person and confirmation for a new policy version", async () => {
    mockFetch();
    render(<MemoryRouter><PortfolioPage /></MemoryRouter>);
    const save = await screen.findByRole("button", { name: "Save new version" });
    expect(save).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Approved by"), { target: { value: "ana" } });
    expect(save).toBeDisabled();
    fireEvent.click(screen.getByLabelText("I confirm this new policy version"));
    expect(save).toBeEnabled();
  });
});

import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import PaperValidationPage from "../pages/PaperValidation";

const elig = [
  { lab_experiment_id: "e1", title: "QQQ 1d: time-series momentum", family: "momentum", is_synthetic: false, universe: ["QQQ"], decision: "INCONCLUSIVE",
    robustness_run_id: "r1", robustness_state: "FRAGILE", meets_evidence_requirements: false,
    checks: [{ check: "lab_decision", passed: false, detail: "lab decision INCONCLUSIVE (needs OOS_SUPPORTED or PAPER_ELIGIBLE)", blocking: true }] },
];
const cands = [
  { id: "c1", status: "admitted", name: "SYN momentum", version: 1, admission_kind: "exploratory", admitted_by: "ana", admitted_at: "2026-10-28T10:00:00Z",
    strategy: "ts_momentum", params: { lookback: 60 }, universe: ["SYN-D"], timeframe: "1d", is_synthetic: true, allowed_modes: ["C"], integrity_ok: true, record_hash: "abcdef1234567890" },
  { id: "c0", status: "refused", name: "QQQ momentum", reasons: ["lab decision INCONCLUSIVE"], requested_by: "ana" },
];
const summary = { id: "p1", candidate_id: "c1", candidate_name: "SYN momentum", candidate_version: 1, protocol_id: "q1", session_id: "s1", mode: "C",
  mode_label: "SYNTHETIC_SIMULATION", admission_kind: "exploratory", is_synthetic: true, status: "FINISHED", rule_state: "INCONCLUSIVE",
  classification: { state: null, reviewed: false }, open_pauses: 0, bars: 120, prospective_bars: 0 };
const curves = { ts: ["2024-01-01", "2024-01-02"], paper: [1, 1.01], backtest: [1, 1.012], baseline: [1, 1], drawdown: [0, 0], gross: [0.3, 0.3] };
const st = { n: 120, mean: 0.0002, mean_ci95: [-0.001, 0.0014], vol_ann: 0.1, sharpe_ann: 0.4, sharpe_se: 1.4, total_return: 0.02, max_drawdown: -0.05, hit_rate: 0.5 };
const detail = {
  summary, config: {}, candidate: cands[0], candidate_snapshot: {}, review_allowed: ["INCONCLUSIVE", "REJECTED"],
  protocol: { mode_note: "Synthetic simulation: tests the procedure only; it is not evidence about any market.", frozen_at: "2026-10-28T10:01:00Z",
    replay: { start: "2023-12-05", end: "2024-09-30", note: "the reserved final period, never used to select the parameters" } },
  session: { id: "s1", status: "PAUSED", pause_reason: "program finished", kill_switch: false, bars_processed: 120, cursor: "2024-06-01", summary: {} },
  tracking: { bars: 120, trades: 4, horizon_bars: 90, min_trades: 3, provisional: false, provisional_reasons: [], baseline: "no_trade", paper: st, backtest: st, baseline_stats: st,
    excess_vs_baseline: { mean: 0.0002, mean_ci95: [-0.001, 0.0014] }, curves, rolling: null, note: "Intervals are not a probability of future profit." },
  comparison: { comparable: true, conventions: [{ check: "strategy_and_parameters", passed: true, detail: "ts_momentum" }],
    returns: { paper_total: 0.02, backtest_total: 0.021, diff_ci95_bps: [-0.5, 0.4], correlation: 0.99, tracking_error_ann: 0.01 },
    volatility: { paper_ann: 0.1, backtest_ann: 0.1 }, drawdown: { paper: -0.05, backtest: -0.05, ratio: 1 },
    signal_frequency: { paper_changes_per_100_bars: 3, backtest_changes_per_100_bars: 3, agreement: 1 }, per_trade: { units: "net return per trade", paper: null, backtest: null },
    exposure: { paper_avg_gross: 0.3, backtest_avg_gross: 0.3 }, per_asset: [], per_asset_units: "fraction", regimes: [],
    unreconciled: [{ item: "mean return per bar", unreconciled_bps: 0.7, explained_by_costs_bps: 0.1, cause: "not asserted: no measured component explains the remainder" }],
    costs: { paper_total: 120, backtest_total: 100, ratio: 1.2, paper_breakdown: { commission: 50 }, paper_cost_bps_per_fill: { p50: 3, p90: 5 }, note: "configured cost models are estimates" },
    fills: { orders: 10, fills: 10, fill_rate: 1, partial: 0, rejected: 0, expired: 0, avg_shortfall_vs_decision_close_bps: 2 }, note: "A comparison is not an approval." },
  rule: { state: "INCONCLUSIVE", reasons: ["primary_excess_over_baseline: CI includes zero"], provisional: false,
    criteria: [{ criterion: "primary_excess_over_baseline", kind: "accept", met: false, evaluable: true, detail: "excess CI [-0.001, 0.0014]" }],
    note: "COMPLETED means the protocol finished with its criteria met in simulation. It does not authorise real trading." },
  classification: { state: null, reviewed: false },
  pauses: [{ id: 1, cause: "drawdown", message: "drawdown -3% beyond the pause level", evidence: { drawdown: -0.03 }, at: "2026-10-28T10:02:00Z", sim_ts: "2024-02-01",
    resolved_at: "2026-10-28T10:03:00Z", resolved_by: "ana", resolution: "within expectations" }],
  reviews: [], errors: [], missing_bars: 0, feed_log: [{ bar: "2024-06-01", closed: "2024-06-01", available: "2024-06-01", processed_at: "2026-10-28", processing_delay_h: 1, prospective: false }],
  timeline: [{ at: "2026-10-28T10:01:00Z", sim_ts: null, kind: "started", detail: "program started by ana", event_seq: 1, evidence: {} }],
  events: [{ seq: 5, sim_ts: "2024-06-01", level: "info", category: "program", code: "program_finished", message: "session paused: program finished" }],
};

function mockFetch() {
  const calls: { url: string; method: string; body?: string }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    calls.push({ url, method, body: init?.body as string | undefined });
    let body: unknown = [];
    if (url === "/api/v1/program/eligibility") body = elig;
    else if (url === "/api/v1/program/candidates" && method === "GET") body = cands;
    else if (url === "/api/v1/program/protocols" && method === "GET") body = [];
    else if (url === "/api/v1/program/programs" && method === "GET") body = [summary];
    else if (url === "/api/v1/program/programs/p1") body = detail;
    else if (url === "/api/v1/program/candidates/c1/versions") body = [{ ...cands[0], programs: [summary] }];
    else if (url === "/api/v1/program/programs/p1/review") body = summary;
    else if (url === "/api/v1/program/candidates" && method === "POST") return new Response(JSON.stringify({ detail: "admission refused, evidence requirements not met" }), { status: 400 });
    return new Response(JSON.stringify(body), { status: 200 });
  }));
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe("Paper Trading Validation", () => {
  it("shows the registry, the ten sections of a program and keeps modes labelled", async () => {
    mockFetch();
    render(<MemoryRouter><PaperValidationPage /></MemoryRouter>);
    expect(await screen.findByText("QQQ 1d: time-series momentum")).toBeInTheDocument();
    expect(screen.getByText("not met")).toBeInTheDocument();
    expect(screen.getByText(/lab decision INCONCLUSIVE \(needs OOS_SUPPORTED/)).toBeInTheDocument();
    expect(screen.getAllByText("exploratory").length).toBeGreaterThan(0);
    fireEvent.click(await screen.findByRole("button", { name: "Open" }));
    for (const h of ["3 · Session status: SYN momentum v1", "4 · Mode and data feed", "6 · Comparison with the backtest", "7 · Costs and execution quality",
      "8 · Alerts and pauses", "9 · Versions and history", "10 · Final report"]) expect(await screen.findByText(h)).toBeInTheDocument();
    expect(screen.getByText("1 · Candidate registry")).toBeInTheDocument();
    expect(screen.getByText("2 · Protocols (fixed before the program starts)")).toBeInTheDocument();
    expect(screen.getByText(/5 · P&L and risk \(SYNTHETIC SIMULATION/)).toBeInTheDocument();
    expect(screen.getAllByText("SYNTHETIC SIMULATION · not market evidence").length).toBeGreaterThan(0);
    expect(screen.getAllByText("pending review").length).toBe(2); // in the list and in the program
    expect(screen.getByText(/not asserted: no measured component/)).toBeInTheDocument();
    expect(screen.getByText(/It does not authorise real trading/)).toBeInTheDocument();
    expect(screen.getByText(/within expectations/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Export JSON" })).toHaveAttribute("href", "/api/v1/program/programs/p1/export.json");
    const select = screen.getByLabelText(/Classification/);
    expect(within(select).queryByText("COMPLETED")).not.toBeInTheDocument(); // never above the rules
  });

  it("records a review and shows a refused admission", async () => {
    const calls = mockFetch();
    render(<MemoryRouter><PaperValidationPage /></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: "Open" }));
    await screen.findByText("10 · Final report");
    fireEvent.change(screen.getByLabelText("Person"), { target: { value: "ana" } });
    fireEvent.change(screen.getByLabelText(/Classification/), { target: { value: "INCONCLUSIVE" } });
    fireEvent.change(screen.getByLabelText("Rationale"), { target: { value: "Rules confirmed after the comparison." } });
    fireEvent.click(screen.getByRole("button", { name: "Record review" }));
    await vi.waitFor(() => expect(calls.some((c) => c.url.endsWith("/review") && c.method === "POST")).toBe(true));
    const sent = JSON.parse(calls.find((c) => c.url.endsWith("/review"))!.body!);
    expect(sent).toEqual({ by: "ana", decision: "INCONCLUSIVE", rationale: "Rules confirmed after the comparison." });
    fireEvent.click(screen.getByRole("button", { name: "Admit candidate" }));
    expect(await screen.findByText(/evidence requirements not met/)).toBeInTheDocument();
  });
});

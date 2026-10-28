import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import StrategyReviewPage from "../pages/StrategyReview";

const ok = (v: number | null, n = 90) => ({ status: "OK", value: v, n, min_n: 1, unit: "", note: "" });
const insufficient = (n = 12) => ({ status: "INSUFFICIENT_DATA", value: null, n, min_n: 60, unit: "", note: "needs at least 60 (has 12)" });

const row = {
  decision_id: "d1", subject: "program:p1", subject_kind: "program", strategy: "SYN momentum", version: 1, program_id: "p1",
  session_id: "s1", evidence_class: "SYNTHETIC", admission_kind: "exploratory", markets: ["other"], assets: ["SYN-D"], timeframes: ["1d"],
  first_bar: "2023-12-05", last_bar: "2024-04-08", bars: 90, horizon_bars: 90, trades: 4, independent_trades: 4,
  cumulative_return: ok(0.008), max_drawdown: ok(-0.02), excess_ci95: [-0.001, 0.002], backtest_diff_ci95_bps: [-0.4, 0.3],
  comparable: true, integrity: "LIMITED", open_incidents: 0, criteria_met: 6, criteria_total: 8, state: "INCONCLUSIVE",
  reason: "criteria unmet", decided_at: "2026-10-28T10:00:00Z", engine_version: "review-decision@1.0.0", integrity_ok: true,
  current: { state: "INCONCLUSIVE", source: "engine (pending a person's review)" },
};
const report = {
  decision_id: "d1", subject: "program:p1", strategy: "SYN momentum", version: 1, label: "SYNTHETIC_SIMULATION", evidence_class: "SYNTHETIC",
  decided_at: "2026-10-28T10:00:00Z", engine_version: "review-decision@1.0.0", record_hash: "abcdef0123456789abcdef", integrity_ok: true,
  not_an_authorisation: "No state authorises real trading or sending any order.",
  what_we_can_and_cannot_conclude: { can_conclude: ["what was observed in the period, with its uncertainty"], cannot_conclude: ["that the strategy will be profitable in the future or with real money"] },
  sections: {
    "1_hypothesis": {}, "2_original_protocol": { amendments: [{ number: 1, by: "ana", post_hoc: true, changes: { min_trades: 4 }, rule_state_original: "INCONCLUSIVE", rule_state_amended: "INCONCLUSIVE", justification: "documented" }] },
    "3_evidence": {}, "4_data_quality": { integrity: { status: "LIMITED", note: "No data was corrected.", checks: [
      { check: "no_lookahead", status: "PASS", material: false, detail: "0 fills on the decision bar" },
      { check: "open_positions_and_missing_data", status: "WARN", material: false, detail: "1 position open at the last bar" }] } },
    "5_performance_and_risk": { performance: { pnl_net: ok(812.5) }, risk: { sharpe_ann: insufficient() }, execution: { costs_total: ok(20.1, 5) }, note: "INSUFFICIENT_DATA means the value is withheld" },
    "6_comparison_backtest_and_baseline": { comparable: true, compatibility: [{ check: "strategy_version", compatible: true, detail: "same" }],
      baseline: { baseline: "no_trade", excess_mean: 0.0001, excess_ci95: [-0.001, 0.002], test: { p_one_sided: 0.3 } },
      backtest: { returns: { paper_total: 0.008, backtest_total: 0.009, diff_ci95_bps: [-0.4, 0.3], correlation: 0.99 }, drawdown: {}, costs: { ratio: 1.1 }, signal_frequency: { agreement: 1 } },
      deviations: [], research_oos_context: { sharpe_net: 0.5, n_trades: 30, note: "context only" } },
    "7_uncertainty": { effective_n: 80, p_note: "p is not the probability that the strategy is profitable.", multiple_testing: { adjusted_p: 0.3 },
      layers: { statistical_evidence: "the 95% interval includes zero", economic_evidence: "mean excess 1 bps" } },
    "8_operational_incidents": { incidents: { pauses: 0 }, timeline: [], diagnosis: { triggered: true, why: ["paper below the backtest"], note: "No cause is asserted.",
      causes: [{ cause: "underestimated costs", status: "COMPATIBLE", evidence: "ratio 1.1", data_to_distinguish: "observed spreads" }] } },
    "9_criteria": { protocol_criteria: [{ criterion: "primary_excess_over_baseline", kind: "accept", met: false, evaluable: true, detail: "CI includes zero" }],
      readiness_checks: [{ check: "stable_enough", met: null, detail: "too few bars", source: "review" }] },
    "10_limitations": ["synthetic data: the result tests the procedure, not any market"],
    "11_final_state": { engine: { state: "INCONCLUSIVE", reason: "the protocol finished without meeting its acceptance criteria", rules: [{ rule: "stopped_early_or_criteria_unmet", applies: true, detail: "unmet" }],
      uncertainties: ["excess CI"], recommended_actions: ["new protocol"], engine_version: "review-decision@1.0.0" }, current: { state: "INCONCLUSIVE", source: "engine (pending a person's review)" }, manual_reviews: [] },
    "12_next_steps": ["new protocol"],
  },
};

function mockFetch(opts: { empty?: boolean } = {}) {
  const calls: { url: string; method: string; body?: string }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    calls.push({ url, method, body: init?.body as string | undefined });
    let body: unknown = [];
    if (url.startsWith("/api/v1/review/strategies")) body = opts.empty ? [] : [row];
    else if (url === "/api/v1/review/summary") body = opts.empty
      ? { status: "INSUFFICIENT_EVIDENCE", note: "No session is fabricated.", to_collect: ["no paper trading session exists: nothing has been observed"], subjects: [] }
      : { status: "INSUFFICIENT_EVIDENCE", note: "No session is fabricated.", to_collect: [], subjects: [] };
    else if (url === "/api/v1/review/decisions/d1") body = { summary: row, report, manual_allowed: ["INCONCLUSIVE", "PAUSED_REVIEW", "REJECTED"], is_latest: true };
    else if (url.startsWith("/api/v1/review/history")) body = [{ kind: "engine", id: "d1", at: "2026-10-28T10:00:00Z", state: "INCONCLUSIVE", by: "ana", reason: "criteria unmet" }];
    else if (url === "/api/v1/review/decisions/d1/manual") return new Response(JSON.stringify({ id: "m1" }), { status: 201 });
    else if (url === "/api/v1/review/run") body = [];
    return new Response(JSON.stringify(body), { status: 200 });
  }));
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe("Strategy Review & Readiness", () => {
  it("shows insufficient evidence when nothing exists, and runs a review", async () => {
    const calls = mockFetch({ empty: true });
    render(<MemoryRouter><StrategyReviewPage /></MemoryRouter>);
    expect(await screen.findByText(/Still to collect: no paper trading session exists/)).toBeInTheDocument();
    expect(screen.getByText("INSUFFICIENT EVIDENCE")).toBeInTheDocument();
    expect(screen.getByText(/No reviewed strategy/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Run by"), { target: { value: "ana" } });
    fireEvent.click(screen.getByRole("button", { name: "Run review" }));
    await vi.waitFor(() => expect(calls.some((c) => c.url === "/api/v1/review/run" && c.method === "POST")).toBe(true));
    fireEvent.change(screen.getByLabelText("State filter"), { target: { value: "REJECTED" } });
    await vi.waitFor(() => expect(calls.some((c) => c.url.includes("state=REJECTED"))).toBe(true));
  });

  it("shows a strategy's evidence, integrity, criteria, decision and history, and records a manual review", async () => {
    const calls = mockFetch();
    render(<MemoryRouter><StrategyReviewPage /></MemoryRouter>);
    expect(await screen.findByText("SYN momentum v1")).toBeInTheDocument();
    expect(screen.getAllByText("SYNTHETIC · not market evidence").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    for (const h of ["Decision and justification", "Quality and integrity of the results", "Metrics", "Comparison with the baseline",
      "Comparison with the backtest", "Acceptance criteria (fixed before the start)", "Alerts, incidents and diagnosis", "Decision history"]) {
      expect(await screen.findByText(h)).toBeInTheDocument();
    }
    expect(screen.getByText("What we cannot conclude")).toBeInTheDocument();
    expect(screen.getByText(/will be profitable in the future/)).toBeInTheDocument();
    expect(screen.getAllByText(/INSUFFICIENT DATA \(n=12\)/).length).toBeGreaterThan(0); // withheld, not zero
    expect(screen.getByText(/after results were seen/)).toBeInTheDocument();
    expect(screen.getByText(/No state authorises real trading/)).toBeInTheDocument();
    const select = screen.getByLabelText("Classification");
    expect(within(select).queryByText("SUPPORTED_FOR_FURTHER_RESEARCH")).not.toBeInTheDocument(); // never above the engine
    fireEvent.change(screen.getByLabelText("Person"), { target: { value: "ana" } });
    fireEvent.change(select, { target: { value: "REJECTED" } });
    fireEvent.change(screen.getByLabelText("Rationale"), { target: { value: "Reviewed: the cost assumptions are not credible." } });
    fireEvent.click(screen.getByRole("button", { name: "Record manual review" }));
    await vi.waitFor(() => expect(calls.some((c) => c.url.endsWith("/manual") && c.method === "POST")).toBe(true));
    const sent = JSON.parse(calls.find((c) => c.url.endsWith("/manual"))!.body!);
    expect(sent).toEqual({ by: "ana", state: "REJECTED", rationale: "Reviewed: the cost assumptions are not credible." });
    expect(screen.getByRole("link", { name: "Report Markdown" })).toHaveAttribute("href", "/api/v1/review/decisions/d1/report.md");
  });
});

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import AlphaResearchPage from "../pages/AlphaResearch";
import AlphaHypothesisPage from "../pages/AlphaHypothesis";

const run = {
  id: "r1", status: "succeeded", created_at: "2026-10-24T10:00:00Z", finished_at: null, error: null, budget: null,
  n_ideas: 19, n_registered: 11, n_duplicates: 0, n_rejected_by_review: 8, n_data_gaps: 7, n_experiments: 2,
  n_validation_passed: 0, n_validation_failed: 2, n_variants_backtested: 4, duration_s: 1.2,
};
const row = {
  id: "h1", title: "EURUSD 1d: reversal after return shocks", status: "VALIDATION_FAILED", version: 1,
  created_by: "agent:quant_analyst", created_at: "2026-10-24T10:00:00Z", origin: "anomaly",
  origin_observation: "The scans flagged 40 return shocks", strategy: "event", family: "mean_reversion",
  n_source_anomalies: 20, objections: { block: 0, major: 2, minor: 1 }, research_priority: 0.42,
  predictive_evidence: { level: "no_evidence", text: "OOS net Sharpe -0.3, DSR 0.1; blocked by gate(s) B" },
  validated_net_profitability: { established: false, text: "Not established: validation_passed, explicit_costs" },
};
const detail = {
  hypothesis: { id: "h1", title: row.title, status: row.status, version: 1, created_by: row.created_by, created_at: row.created_at,
    contract_hash: "abc", final_test_accesses: 0,
    contract: { origin: "anomaly", origin_observation: row.origin_observation, premise: "p", mechanism: "m", features: ["z"],
      strategy: { type: "event" }, entry_rule: "enter", exit_rule: "exit", horizon_bars: 5, benchmark: "cash",
      invalidation: ["OOS Sharpe <= 0"], limitations: [], success: { metric: "sharpe_net", min_value: 0.5 }, validation: {}, costs: {} } },
  idea_key: "idea_1",
  messages: [{ id: "m1", run_id: "r1", seq: 1, sender: "alpha_researcher", receiver: "quant_analyst", kind: "idea", subject_key: "idea_1",
    hypothesis_id: "h1", payload: { priority: 0.42 }, evidence: [], created_at: row.created_at }],
  objections: [{ code: "default_costs", severity: "major", message: "Costs are asset-class defaults" }],
  decisions: [{ key: "idea_1", action: "registered", reason: "no blocking objection", hypothesis_id: "h1", experiment_id: null }],
  validations: [{ experiment_id: "e1", status: "succeeded", created_at: row.created_at, error: null, decision: "VALIDATION_FAILED",
    variants: 3, windows: 6, metrics: { sharpe_net: -0.3, total_return_net: -0.05, n_trades: 41 }, benchmark_sharpe_net: null,
    excess_vs_benchmark_mean_net: null, sharpe_ci: [-1.2, 0.6], positive_window_frac: 0.33, placebo_pvalue: 0.7, dsr: 0.1, pbo: 0.6,
    gates: [], leakage_checks_passed: true }],
  gates: [{ gate: "B", decision: "block", reasons: ["OOS Sharpe below 0.5"], experiment_id: "e1", created_at: row.created_at }],
  eligibility: { eligible: false, requirements: [{ requirement: "validation_passed", met: false, detail: "status VALIDATION_FAILED" }] },
  predictive_evidence: row.predictive_evidence, validated_net_profitability: row.validated_net_profitability,
};

const worthItem = (key: string, title: string, verdict: string, verdict_label: string, reasons: string[], score: number | null) => ({
  key, title, verdict, verdict_label, reasons, score, route: "anomaly", family: "momentum", dataset_ids: ["d1"],
  origin_observation: "obs " + key, detector: null, strategy: "ts_momentum", priority: 0.3, refuted_if: [], objections: [], existing: [],
});
const worth = {
  counts: { worth_testing: 1, already_tested: 0, not_worth: 1, not_testable: 0 },
  items: [
    worthItem("a", "GBPUSD 1d: time-series momentum", "worth_testing", "Worth testing", ["No blocking objection and no earlier study refuted it."], 0.3),
    worthItem("b", "EURUSD 1h: reversal after return shocks", "not_worth", "Not worth testing", ["an earlier event study found no out-of-sample evidence"], null),
  ],
  data_gaps: [],
  notes: ["Read-only: nothing was registered or backtested to build this list."],
};

function mockFetch() {
  const calls: { url: string; method: string; body?: string }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, method: init?.method ?? "GET", body: init?.body as string | undefined });
    let body: unknown = [];
    if (url.startsWith("/api/v1/alpha/worth-testing")) body = worth;
    else if (url === "/api/v1/alpha/runs" && init?.method === "POST") body = { job: { id: "j1" } };
    else if (url === "/api/v1/alpha/runs") body = [run];
    else if (url.startsWith("/api/v1/alpha/runs/")) body = { run, messages: [{ ...detail.messages[0], kind: "data_gap", payload: { family: "factor", reason: "Value: No point-in-time fundamentals" } }] };
    else if (url === "/api/v1/alpha/hypotheses") body = [row];
    else if (url.startsWith("/api/v1/alpha/hypotheses/h1/experiments")) body = { job: { id: "j2" } };
    else if (url.startsWith("/api/v1/alpha/hypotheses/h1")) body = detail;
    else if (url.startsWith("/api/v1/alpha/factors")) body = [{ name: "value", title: "Value", available: false, reason: "No point-in-time fundamentals", formula: "B/M", universes: [] }];
    return new Response(JSON.stringify(body), { status: 200 });
  }));
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe("Alpha Research", () => {
  it("lists hypotheses with priority, predictive evidence and net profitability kept apart", async () => {
    mockFetch();
    render(<MemoryRouter><AlphaResearchPage /></MemoryRouter>);
    expect(await screen.findByText(row.title)).toBeInTheDocument();
    for (const h of ["Research priority", "Predictive evidence", "Validated net profitability", "Origin", "State"]) {
      expect(screen.getByRole("columnheader", { name: h })).toBeInTheDocument();
    }
    expect(screen.getByText("no OOS evidence")).toBeInTheDocument();
    expect(screen.getByText("not established")).toBeInTheDocument();
    expect(screen.getByText("VALIDATION_FAILED")).toBeInTheDocument();
    expect(screen.getByText(/2 major/)).toBeInTheDocument();
    expect(await screen.findByText(/Value: No point-in-time fundamentals/)).toBeInTheDocument();
  });

  it("says which anomalies are worth testing and why the rest are not", async () => {
    mockFetch();
    render(<MemoryRouter><AlphaResearchPage /></MemoryRouter>);
    expect(await screen.findByText("GBPUSD 1d: time-series momentum")).toBeInTheDocument();
    expect(screen.getByText("Worth testing")).toBeInTheDocument();
    expect(screen.getByText("Not worth testing (1)")).toBeInTheDocument();
    expect(screen.getByText("an earlier event study found no out-of-sample evidence")).toBeInTheDocument();
  });

  it("launches a bounded research cycle", async () => {
    const calls = mockFetch();
    render(<MemoryRouter><AlphaResearchPage /></MemoryRouter>);
    fireEvent.change(screen.getByLabelText("Max experiments"), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "Run research cycle" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    const post = calls.find((c) => c.method === "POST")!;
    expect(JSON.parse(post.body!).budget.max_experiments).toBe(2);
    expect(await screen.findByText(/Research cycle queued/)).toBeInTheDocument();
  });

  it("shows objections, experiment comparison and why it was rejected", async () => {
    const calls = mockFetch();
    render(
      <MemoryRouter initialEntries={["/alpha/h1"]}>
        <Routes><Route path="/alpha/:id" element={<AlphaHypothesisPage />} /></Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByText("default_costs")).toBeInTheDocument();
    expect(screen.getByText("The scans flagged 40 return shocks")).toBeInTheDocument();
    expect(screen.getByText(/Gate B block/)).toBeInTheDocument();
    expect(screen.getByText("validation_passed")).toBeInTheDocument();
    expect(screen.getByText("[-1.2, 0.6]")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Launch experiment" }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/experiments") && c.method === "POST")).toBe(true));
  });
});

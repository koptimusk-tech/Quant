import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import ModelLabPage from "../pages/ModelLab";

const iso = {
  model: "isolation_forest", detector: "isolation_forest", status: "ok", params: {}, runtime_s: 1.2, n_scored: 800,
  n_flagged: 31, n_episodes: 6, flag_rate: 0.0388, nominal_rate: 0.01, n_bh_significant: 0,
  per_year: [{ year: 2022, bars: 250, flags: 10, rate: 0.04 }], rate_cv_across_years: 0.5,
  per_regime: { high_vol: { bars: 300, flags: 20, rate: 0.066 }, low_vol: { bars: 500, flags: 11, rate: 0.022 }, high_to_low_ratio: 3 },
  stability: { seeds: { jaccard_mean: 0.66, runs: [{ seed: 1, jaccard: 0.66, share_a_in_b: 0.7, share_b_in_a: 0.9 }] },
    window_variants: [{ variant: "train_window=1000", n_flagged: 28, jaccard: 0.79 }] },
  top_feature_counts: { ret_robust_z: 12 }, flags: [{ ts: "2022-03-01T00:00:00Z", close: 1.1, p: 0.002, stat: 4 }],
  predictive: { verdict: "no_evidence", verdict_text: "No out-of-sample evidence.", out_of_sample: { n: 9, mean_excess_bps: -3.2 }, oos_run_wide_adjusted_pvalue: 0.8 },
};
const shock = { ...iso, model: "return_shock", detector: "return_shock", nominal_rate: null, top_feature_counts: {},
  stability: { seeds: { note: "deterministic: the seed does not change the result" }, window_variants: [] }, predictive: { verdict: "insufficient_sample" } };
const exp = {
  id: "e1", status: "succeeded", created_at: "2026-10-24T10:00:00Z", finished_at: null, label: "majors", models: ["isolation_forest", "return_shock"],
  datasets: ["EURUSD 1d"], is_synthetic: false, benchmark: true, duration_s: 12, budget_exhausted: false, error: null, config: {},
  results: {
    datasets: [{ dataset_id: "d1", symbol: "EURUSD", frequency: "1d", quality_status: "warn", is_synthetic: false, status: "ok", n_bars: 7000,
      rows: [iso, shock, { model: "local_outlier_factor", detector: "local_outlier_factor", status: "not_applicable", reason: "need 375 complete feature rows", params: {} }],
      overlap: { models: ["isolation_forest", "return_shock"], matrix: [[null, { jaccard: 0.1, share_a_in_b: 0.19, share_b_in_a: 0.75 }], [{ jaccard: 0.1, share_a_in_b: 0.75, share_b_in_a: 0.19 }, null]] },
      chart: { ts: ["2022-01-01T00:00:00Z", "2022-06-01T00:00:00Z"], close: [1.1, 1.2], step: 1 } }],
    limitations: ["Flag counts measure rarity under each model's own baseline"],
    synthetic_benchmark: { note: "it is not evidence about real markets", rows: [{ model: "isolation_forest", detector: "isolation_forest", recall_scored: 0.8, nominal_rate: 0.01,
      control_false_alarms: { rate: 0.013, episodes: 20 }, events: [{ symbol: "SYN-A", kind: "return_shock", bar: 1800, description: "", scored: true, detected: true, delay_bars: 0 }] }] },
  },
};

function mockFetch() {
  const calls: { url: string; method: string; body?: string }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, method: init?.method ?? "GET", body: init?.body as string | undefined });
    let body: unknown = [];
    if (url === "/api/v1/models/detectors") body = [
      { name: "isolation_forest", version: "1", anomaly_type: "multivariate_outlier", description: "IF", defaults: {}, stochastic: true, multivariate: true },
      { name: "return_shock", version: "1", anomaly_type: "extreme_return", description: "z", defaults: {}, stochastic: false, multivariate: false },
    ];
    else if (url === "/api/v1/models/features") body = { default: ["ret_1"], features: [{ name: "ret_1", group: "returns", formula: "log(P_t / P_{t-1})", source: "close", window: "1 bar", availability: "close", missing: "NaN", assumptions: "a", biases: "b", needs: [] }] };
    else if (url === "/api/v1/datasets") body = [{ id: "d1", symbol: "EURUSD", frequency: "1d", is_synthetic: false, rows: 7000 }];
    else if (url === "/api/v1/models/experiments" && init?.method === "POST") body = { job: { id: "j1" } };
    else if (url === "/api/v1/models/experiments") body = [exp];
    else if (url === "/api/v1/models/experiments/e1") body = exp;
    else if (url.startsWith("/api/v1/jobs?")) body = [{ id: "j9", kind: "model_experiment", status: "running", created_at: exp.created_at }];
    else if (url === "/api/v1/jobs/j9/cancel") body = { id: "j9", status: "cancelling" };
    return new Response(JSON.stringify(body), { status: 200 });
  }));
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe("Model Lab", () => {
  it("shows rarity, stability and out-of-sample evidence side by side, without a combined score", async () => {
    mockFetch();
    render(<MemoryRouter><ModelLabPage /></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: "Open" }));
    expect(await screen.findByText("Out-of-sample evidence")).toBeInTheDocument();
    for (const h of ["Flagged bars", "Episodes", "Seed stability", "Window stability", "High-vol / low-vol rate"]) {
      expect(screen.getAllByText(h).length).toBeGreaterThan(0);
    }
    const headers = Array.from(document.querySelectorAll("th")).map((th) => th.textContent ?? "");
    expect(headers.filter((h) => /\bscore\b/i.test(h))).toEqual([]); // no combined score column
    expect(screen.getAllByText("3.88%").length).toBe(2);
    expect(screen.getByText("no evidence")).toBeInTheDocument();
    expect(screen.getByText("deterministic")).toBeInTheDocument();
    expect(screen.getByText(/not applicable: need 375/)).toBeInTheDocument();
    expect(screen.getByText(/it is not evidence about real markets/)).toBeInTheDocument();
    expect(screen.getByText(/Flag counts measure rarity/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Export CSV" })).toHaveAttribute("href", "/api/v1/models/experiments/e1/export.csv");
  });

  it("queues a comparison with the chosen datasets, models and features, and cancels a running one", async () => {
    const calls = mockFetch();
    render(<MemoryRouter><ModelLabPage /></MemoryRouter>);
    fireEvent.click(await screen.findByLabelText(/EURUSD 1d/));
    fireEvent.click(screen.getByRole("button", { name: "Run comparison" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/v1/models/experiments")).toBe(true));
    const body = JSON.parse(calls.find((c) => c.method === "POST" && c.url === "/api/v1/models/experiments")!.body!);
    expect(body.dataset_ids).toEqual(["d1"]);
    expect(body.models.find((m: { detector: string }) => m.detector === "isolation_forest").params.features).toEqual(["ret_1"]);
    expect(body.models.find((m: { detector: string }) => m.detector === "return_shock").params).toEqual({});
    expect(body.seeds).toEqual([0, 1, 2]);
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/v1/jobs/j9/cancel")).toBe(true));
  });
});

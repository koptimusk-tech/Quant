import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import Scanner from "../pages/Scanner";
import { toQuery } from "../api";

const anomaly = {
  id: "a1",
  experiment_id: "e1",
  detector: "return_shock",
  detector_version: "1.0.0",
  anomaly_type: "extreme_return",
  instrument_id: "i1",
  related_instrument_id: null,
  dataset_id: "d1",
  related_dataset_id: null,
  frequency: "1d",
  event_ts: "2024-01-03T00:00:00+00:00",
  available_ts: "2024-01-03T00:00:00+00:00",
  direction: 1,
  statistic: 8.4,
  empirical_pvalue: 0.004,
  adjusted_pvalue: 0.6,
  statistical_score: 0.63,
  economic_score: null,
  metrics: {},
  economic_context: {},
  data_quality: {},
  alternative_explanations: [],
  is_synthetic: true,
  validation_status: "DISCOVERED",
  symbol: "SYN-A",
  related_symbol: null,
  asset_class: "other",
  market: "",
  provider_id: "synthetic",
  summary: {
    method: "Robust z-score", observed_label: "log return", observed: 0.0123, reference_label: "trailing median",
    reference: 0.0002, reference_scale: 0.0014, deviation: 8.4, deviation_unit: "robust σ",
    relative: null, relative_label: null,
  },
  explanations: {
    rarity: { statistic: 8.4, statistical_score: 0.63, threshold: 5, formula: "", text: "Statistic +8.4 against a detection threshold of 5." },
    significance: { level: "not_significant", label: "Not significant", adjusted_pvalue: 0.6, empirical_pvalue: 0.004, text: "A value near 1 is absence of evidence." },
    economic: { score: null, interpretable: false, formula: "", aggregation: "", components: [], text: "No economic score: index", caveat: null, missing: [] },
    predictive: { text: "Not part of any stored score." },
  },
  investigation_priority: {
    value: 0.12, tier: "low", base: 0.24, components: [], multipliers: [], capped: false,
    explanation: "largest contribution: rarity (0.04); no statistical evidence after multiple-testing control.",
    label: "Research priority, not an execution signal: there is no out-of-sample predictive evidence",
    formula: "", event_study: null,
  },
  stage: { stage: "detection", label: "Detected", hypotheses: [] },
};

function mockFetch() {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      calls.push(url);
      const body = url.startsWith("/api/v1/anomalies/facets")
        ? { asset_class: ["other"], market: [], provider: [], frequency: ["1d"], anomaly_type: ["extreme_return"], detector: [], status: [], symbol: [] }
        : url.startsWith("/api/v1/saved-queries")
          ? []
          : { total: 1, items: [anomaly] };
      return new Response(JSON.stringify(body), { status: 200 });
    }),
  );
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe("Market Scanner", () => {
  it("renders anomalies with separate scores and a synthetic label", async () => {
    mockFetch();
    render(<MemoryRouter><Scanner /></MemoryRouter>);
    expect(await screen.findByText("SYN-A")).toBeInTheDocument();
    expect(screen.getByText("SYNTHETIC")).toBeInTheDocument();
    expect(screen.getByLabelText("statistical score 0.63")).toBeInTheDocument();
    expect(screen.getAllByText("n/a").length).toBeGreaterThan(0); // economic score not available
    // Observed value, statistical reference, deviation and method are shown per anomaly.
    expect(screen.getByText("return_shock")).toBeInTheDocument();
    expect(screen.getByTitle("log return")).toHaveTextContent(/0\.01/);
    expect(screen.getByTitle("trailing median")).toBeInTheDocument();
    expect(screen.getByText("robust σ")).toBeInTheDocument();
  });

  it("keeps the old columns and adds priority, significance, stage and an expandable explanation", async () => {
    mockFetch();
    render(<MemoryRouter><Scanner /></MemoryRouter>);
    await screen.findByText("SYN-A");
    for (const h of ["Stat. score", "Econ. score", "Adj. p", "Status", "Investigation priority", "Stage"]) {
      expect(screen.getByRole("columnheader", { name: new RegExp(h) })).toBeInTheDocument();
    }
    expect(screen.getByLabelText("investigation priority 0.12 low")).toBeInTheDocument();
    expect(screen.getByLabelText("significance not_significant")).toBeInTheDocument();
    expect(screen.getByText("Detected")).toBeInTheDocument();
    expect(screen.getByText("DISCOVERED")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Explain scores for SYN-A/ }));
    const panel = screen.getByLabelText("Score explanations");
    expect(panel).toHaveTextContent("Why it was detected");
    expect(panel).toHaveTextContent("absence of evidence");
    expect(panel).toHaveTextContent("not a validated strategy");
    expect(panel).toHaveTextContent("Not evaluated");
  });

  it("sends filters to the API", async () => {
    const calls = mockFetch();
    render(<MemoryRouter><Scanner /></MemoryRouter>);
    await screen.findByText("SYN-A");
    fireEvent.change(screen.getByLabelText("Data origin"), { target: { value: "false" } });
    await waitFor(() => expect(calls.some((c) => c.includes("synthetic=false"))).toBe(true));
    fireEvent.change(screen.getByPlaceholderText("symbol, type, detector"), { target: { value: "btc" } });
    await waitFor(() => expect(calls.some((c) => c.includes("search=btc"))).toBe(true));
  });
});

describe("toQuery", () => {
  it("repeats array params and skips empty values", () => {
    expect(toQuery({ a: ["x", "y"], b: "", c: undefined, d: 1 })).toBe("?a=x&a=y&d=1");
  });
});

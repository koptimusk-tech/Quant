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

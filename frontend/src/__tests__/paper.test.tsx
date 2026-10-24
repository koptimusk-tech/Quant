import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import PaperTradingPage from "../pages/PaperTrading";

const session = {
  id: "s1", name: "Demo replay", status: "RUNNING", mode: "replay", is_synthetic: true, is_demo: true, kill_switch: false,
  pause_reason: null, cursor_ts: "2024-09-30T00:00:00Z", clock_ts: "2024-10-01T00:00:00Z", bars_processed: 10,
  initial_capital: 100000, account_currency: "USD",
  summary: { equity: 101000, realized_pnl: 400, unrealized_pnl: 600, fees: 20, holding_costs: 5, gross_exposure: 0.3,
    net_exposure: 0.3, drawdown: -0.01, margin_used: 30000, return_net: 0.01 },
  strategies: ["DEMO:ts_momentum"], plans: [{ id: "DEMO:ts_momentum", kind: "demo", type: "ts_momentum", params: {}, dataset_ids: ["d1"],
    allocation: 0.3, status: "DEMO", notes: [], validation: {} }], notes: [], config: {}, config_version: 1, config_hash: "abc",
  config_history: [{ version: 1, at: "2026-10-24T10:00:00Z", by: "ana", change: "created" }], criteria: {}, criteria_hash: "h",
  code_version: "git:x", data_refs: [{ dataset_id: "d1", symbol: "SYN-A", content_hash: "c", provider: "synthetic", is_synthetic: true }],
  policy_version: "default@v1#abcd", experiment_id: "e1", verdict: null, created_by: "ana", created_at: "2026-10-24T10:00:00Z",
  labels: ["SIMULATION ONLY: no order is sent to any broker, exchange or account.",
    "HISTORICAL REPLAY: a reproduction of past bars processed one at a time. It is not real-time trading.",
    "SYNTHETIC DATA: not evidence about any market.", "UNVALIDATED DEMO strategies included."],
  last_signals: {},
};
const order = { id: "o1", client_order_id: "po-1", strategy_id: "DEMO:ts_momentum", instrument: "SYN-A", side: "buy", quantity: 300, lots: 300,
  order_type: "market", limit_price: null, stop_price: null, purpose: "rebalance", status: "REJECTED", created_ts: "2024-09-30T00:00:00Z",
  reference_price: 100, filled_quantity: 0, avg_fill_price: null, commission: 0, slippage_cost: 0, reject_reason: "drawdown limit",
  cancel_reason: null, risk_checks: [] };

function mockFetch() {
  const calls: { url: string; method: string; body?: string }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, method: init?.method ?? "GET", body: init?.body as string | undefined });
    let body: unknown = [];
    if (url === "/api/v1/paper/sessions" && !init?.method) body = [session];
    else if (url === "/api/v1/paper/sessions/s1") body = session;
    else if (url.startsWith("/api/v1/paper/sessions/s1/orders")) body = [order];
    else if (url.startsWith("/api/v1/paper/sessions/s1/kill-switch")) body = { ...session, kill_switch: true, status: "HALTED" };
    else if (url === "/api/v1/paper/strategies") body = { validated: [{ strategy_id: "H:h1", name: "EURUSD momentum", status: "VALIDATION_FAILED", eligible: false, reasons: ["status VALIDATION_FAILED is not eligible"], universe: [], is_synthetic: false }], demo: [{ strategy_id: "DEMO:ts_momentum", name: "ts_momentum", description: "Sign of the trailing return.", defaults: {}, n_assets: 1, note: "UNVALIDATED DEMO: exercises the simulator only" }] };
    else if (url === "/api/v1/datasets") body = [{ id: "d1", symbol: "SYN-A", frequency: "1d", provider_id: "synthetic", is_synthetic: true }];
    return new Response(JSON.stringify(body), { status: 200 });
  }));
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe("Paper Trading", () => {
  it("labels simulation, replay, synthetic data and demo strategies, and separates realized from unrealized P&L", async () => {
    mockFetch();
    render(<MemoryRouter><PaperTradingPage /></MemoryRouter>);
    expect(screen.getByText(/No order is sent to any broker/)).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Demo replay" }));
    expect(await screen.findByText(/It is not real-time trading/)).toBeInTheDocument();
    expect(screen.getByText("UNVALIDATED DEMO strategies included.")).toBeInTheDocument();
    expect(screen.getByText("Unrealized P&L")).toBeInTheDocument();
    expect(screen.getByText("open positions, not realized")).toBeInTheDocument();
    expect(await screen.findByText("drawdown limit")).toBeInTheDocument();
  });

  it("asks for a name and a confirmation before the kill switch", async () => {
    const calls = mockFetch();
    render(<MemoryRouter><PaperTradingPage /></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: "Demo replay" }));
    const kill = await screen.findByRole("button", { name: "Kill switch" });
    expect(kill).toBeDisabled();
    fireEvent.change(screen.getByLabelText("acting person"), { target: { value: "ana" } });
    const confirm = vi.fn(() => false);
    vi.stubGlobal("confirm", confirm);
    fireEvent.click(kill);
    expect(confirm).toHaveBeenCalled();
    expect(calls.some((c) => c.url.includes("kill-switch"))).toBe(false);
    confirm.mockReturnValue(true);
    fireEvent.click(kill);
    await waitFor(() => expect(calls.some((c) => c.url.includes("kill-switch") && c.method === "POST")).toBe(true));
    expect(JSON.parse(calls.find((c) => c.url.includes("kill-switch"))!.body!)).toMatchObject({ by: "ana", active: true, confirm: true });
  });

  it("requires the explicit flag to create a session with an unvalidated demo rule", async () => {
    mockFetch();
    render(<MemoryRouter><PaperTradingPage /></MemoryRouter>);
    fireEvent.change(screen.getByLabelText("acting person"), { target: { value: "ana" } });
    fireEvent.click(screen.getByRole("button", { name: "New session" }));
    expect(await screen.findByLabelText("select H:h1")).toBeDisabled();
    fireEvent.change(screen.getByLabelText("demo strategy"), { target: { value: "DEMO:ts_momentum" } });
    const create = screen.getByRole("button", { name: "Create session" });
    expect(create).toBeDisabled();
    fireEvent.click(screen.getByLabelText("allow unvalidated demo"));
    expect(create).toBeEnabled();
  });
});

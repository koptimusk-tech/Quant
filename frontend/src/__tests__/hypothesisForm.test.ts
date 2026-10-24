import { describe, expect, it } from "vitest";
import { contractProblems, formFromContract, readableApiError } from "../pages/Research";

const ok = {
  title: "EURUSD shock reversal",
  premise: "After an extreme news-driven move the overreaction tends to correct.",
  mechanism: "Liquidity thins around macro releases and market orders overshoot fair value.",
  datasets: ["d1"],
  entry_rule: "Fade the move at the next open",
  exit_rule: "Exit after the horizon",
  features: "return_shock statistic",
  risks: "Trend continuation",
  invalidation: "Net OOS Sharpe <= 0",
  holdout_start: "2025-01-01",
};

describe("hypothesis form validation", () => {
  it("accepts a complete contract", () => {
    expect(contractProblems(ok)).toEqual([]);
  });

  it("lists every missing field in plain words", () => {
    const p = contractProblems({ ...ok, premise: "short", risks: " \n ", holdout_start: "" });
    expect(p).toHaveLength(3);
    expect(p[0]).toMatch(/premise: at least 40 characters \(now 5\)/);
    expect(p.join(" ")).toMatch(/Risks/);
    expect(p.join(" ")).toMatch(/Holdout starts/);
  });

  it("turns a raw 422 body into one readable line per field", () => {
    const raw = '422: [{"loc":["premise"],"msg":"String should have at least 40 characters"},{"loc":["validation","holdout_start"],"msg":"Field required"}]';
    expect(readableApiError(raw)).toBe(
      "Economic / behavioural premise: String should have at least 40 characters\nHoldout starts: Field required",
    );
    expect(readableApiError("500: boom")).toBe("500: boom");
  });
});

describe("suggested hypothesis pre-fill", () => {
  it("maps a proposed contract onto the form so it passes the same checks", () => {
    const f = formFromContract({
      title: "EURUSD 1d: reaction after return shocks",
      premise: ok.premise,
      mechanism: ok.mechanism,
      dataset_ids: ["d1"],
      features: ["return_shock robust z-score"],
      strategy: { type: "event", params: { detector: "return_shock", threshold: 4 }, param_grid: { mode: ["reversal", "continuation"], hold_bars: [1, 3, 5] } },
      entry_rule: ok.entry_rule,
      exit_rule: ok.exit_rule,
      horizon_bars: 5,
      risks: ["a", "b"],
      invalidation: ["x"],
      validation: { train_bars: 700, test_bars: 180, embargo_bars: 5, holdout_start: "2023-06-01T00:00:00+00:00" },
      source_anomaly_ids: ["an1", "an2"],
    });
    expect(f.strategy).toBe("event");
    expect(f.grid).toEqual({ mode: "reversal, continuation", hold_bars: "1, 3, 5" });
    expect(f.fixed).toEqual({ detector: "return_shock", threshold: "4" });
    expect(f.holdout_start).toBe("2023-06-01");
    expect(f.risks).toBe("a\nb");
    expect(f.train_bars).toBe("700");
    expect(contractProblems(f)).toEqual([]);
  });
});

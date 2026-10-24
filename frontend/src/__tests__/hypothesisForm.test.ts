import { describe, expect, it } from "vitest";
import { contractProblems, readableApiError } from "../pages/Research";

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

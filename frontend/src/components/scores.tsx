import type { ReactNode } from "react";
import type { Anomaly, EventStudy, Explanations, Priority, Stage } from "../api";
import { Badge, fmt } from "./ui";

/** Definitions shown as tooltips next to every score (kept in one place so table and detail agree). */
export const DEFINITIONS = {
  statistic: "Detector statistic: how far the observed value is from its trailing reference (robust z-score, log2 volatility ratio or spread z-score). Rarity, not evidence.",
  stat_score: "Statistical score = |statistic| / (|statistic| + threshold). Orders events by size within a detector; 0.5 = at the threshold. Not a probability.",
  econ_score: "Economic score = move / (move + 3 × assumed cost), halved for low liquidity. Uses a placeholder cost per asset class; not a return estimate and not a backtest.",
  adj_p: "Benjamini-Hochberg adjusted p-value over every test in the scan. Lower = more evidence; below 0.05 is significant. A value near 1 means no evidence, not a strong signal.",
  status: "Validation status stored with the anomaly: DISCOVERED, or HYPOTHESIS_FORMULATED once a hypothesis cites it.",
  priority: "Investigation priority: which anomalies to research first. Combines evidence, predictive tests, economic size, rarity, data quality, duplication and age. Not an execution signal or a probability.",
  stage: "Research stage from the hypotheses that cite the anomaly: detected, under investigation, out-of-sample validation passed or failed, paper-trading ready.",
} as const;

const SIG_TONE = { strong: "green", significant: "green", weak: "amber", not_significant: "slate", unknown: "slate" } as const;

/** Visual significance level from the BH-adjusted p-value (same thresholds as the API). */
export function significanceLevel(p: number | null | undefined): keyof typeof SIG_TONE {
  if (p === null || p === undefined || !Number.isFinite(p)) return "unknown";
  if (p < 0.01) return "strong";
  if (p < 0.05) return "significant";
  if (p < 0.1) return "weak";
  return "not_significant";
}

const SIG_LABEL = { strong: "strong", significant: "significant", weak: "weak", not_significant: "not sig.", unknown: "n/a" };

export function SignificanceBadge({ p }: { p: number | null | undefined }) {
  const lv = significanceLevel(p);
  return (
    <span title={DEFINITIONS.adj_p} aria-label={`significance ${lv}`}>
      <Badge tone={SIG_TONE[lv]}>{SIG_LABEL[lv]}</Badge>
    </span>
  );
}

const TIER_TONE = { high: "indigo", medium: "amber", low: "slate" } as const;

export function PriorityBadge({ priority }: { priority: Priority | null | undefined }) {
  if (!priority) return <span className="text-xs text-slate-400">n/a</span>;
  return (
    <span title={`${priority.label}. ${priority.explanation}`} aria-label={`investigation priority ${priority.value.toFixed(2)} ${priority.tier}`} className="inline-flex items-center gap-1">
      <span className="text-xs tabular-nums">{priority.value.toFixed(2)}</span>
      <Badge tone={TIER_TONE[priority.tier]}>{priority.tier}</Badge>
    </span>
  );
}

const STAGE_TONE = { detection: "slate", investigation: "indigo", oos_failed: "red", oos_passed: "green", paper_trading_ready: "green" } as const;

export function StageBadge({ stage }: { stage: Stage | null | undefined }) {
  if (!stage) return <span className="text-xs text-slate-400">n/a</span>;
  return (
    <span title={DEFINITIONS.stage}>
      <Badge tone={STAGE_TONE[stage.stage]}>{stage.label}</Badge>
    </span>
  );
}

const VERDICT_LABEL: Record<string, string> = {
  insufficient_sample: "Insufficient sample",
  no_evidence: "No evidence",
  in_sample_only: "In-sample only",
  oos_supported: "Supported out of sample",
  not_evaluated: "Not evaluated",
};

/** Plain sentence on why the event was flagged, from the detection summary. */
export function whyDetected(a: Anomaly): string {
  const s = a.summary;
  const thr = a.metrics?.threshold;
  if (!s) return `The ${a.detector} statistic was ${fmt(a.statistic)}, beyond the threshold.`;
  return (
    `${s.observed_label ?? "Observed value"} was ${fmt(s.observed)} against a reference of ${fmt(s.reference)}` +
    `${s.reference_scale != null ? ` ± ${fmt(s.reference_scale)}` : ""} (${s.reference_label ?? "trailing baseline"}). ` +
    `The deviation of ${fmt(s.deviation)} ${s.deviation_unit} passed the detector threshold of ${fmt(thr)}, so ${a.detector} flagged it. ` +
    `Only data available up to ${a.available_ts.slice(0, 16).replace("T", " ")} UTC was used.`
  );
}

function Card({ title, tip, children }: { title: string; tip: string; children: ReactNode }) {
  return (
    <div className="rounded border border-slate-200 p-3 dark:border-slate-700">
      <h3 className="mb-1 text-xs font-semibold uppercase text-slate-500" title={tip}>
        {title} <span aria-hidden className="cursor-help">ⓘ</span>
      </h3>
      <div className="text-sm">{children}</div>
    </div>
  );
}

export function EconomicBreakdown({ e }: { e: Explanations["economic"] }) {
  return (
    <div className="flex flex-col gap-2">
      <p>{e.text}</p>
      {!e.interpretable && e.score !== null && (
        <p className="rounded bg-amber-50 p-2 text-xs text-amber-800 dark:bg-amber-950 dark:text-amber-300">Not interpretable for this detector: read it as context only.</p>
      )}
      <div className="overflow-x-auto">
      <table className="w-full text-xs" aria-label="Economic score components">
        <thead>
          <tr><th className="th">Component</th><th className="th">Value</th><th className="th">Role</th><th className="th">Source</th></tr>
        </thead>
        <tbody>
          {e.components.map((c) => (
            <tr key={c.name}>
              <td className="td">{c.name}</td>
              <td className="td tabular-nums">{fmt(c.value)}</td>
              <td className="td">{c.role ?? ""}</td>
              <td className="td text-slate-500" style={{ whiteSpace: "normal" }}>{c.source}</td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
      <p className="text-xs text-slate-500"><span className="font-medium">Formula:</span> {e.formula}. {e.aggregation}</p>
      <div className="text-xs text-slate-500">
        <span className="font-medium">Missing to validate economic relevance:</span>
        <ul className="list-disc pl-5">{e.missing.map((m) => <li key={m}>{m}</li>)}</ul>
      </div>
    </div>
  );
}

export function PriorityBreakdown({ p }: { p: Priority }) {
  return (
    <div className="flex flex-col gap-2">
      <p>
        <PriorityBadge priority={p} /> <span className="text-slate-500">{p.label}.</span>
      </p>
      <p>Why this rank: {p.explanation}</p>
      <table className="text-xs" aria-label="Priority components">
        <thead>
          <tr><th className="th">Component</th><th className="th">Value</th><th className="th">Weight</th><th className="th">Contribution</th><th className="th">Source</th></tr>
        </thead>
        <tbody>
          {p.components.map((c) => (
            <tr key={c.name}>
              <td className="td">{c.name}</td>
              <td className="td tabular-nums">{fmt(c.value)}</td>
              <td className="td tabular-nums">{fmt(c.weight)}</td>
              <td className="td tabular-nums">{fmt(c.contribution)}</td>
              <td className="td text-slate-500" style={{ whiteSpace: "normal" }}>{c.source}</td>
            </tr>
          ))}
          {p.multipliers.map((m) => (
            <tr key={m.name}>
              <td className="td">× {m.name}</td>
              <td className="td tabular-nums">{fmt(m.value)}</td>
              <td className="td" colSpan={2} />
              <td className="td text-slate-500" style={{ whiteSpace: "normal" }}>{m.source}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="text-xs text-slate-500">
        {p.formula}{p.capped ? "; capped at 0.49 without significance or out-of-sample support" : ""}. Base {fmt(p.base)} → {fmt(p.value)}.
      </p>
    </div>
  );
}

/** Rarity, significance, economic relevance and predictive capacity, side by side and never merged. */
export function ScoreExplanations({ a, study, compact = false }: { a: Anomaly; study?: EventStudy | null; compact?: boolean }) {
  const ex = a.explanations;
  if (!ex) return null;
  const verdict = study?.verdict ?? a.investigation_priority?.event_study?.verdict ?? "not_evaluated";
  return (
    <div className="flex flex-col gap-3" aria-label="Score explanations">
      <p className="text-sm"><span className="font-semibold">Why it was detected: </span>{whyDetected(a)}</p>
      <div className={`grid gap-3 ${compact ? "md:grid-cols-2 xl:grid-cols-4" : "md:grid-cols-2"}`}>
        <Card title="1 · Statistical rarity" tip={DEFINITIONS.stat_score}>
          <p>{ex.rarity.text}</p>
        </Card>
        <Card title="2 · Statistical significance" tip={DEFINITIONS.adj_p}>
          <p className="mb-1"><SignificanceBadge p={ex.significance.adjusted_pvalue} /> adjusted p = {fmt(ex.significance.adjusted_pvalue)}</p>
          <p>{ex.significance.text}</p>
        </Card>
        <Card title="3 · Economic relevance" tip={DEFINITIONS.econ_score}>
          {compact ? <p>{ex.economic.text}</p> : <EconomicBreakdown e={ex.economic} />}
        </Card>
        <Card title="4 · Predictive capacity" tip="Evidence that events of this type anticipate returns out of sample, after costs. Comes only from an event study.">
          <p className="mb-1"><Badge tone={verdict === "oos_supported" ? "green" : verdict === "in_sample_only" ? "amber" : "slate"}>{VERDICT_LABEL[verdict] ?? verdict}</Badge></p>
          <p>{study?.verdict_text ?? a.investigation_priority?.event_study?.verdict_text ?? ex.predictive.text}</p>
          {verdict !== "oos_supported" && <p className="mt-1 text-xs text-slate-500">No demonstrated alpha: this is an interesting anomaly, not a validated strategy.</p>}
        </Card>
      </div>
      {a.investigation_priority && (
        <Card title="Investigation priority" tip={DEFINITIONS.priority}>
          {compact ? <p><PriorityBadge priority={a.investigation_priority} /> {a.investigation_priority.explanation}</p> : <PriorityBreakdown p={a.investigation_priority} />}
        </Card>
      )}
    </div>
  );
}

const bps = (v: number | null | undefined) => (v === null || v === undefined || !Number.isFinite(v) ? "—" : v.toFixed(1));

const ci = (c: [number, number] | null | undefined) => (c ? `${bps(c[0])} … ${bps(c[1])}` : "—");

export function EventStudyView({ s }: { s: EventStudy }) {
  const oos = s.out_of_sample;
  const base = s.base_cost_scenario ?? "base_estimate";
  return (
    <div className="flex flex-col gap-3 text-sm" aria-label="Event study">
      <p>
        <Badge tone={s.verdict === "oos_supported" ? "green" : s.verdict === "in_sample_only" ? "amber" : "slate"}>{VERDICT_LABEL[s.verdict]}</Badge>{" "}
        {s.verdict_text}
      </p>
      <p className="text-xs text-slate-500">
        {s.n_events} events{s.position ? ` (${s.position})` : ""}. Entry: {s.entry_rule.toLowerCase()}; exit: {s.exit_rule.toLowerCase()}. Independent
        (non-overlapping) events only. Excess = forward return minus the unconditional return of the same horizon, using only history before entry.
        {s.split_ts ? ` Selection on events before ${s.split_ts.slice(0, 10)}, evaluated once after.` : ""} {s.multiple_testing}
        {s.is_synthetic ? " Synthetic data: results say nothing about real markets." : ""}
      </p>
      <div className="overflow-x-auto">
        <table className="min-w-full text-xs" aria-label="Forward returns by horizon">
          <thead>
            <tr>
              {["Horizon (bars)", "Rule", "Events", "Gross bps", "Excess bps", "95% CI excess", `Cost bps (${base})`, "Net bps", "Hit rate", "Max DD bps", "In-sample adj. p"].map((h) => (
                <th key={h} className="th">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {s.table.map((r) => {
              const sel = s.selected && s.selected.horizon === r.horizon && s.selected.rule === r.rule;
              return (
                <tr key={`${r.horizon}-${r.rule}`} className={sel ? "bg-indigo-50 dark:bg-indigo-950/40" : ""}>
                  <td className="td">{r.horizon}</td>
                  <td className="td">{r.rule}{sel ? " (selected in-sample)" : ""}</td>
                  <td className="td tabular-nums">{r.all.n}</td>
                  <td className="td tabular-nums">{bps(r.all.mean_gross_bps)}</td>
                  <td className="td tabular-nums">{bps(r.all.mean_excess_bps)}</td>
                  <td className="td tabular-nums">{ci(r.all.ci95_excess_bps)}</td>
                  <td className="td tabular-nums">{bps(r.all.mean_cost_bps)}</td>
                  <td className="td tabular-nums">{bps(r.all.mean_net_bps)}</td>
                  <td className="td tabular-nums">{r.all.hit_rate != null ? `${Math.round(r.all.hit_rate * 100)}%` : "—"}</td>
                  <td className="td tabular-nums">{bps(r.all.max_drawdown_bps)}</td>
                  <td className="td tabular-nums">{fmt(r.in_sample.adjusted_pvalue)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {s.selected && oos && (
        <div aria-label="Out of sample">
          <p>
            Out of sample ({s.selected.horizon} bars, {s.selected.rule}): {oos.n} events, mean excess {bps(oos.mean_excess_bps)} bps
            {oos.ci95_excess_bps ? `, 95% CI ${ci(oos.ci95_excess_bps)}` : ""}
            {oos.pvalue_one_sided != null ? `, one-sided p ${fmt(oos.pvalue_one_sided)}` : ""}.
          </p>
          {oos.by_cost && (
            <p className="text-xs text-slate-500">
              Net by cost scenario:{" "}
              {Object.entries(oos.by_cost)
                .map(([k, v]) => `${k} ${bps(v.mean_net_bps)} bps${s.survives_costs_oos?.[k] ? " (CI above 0)" : ""}`)
                .join(" · ")}
            </p>
          )}
        </div>
      )}
      {s.walk_forward && s.walk_forward.length > 0 && (
        <p className="text-xs text-slate-500">
          Walk-forward, each fold selected on the folds before it (mean excess bps):{" "}
          {s.walk_forward.map((f) => `${f.start.slice(0, 10)}: ${bps(f.mean_excess_bps ?? f.mean_net_bps)} (n=${f.n})`).join(" · ")}
        </p>
      )}
      {s.cost_scenarios && (
        <p className="text-xs text-slate-500">
          Cost scenarios (bps per side, commission + half spread + slippage):{" "}
          {s.cost_scenarios
            .map((c) => `${c.name}${c.source === "historical_spread" ? " (historical spread)" : c.source === "estimate" ? " (estimate)" : ""} ${bps(c.commission_bps + c.half_spread_bps + c.slippage_bps)}`)
            .join(" · ")}
          . {s.cost_model?.note}
        </p>
      )}
    </div>
  );
}

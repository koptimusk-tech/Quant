import { useCallback, useEffect, useMemo, useState } from "react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import {
  robustness as api,
  type DimensionStatus,
  type Job,
  type RobustnessCandidate,
  type RobustnessMeta,
  type RobustnessRun,
  type RobustnessRunSummary,
} from "../api";
import { Badge, ErrorBox, KV, Section, SyntheticBadge, fmt, fmtTs } from "../components/ui";
import { chartColors } from "../components/theme";
import { Decision } from "./ExperimentLab";

type Row = Record<string, any>;

const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(2)}%`);
const STATE_TONE: Record<string, "green" | "red" | "amber" | "slate" | "indigo"> = {
  ROBUST_UNDER_TESTED_ASSUMPTIONS: "green",
  MIXED_EVIDENCE: "amber",
  FRAGILE: "red",
  INSUFFICIENT_DATA: "slate",
  NOT_EVALUATED: "slate",
};
const DIM_TONE: Record<DimensionStatus, "green" | "red" | "slate"> = { PASS: "green", FAIL: "red", INSUFFICIENT_DATA: "slate" };

export function State({ s }: { s: string | null | undefined }) {
  if (!s) return <span className="text-xs text-slate-500">—</span>;
  return <Badge tone={STATE_TONE[s] ?? "slate"}>{s.replace(/_/g, " ")}</Badge>;
}

function Dim({ s }: { s: DimensionStatus | undefined }) {
  if (!s) return <span className="text-xs text-slate-400">—</span>;
  return <Badge tone={DIM_TONE[s]}>{s === "INSUFFICIENT_DATA" ? "INSUFF." : s}</Badge>;
}

interface Filters {
  strategy: string;
  asset: string;
  market: string;
  timeframe: string;
  from: string;
  to: string;
}
const NO_FILTERS: Filters = { strategy: "", asset: "", market: "", timeframe: "", from: "", to: "" };

/** True when [a, b] overlaps the filter period (open ends allowed). */
function inPeriod(f: Filters, a?: string | null, b?: string | null) {
  const s = (a ?? "").slice(0, 10);
  const e = (b ?? a ?? "").slice(0, 10);
  return (!f.from || !e || e >= f.from) && (!f.to || !s || s <= f.to);
}

function Table({ cols, rows, empty }: { cols: [string, (r: Row) => React.ReactNode][]; rows: Row[]; empty?: string }) {
  if (!rows.length) return <p className="text-xs text-slate-500">{empty ?? "No rows."}</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-left text-slate-500">{cols.map(([h]) => <th key={h} className="px-1 py-1 font-medium">{h}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-t border-slate-100 align-top dark:border-slate-800">
              {cols.map(([h, f]) => <td key={h} className="px-1 py-1 tabular-nums">{f(r)}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const metricCols: [string, (r: Row) => React.ReactNode][] = [
  ["bars", (r) => fmt(r.bars)],
  ["mean net/bar", (r) => fmt(r.mean_net)],
  ["Sharpe net", (r) => fmt(r.sharpe_net)],
  ["total net", (r) => pct(r.total_return_net)],
  ["max DD", (r) => pct(r.max_drawdown)],
];

// -- candidates (audit) and queue -------------------------------------------------------------------
function Candidates({ rows, onEvaluate, onOpen, busy }: { rows: RobustnessCandidate[]; onEvaluate: (id: string) => void; onOpen: (id: string) => void; busy: string | null }) {
  return (
    <Table
      empty="No completed Experiment Lab candidates match the filters."
      rows={rows as unknown as Row[]}
      cols={[
        ["candidate", (r) => <span className="font-medium">{r.title} {r.is_synthetic && <SyntheticBadge />}</span>],
        ["strategy", (r) => r.strategy],
        ["assets", (r) => r.universe.join(", ")],
        ["market · tf", (r) => `${r.asset_class ?? "—"} · ${r.timeframe ?? "—"}`],
        ["lab decision", (r) => <><Decision d={r.decision} />{r.exploratory && <span className="ml-1 text-amber-700 dark:text-amber-300">exploratory</span>}</>],
        ["adj. p", (r) => fmt(r.adjusted_p)],
        ["OOS Sharpe", (r) => fmt(r.oos_sharpe_net)],
        ["OOS bars · trades", (r) => `${fmt(r.oos_bars)} · ${fmt(r.n_trades)}`],
        ["robustness", (r) => <State s={r.robustness_state} />],
        ["", (r) => (
          <div className="flex gap-1">
            <button className="btn-ghost" disabled={busy === r.experiment_id} onClick={() => onEvaluate(r.experiment_id)}>Evaluate</button>
            {r.robustness_run_id && <button className="btn-ghost" onClick={() => onOpen(r.robustness_run_id)}>View</button>}
          </div>
        )],
      ]}
    />
  );
}

// -- 9. robustness matrix by strategy ---------------------------------------------------------------
function Matrix({ runs, dims, onOpen }: { runs: RobustnessRunSummary[]; dims: string[]; onOpen: (id: string) => void }) {
  return (
    <Table
      empty="No robustness runs yet."
      rows={runs as unknown as Row[]}
      cols={[
        ["candidate", (r) => <button className="text-left underline-offset-2 hover:underline" onClick={() => onOpen(r.id)}>{r.title}</button>],
        ["status", (r) => r.status],
        ["state", (r) => <State s={r.state} />],
        ...dims.map((d) => [d, (r: Row) => <Dim s={r.dimensions?.[d]} />] as [string, (r: Row) => React.ReactNode]),
        ["finished", (r) => fmtTs(r.finished_at)],
      ]}
    />
  );
}

// -- run detail: sections 1-8 and 10 -----------------------------------------------------------------
function Curves({ curves }: { curves: Row }) {
  const c = chartColors();
  const names = ["base", "adverse", "severe"].filter((k) => curves[k]);
  const data = (curves.ts as string[]).map((t, i) => ({ t, ...Object.fromEntries(names.map((n) => [n, curves[n][i]])) }));
  return (
    <div className="h-56" aria-label="Net equity by scenario">
      <ResponsiveContainer>
        <LineChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={c.grid} vertical={false} />
          <XAxis dataKey="t" tick={{ fontSize: 11, fill: c.axis }} minTickGap={50} />
          <YAxis domain={["auto", "auto"]} tick={{ fontSize: 11, fill: c.axis }} width={56} />
          <Tooltip formatter={(v: number) => fmt(v)} />
          <Legend wrapperStyle={{ fontSize: 11 }} />
          {names.map((n, i) => <Line key={n} dataKey={n} name={`${n} (net)`} stroke={c.series[i]} dot={false} isAnimationActive={false} />)}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function RunDetail({ id, meta, filters, onClose }: { id: string; meta: RobustnessMeta; filters: Filters; onClose: () => void }) {
  const [run, setRun] = useState<RobustnessRun | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setRun(null);
    api.run(id).then(setRun).catch((x) => setError(String(x)));
  }, [id]);
  if (error) return <ErrorBox error={error} />;
  if (!run) return null;
  const r = run.results;
  if (!r.dimensions) {
    return (
      <Section title={run.title} right={<button className="btn-ghost" onClick={onClose}>Close</button>}>
        <div className="text-sm"><State s={run.state} /> {run.status}: {(r.state_reasons ?? []).join("; ")}</div>
      </Section>
    );
  }
  const f = filters;
  const assetOk = (x: Row) => (!f.asset || String(x.symbol ?? "").toLowerCase().includes(f.asset.toLowerCase()))
    && (!f.market || [x.asset_class, x.market].includes(f.market)) && (!f.timeframe || x.timeframe === f.timeframe)
    && inPeriod(f, x.period?.[0], x.period?.[1]);
  const xa = r.cross_asset;
  const t = r.temporal;
  const ex = r.execution;
  const cap = r.capacity;
  const st = r.stress;
  const dv = r.diversification;
  return (
    <Section title={`${run.title}: robustness`} right={
      <div className="flex gap-2">
        <a className="btn-ghost" href={api.matrixCsvUrl(run.id)}>Matrix CSV</a>
        <a className="btn-ghost" href={api.exportJsonUrl(run.id)}>Export JSON</a>
        <button className="btn-ghost" onClick={onClose}>Close</button>
      </div>
    }>
      <div className="flex flex-col gap-5 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <State s={run.state} />
          {r.lab?.is_synthetic && <SyntheticBadge />}
          {r.lab?.exploratory && <Badge tone="amber">exploratory: lab decision {r.lab.decision}</Badge>}
          <span className="text-xs text-slate-500">{run.strategy} · {(r.lab?.universe ?? []).join(", ")} · {r.lab?.timeframe} · {run.algorithm_version} · code {run.code_version}</span>
        </div>
        <ul className="text-xs">{run.state_reasons.map((x) => <li key={x}>• {x}</li>)}</ul>
        <p className="text-xs text-slate-500">{r.note} {meta.not_a_guarantee}</p>

        <Table rows={meta.dimensions.map((d) => ({ d, ...r.dimensions[d] }))} cols={[
          ["dimension", (x) => <span className="font-medium">{x.d}{meta.essential.includes(x.d) && " *"}</span>],
          ["status", (x) => <Dim s={x.status} />],
          ["evidence", (x) => x.reason],
          ["criterion", (x) => <span className="text-slate-500">{meta.criteria[x.d]}</span>],
        ]} />
        <p className="text-xs text-slate-500">* essential dimension. A failed temporal, parameter or cost dimension makes the candidate FRAGILE whatever the others show.</p>

        <div>
          <h3 className="mb-1 font-semibold">1 · Assets and markets</h3>
          {xa.not_applicable && <p className="text-xs text-amber-700 dark:text-amber-300">⚠ {xa.not_applicable}</p>}
          <Table rows={xa.rows.filter(assetOk)} cols={[
            ["category", (x) => x.category.replace("_", " ")],
            ["asset", (x) => <>{x.symbol} {x.is_synthetic && <SyntheticBadge />}</>],
            ["market", (x) => `${x.asset_class}${x.market ? ` / ${x.market}` : ""}`],
            ["tf", (x) => x.timeframe],
            ["period", (x) => (x.period ?? []).join(" → ")],
            ["source · quality", (x) => `${x.provider} · ${x.quality_status}`],
            ["costs", (x) => <span title={JSON.stringify(x.costs)}>{x.cost_source}</span>],
            ["status", (x) => x.status === "evaluated" ? x.sample : <span className="text-amber-700 dark:text-amber-300">{x.reason ?? x.status}</span>],
            ["bars · trades", (x) => `${fmt(x.bars)} · ${fmt(x.n_trades)}`],
            ["Sharpe gross", (x) => fmt(x.sharpe_gross)],
            ["Sharpe net", (x) => fmt(x.sharpe_net)],
            ["total net", (x) => pct(x.total_return_net)],
            ["transfer Sharpe", (x) => fmt(x.transfer_fixed_params?.sharpe_net)],
            ["ρ design", (x) => <>{fmt(x.corr_with_design)}{x.near_duplicate_of_design && <span className="ml-1 text-amber-700 dark:text-amber-300">near-duplicate</span>}</>],
          ]} />
          <p className="mt-1 text-xs text-slate-500">Effective independent assets: {fmt(xa.n_effective)}. {xa.note}</p>
        </div>

        <div>
          <h3 className="mb-1 font-semibold">2 · Walk-forward windows, years and regimes</h3>
          <Table rows={t.windows.filter((w: Row) => inPeriod(f, w.test_from, w.test_to))} cols={[
            ["test window", (w) => `${String(w.test_from).slice(0, 10)} → ${String(w.test_to).slice(0, 10)}`],
            ["chosen params", (w) => fmt(w.chosen_params)],
            ["bars", (w) => fmt(w.bars)],
            ["mean net/bar", (w) => <span className={w.mean_net < 0 ? "text-rose-700 dark:text-rose-300" : ""}>{fmt(w.mean_net)}</span>],
            ["Sharpe net ± SE", (w) => `${fmt(w.sharpe_net)} ± ${fmt(w.sharpe_se)}`],
          ]} />
          <p className="my-1 text-xs">Positive windows {pct(t.positive_window_frac)}, positive years {pct(t.positive_year_frac)} (years with ≥ 60 bars).</p>
          <Table rows={t.years.filter((y: Row) => inPeriod(f, `${y.year}-01-01`, `${y.year}-12-31`))} cols={[["year", (y) => y.year], ...metricCols]} />
          <div className="mt-2 grid gap-2 md:grid-cols-3">
            {Object.entries(t.regimes as Row).map(([k, v]) => (
              <div key={k}>
                <div className="text-xs font-medium">{k}</div>
                <Table rows={Object.entries(v as Row).map(([lab, m]) => ({ lab, ...(m as Row) }))} cols={[["regime", (x) => x.lab], ["bars", (x) => fmt(x.bars)], ["mean net", (x) => fmt(x.mean_net)], ["Sharpe", (x) => fmt(x.sharpe_net)]]} />
              </div>
            ))}
          </div>
          {t.regime_dependence.length > 0 && <ul className="text-xs text-amber-700 dark:text-amber-300">{t.regime_dependence.map((x: string) => <li key={x}>⚠ loses in {x}</li>)}</ul>}
          {t.training_length.length > 0 && (
            <Table rows={t.training_length} cols={[["training bars", (x) => fmt(x.train_bars)], ["factor", (x) => fmt(x.factor)], ...metricCols]} />
          )}
          <p className="text-xs text-slate-500">{t.note}</p>
        </div>

        <div>
          <h3 className="mb-1 font-semibold">3 · Parameter sensitivity ({r.parameters.n_configs} configurations)</h3>
          <Table rows={r.parameters.rows} cols={[["configuration", (x) => x.config], ["params", (x) => fmt(x.params)], ...metricCols.slice(0, 4), ["note", (x) => x.not_applicable ?? ""]]} />
          <p className="text-xs text-slate-500">Neighbours positive {pct(r.parameters.neighbour_positive_frac)}, median Sharpe {fmt(r.parameters.neighbour_sharpe_median)} vs centre {fmt(r.parameters.centre_sharpe)}. {r.parameters.note}</p>
        </div>

        <div>
          <h3 className="mb-1 font-semibold">4 · Costs and execution: base, adverse, severe</h3>
          <Table rows={Object.entries(ex.scenarios as Row).map(([k, v]) => ({ k, ...(v as Row) }))} cols={[
            ["scenario", (x) => x.k],
            ["costs ×", (x) => fmt(x.cost_multiplier)],
            ["delay bars", (x) => fmt(x.delay_bars)],
            ["participation ×", (x) => fmt(x.participation_factor)],
            ["gross/bar", (x) => fmt(x.gross_mean)],
            ["cost/bar", (x) => fmt(x.cost_mean)],
            ...metricCols.slice(1),
            ["partial fills", (x) => fmt(x.partial_fill_events)],
          ]} />
          <ul className="mt-1 text-xs text-slate-500">
            {ex.cost_sources.map((x: string) => <li key={x}>{x}</li>)}
            <li>Not modelled: {ex.not_modelled.join("; ")}. {ex.note}</li>
          </ul>
        </div>

        <div>
          <h3 className="mb-1 font-semibold">5 · Net equity by scenario (walk-forward test windows)</h3>
          <Curves curves={ex.curves} />
        </div>

        <div>
          <h3 className="mb-1 font-semibold">6 · Stress tests</h3>
          <p className="text-xs font-medium">Synthetic scenarios (stress tests, not predictions)</p>
          <Table rows={st.synthetic} cols={[["scenario", (x) => x.scenario], ["what changes", (x) => x.description], ...metricCols.slice(1).map(([h, g]) => [h, (x: Row) => (x.not_applicable ? "n/a" : g(x))] as [string, (r: Row) => React.ReactNode])]} />
          <p className="mt-2 text-xs font-medium">Historical episodes observed (largest drawdowns of the reference)</p>
          <Table rows={st.historical} empty="No drawdown deeper than 5% in the evaluated period." cols={[["from", (x) => x.from], ["to", (x) => x.to], ["reference DD", (x) => pct(x.reference_drawdown)], ["candidate return", (x) => pct(x.candidate_return)], ["bars", (x) => fmt(x.bars)]]} />
          <p className="mt-2 text-xs font-medium">Loss of data sources</p>
          <Table rows={st.source_loss} cols={[["without", (x) => x.not_applicable ?? fmt(x.without)], ["Sharpe net", (x) => fmt(x.sharpe_net)]]} />
          <p className="mt-2 text-xs font-medium">Risk controls on unreliable data</p>
          <Table rows={st.risk_controls} cols={[["check", (x) => x.check], ["result", (x) => <Badge tone={x.passed ? "green" : "red"}>{x.passed ? "blocked" : "NOT BLOCKED"}</Badge>], ["evidence", (x) => x.evidence]]} />
          <p className="text-xs text-slate-500">{st.note}</p>
        </div>

        <div>
          <h3 className="mb-1 font-semibold">7 · Capacity: {cap.status}{cap.estimate !== null && cap.estimate !== undefined ? ` ≈ ${fmt(cap.estimate)}` : ""}</h3>
          {cap.reason && <p className="text-xs text-amber-700 dark:text-amber-300">⚠ {cap.reason}</p>}
          {cap.levels && <Table rows={cap.levels} cols={[
            ["capital", (x) => fmt(x.capital)],
            ["mean net after impact", (x) => fmt(x.mean_net_after_impact)],
            ["Sharpe after impact", (x) => fmt(x.sharpe_after_impact)],
            ["participation p95", (x) => pct(x.participation_p95)],
            ["trades over limit", (x) => pct(x.trades_over_limit_frac)],
            ["notional in lowest-liquidity 20%", (x) => pct(x.notional_in_lowest_liquidity_quintile)],
          ]} />}
          <div className="mt-1"><KV data={cap.assumptions} /></div>
          <p className="text-xs text-slate-500">{cap.rule} {cap.note} Observed capacity needs spread and depth data, which this project does not have.</p>
        </div>

        <div>
          <h3 className="mb-1 font-semibold">8 · Incremental portfolio contribution</h3>
          {dv.status_hint ? <p className="text-xs text-slate-500">{dv.reason}</p> : (
            <>
              <Table rows={["reference", "combined", "candidate"].map((k) => ({ k, ...dv[k] }))} cols={[["portfolio", (x) => x.k], ...metricCols]} />
              <KV data={{ "peers (reference, equal weight)": dv.peers.map((p: Row) => p.title).join("; "), days: dv.days,
                "correlation with reference": dv.corr_with_reference, "correlation on stress days": dv.corr_on_stress_days,
                "rolling 126-day correlation": `${fmt(dv.rolling_corr_126d.min)} … ${fmt(dv.rolling_corr_126d.max)}`,
                "active-day overlap": pct(dv.active_day_overlap), "candidate in reference's max drawdown": pct(dv.candidate_return_in_reference_max_drawdown) }} />
              <p className="text-xs text-slate-500">{dv.note}</p>
            </>
          )}
        </div>

        <div>
          <h3 className="mb-1 font-semibold">10 · Limitations and missing data</h3>
          <ul className="text-xs text-amber-700 dark:text-amber-300">{r.limitations.map((x: string) => <li key={x}>⚠ {x}</li>)}</ul>
          <p className="mt-1 text-xs text-slate-500">Lab statistics behind the uncertainty dimension: {fmt(r.lab_statistics)}</p>
        </div>
      </div>
    </Section>
  );
}

// -- page ------------------------------------------------------------------------------------------
export default function RobustnessLabPage() {
  const [meta, setMeta] = useState<RobustnessMeta | null>(null);
  const [cands, setCands] = useState<RobustnessCandidate[]>([]);
  const [runs, setRuns] = useState<RobustnessRunSummary[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [jobLab, setJobLab] = useState<Record<string, string>>({});
  const [open, setOpen] = useState<string | null>(null);
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [c, r] = await Promise.all([api.candidates(), api.runs()]);
      setCands(c);
      setRuns(r);
    } catch (e) {
      setError(String(e));
    }
  }, []);
  useEffect(() => {
    api.meta().then(setMeta).catch((e) => setError(String(e)));
    refresh();
  }, [refresh]);

  const active = jobs.filter((j) => ["queued", "running", "cancelling"].includes(j.status));
  useEffect(() => {
    if (!active.length) return;
    const t = setInterval(async () => {
      const next = await Promise.all(jobs.map((j) => (["queued", "running", "cancelling"].includes(j.status) ? api.job(j.id).catch(() => j) : j)));
      setJobs(next);
      if (next.some((j, i) => j.status !== jobs[i].status)) refresh();
    }, 2000);
    return () => clearInterval(t);
  }, [jobs, active.length, refresh]);

  const evaluate = async (experimentId: string) => {
    setBusy(experimentId);
    setError(null);
    try {
      const out = await api.create({ experiment_id: experimentId });
      const job = out.job;
      if (job) {
        setJobLab((m) => ({ ...m, [job.id]: experimentId }));
        setJobs((js) => [job, ...js]);
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };
  const cancel = async (jobId: string) => {
    try {
      const j = await api.cancelJob(jobId);
      setJobs((js) => js.map((x) => (x.id === jobId ? j : x)));
    } catch (e) {
      setError(String(e));
    }
  };

  const options = useMemo(() => ({
    strategy: [...new Set(cands.map((c) => c.strategy))].sort(),
    market: [...new Set(cands.flatMap((c) => [c.asset_class ?? "", ...c.markets]).filter(Boolean))].sort(),
    timeframe: [...new Set(cands.map((c) => c.timeframe ?? "").filter(Boolean))].sort(),
  }), [cands]);
  const f = filters;
  const candOk = (c: { strategy: string; universe?: string[] | null; asset_class?: string | null; markets?: string[]; timeframe?: string | null; period?: [string, string] | null }) =>
    (!f.strategy || c.strategy === f.strategy)
    && (!f.asset || (c.universe ?? []).some((u) => u.toLowerCase().includes(f.asset.toLowerCase())))
    && (!f.market || c.asset_class === f.market || (c.markets ?? []).includes(f.market))
    && (!f.timeframe || c.timeframe === f.timeframe)
    && inPeriod(f, c.period?.[0], c.period?.[1]);
  const latest = useMemo(() => {
    const seen = new Set<string>();
    return runs.filter((r) => (seen.has(r.experiment_id) ? false : (seen.add(r.experiment_id), true)));
  }, [runs]);
  const titleOf = (id: string) => cands.find((c) => c.experiment_id === id)?.title ?? id;

  if (!meta) return <ErrorBox error={error} />;
  const sel = "input text-xs";
  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-lg font-semibold">Robustness Lab</h1>
      <div className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200">
        Research only. A robustness state describes the tests run under their stated assumptions: it is not a forecast or a probability of success,
        it does not replace the statistical validation and it promotes nothing. No order can be sent from here; paper trading allocations are not changed.
      </div>
      <ErrorBox error={error} />
      <div className="flex flex-wrap items-end gap-2 text-xs" aria-label="Filters">
        <label className="flex flex-col">Strategy
          <select aria-label="Strategy" className={sel} value={f.strategy} onChange={(e) => setFilters({ ...f, strategy: e.target.value })}>
            <option value="">all</option>{options.strategy.map((s) => <option key={s}>{s}</option>)}
          </select>
        </label>
        <label className="flex flex-col">Asset
          <input aria-label="Asset" className={sel} value={f.asset} placeholder="symbol" onChange={(e) => setFilters({ ...f, asset: e.target.value })} />
        </label>
        <label className="flex flex-col">Market
          <select aria-label="Market" className={sel} value={f.market} onChange={(e) => setFilters({ ...f, market: e.target.value })}>
            <option value="">all</option>{options.market.map((s) => <option key={s}>{s}</option>)}
          </select>
        </label>
        <label className="flex flex-col">Timeframe
          <select aria-label="Timeframe" className={sel} value={f.timeframe} onChange={(e) => setFilters({ ...f, timeframe: e.target.value })}>
            <option value="">all</option>{options.timeframe.map((s) => <option key={s}>{s}</option>)}
          </select>
        </label>
        <label className="flex flex-col">From<input aria-label="From" type="date" className={sel} value={f.from} onChange={(e) => setFilters({ ...f, from: e.target.value })} /></label>
        <label className="flex flex-col">To<input aria-label="To" type="date" className={sel} value={f.to} onChange={(e) => setFilters({ ...f, to: e.target.value })} /></label>
        <button className="btn-ghost" onClick={() => setFilters(NO_FILTERS)}>Clear filters</button>
      </div>

      <Section title="Candidates and evidence" right={<button className="btn-ghost" onClick={refresh}>Refresh</button>}>
        <Candidates rows={cands.filter(candOk)} onEvaluate={evaluate} onOpen={setOpen} busy={busy} />
        {jobs.length > 0 && (
          <ul className="mt-2 text-xs">
            {jobs.map((j) => (
              <li key={j.id} className="flex items-center gap-2">
                <Badge tone={j.status === "succeeded" ? "green" : j.status === "failed" ? "red" : "indigo"}>{j.status}</Badge>
                robustness run of {titleOf(jobLab[j.id] ?? "")}
                {["queued", "running"].includes(j.status) && <button className="btn-ghost" onClick={() => cancel(j.id)}>Cancel</button>}
                {j.error && <span className="text-rose-700 dark:text-rose-300">{j.error}</span>}
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="9 · Robustness matrix by strategy (latest run of each candidate)">
        <Matrix runs={latest.filter(candOk)} dims={meta.dimensions} onOpen={setOpen} />
        <details className="mt-2 text-xs text-slate-500">
          <summary>State rules</summary>
          <pre className="whitespace-pre-wrap">{meta.rules}</pre>
        </details>
      </Section>

      {open && <RunDetail id={open} meta={meta} filters={filters} onClose={() => setOpen(null)} />}
    </div>
  );
}

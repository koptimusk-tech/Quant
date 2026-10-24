import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, research, type DatasetRow, type Hypothesis, type StrategyInfo } from "../api";
import { Badge, ErrorBox, Section, SyntheticBadge, fmtTs } from "../components/ui";

export function StatusBadge({ status }: { status: string }) {
  const tone = status === "VALIDATION_PASSED" ? "green" : status === "VALIDATION_FAILED" || status === "REJECTED" ? "red" : status === "BACKTEST_PENDING" ? "amber" : "slate";
  return <Badge tone={tone}>{status}</Badge>;
}

const parseList = (s: string) =>
  s
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean)
    .map((x) => (x === "true" ? true : x === "false" ? false : isNaN(Number(x)) ? x : Number(x)));

interface Form {
  title: string;
  premise: string;
  mechanism: string;
  strategy: string;
  datasets: string[];
  fixed: Record<string, string>;
  grid: Record<string, string>;
  entry_rule: string;
  exit_rule: string;
  horizon_bars: string;
  features: string;
  risks: string;
  invalidation: string;
  benchmark: string;
  train_bars: string;
  test_bars: string;
  embargo_bars: string;
  holdout_start: string;
  metric: string;
  min_value: string;
  min_trades: string;
  max_drawdown: string;
  dsr_min: string;
  cost_multiplier_survive: string;
  anomaly: string;
}

const EMPTY: Form = {
  title: "",
  premise: "",
  mechanism: "",
  strategy: "event",
  datasets: [],
  fixed: {},
  grid: {},
  entry_rule: "",
  exit_rule: "",
  horizon_bars: "5",
  features: "",
  risks: "",
  invalidation: "",
  benchmark: "cash",
  train_bars: "500",
  test_bars: "125",
  embargo_bars: "10",
  holdout_start: "",
  metric: "sharpe_net",
  min_value: "0.5",
  min_trades: "30",
  max_drawdown: "0.35",
  dsr_min: "0.9",
  cost_multiplier_survive: "2",
  anomaly: "",
};

const FIELD_LABELS: Record<string, string> = {
  title: "Title",
  premise: "Economic / behavioural premise",
  mechanism: "Proposed mechanism",
  dataset_ids: "Datasets",
  entry_rule: "Entry rule",
  exit_rule: "Exit rule",
  features: "Features",
  horizon_bars: "Horizon (bars)",
  risks: "Risks",
  invalidation: "Invalidation",
  holdout_start: "Holdout starts",
  train_bars: "train_bars",
  test_bars: "test_bars",
  embargo_bars: "embargo_bars",
};

/** Missing or too-short fields, checked before sending (the API applies the same rules). */
export function contractProblems(f: {
  title: string; premise: string; mechanism: string; datasets: string[]; entry_rule: string; exit_rule: string;
  features: string; risks: string; invalidation: string; holdout_start: string;
}): string[] {
  const out: string[] = [];
  const lines = (v: string) => v.split("\n").map((x) => x.trim()).filter(Boolean);
  if (f.title.trim().length < 5) out.push("Title: at least 5 characters.");
  if (f.premise.trim().length < 40) out.push(`Economic / behavioural premise: at least 40 characters (now ${f.premise.trim().length}).`);
  if (f.mechanism.trim().length < 40) out.push(`Proposed mechanism: at least 40 characters (now ${f.mechanism.trim().length}).`);
  if (f.datasets.length === 0) out.push("Datasets: choose at least one.");
  if (!f.features.split(",").some((x) => x.trim())) out.push("Features: name at least one (comma separated).");
  if (f.entry_rule.trim().length < 10) out.push("Entry rule: at least 10 characters.");
  if (f.exit_rule.trim().length < 10) out.push("Exit rule: at least 10 characters.");
  if (lines(f.risks).length === 0) out.push("Risks: write at least one (one per line).");
  if (lines(f.invalidation).length === 0) out.push("Invalidation: write at least one condition that would falsify it.");
  if (!f.holdout_start) out.push("Holdout starts: pick the date from which data is kept aside for the final test.");
  return out;
}

/** Turn a FastAPI 422 body ("422: [{loc, msg}, …]") into one readable line per field. */
export function readableApiError(message: string): string {
  const m = message.match(/^422: (\[.*\])$/s);
  if (!m) return message;
  try {
    const items = JSON.parse(m[1]) as { loc?: (string | number)[]; msg?: string }[];
    return items
      .map((i) => {
        const key = String(i.loc?.[i.loc.length - 1] ?? "");
        return `${FIELD_LABELS[key] ?? key}: ${(i.msg ?? "").replace(/^Value error, /, "")}`;
      })
      .join("\n");
  } catch {
    return message;
  }
}

function NewHypothesis({ strategies, datasets, onCreated }: { strategies: StrategyInfo[]; datasets: DatasetRow[]; onCreated: (h: Hypothesis) => void }) {
  const [params] = useSearchParams();
  const [f, setF] = useState<Form>(EMPTY);
  const [errors, setErrors] = useState<string | null>(null);
  const strat = strategies.find((s) => s.name === f.strategy);
  const set = (k: keyof Form) => (e: { target: { value: string } }) => setF((x) => ({ ...x, [k]: e.target.value }));

  useEffect(() => {
    const aid = params.get("anomaly");
    if (!aid) return;
    api.anomaly(aid).then((d) => {
      const a = d.anomaly;
      const single = !a.related_dataset_id;
      setF((x) => ({
        ...x,
        anomaly: a.id,
        title: `${a.symbol}${a.related_symbol ? "/" + a.related_symbol : ""}: ${a.anomaly_type} follow-up`,
        strategy: single ? "event" : "pair_mean_reversion",
        datasets: single ? [a.dataset_id] : [a.dataset_id, a.related_dataset_id!],
        fixed: (single ? { detector: a.detector, threshold: String(d.experiment.detector_params.threshold ?? "") } : {}) as Record<string, string>,
        grid: (single ? { mode: "reversal, continuation", hold_bars: "3, 5, 10" } : { entry_z: "1.5, 2, 2.5" }) as Record<string, string>,
        features: single ? `${a.detector} statistic` : "rolling OLS spread z-score",
      }));
    }, () => undefined);
  }, [params]);

  const submit = async () => {
    setErrors(null);
    const problems = contractProblems(f);
    if (problems.length) {
      setErrors(problems.join("\n"));
      return;
    }
    const fixed = Object.fromEntries(Object.entries(f.fixed).filter(([, v]) => v !== "").map(([k, v]) => [k, parseList(v)[0]]));
    const grid = Object.fromEntries(Object.entries(f.grid).filter(([, v]) => v.trim() !== "").map(([k, v]) => [k, parseList(v)]));
    const contract = {
      title: f.title,
      premise: f.premise,
      mechanism: f.mechanism,
      source_anomaly_ids: f.anomaly ? [f.anomaly] : [],
      dataset_ids: f.datasets,
      features: f.features.split(",").map((s) => s.trim()).filter(Boolean),
      strategy: { type: f.strategy, params: fixed, param_grid: grid },
      entry_rule: f.entry_rule,
      exit_rule: f.exit_rule,
      horizon_bars: Number(f.horizon_bars),
      benchmark: f.benchmark,
      risks: f.risks.split("\n").map((s) => s.trim()).filter(Boolean),
      invalidation: f.invalidation.split("\n").map((s) => s.trim()).filter(Boolean),
      success: {
        metric: f.metric,
        min_value: Number(f.min_value),
        min_trades: Number(f.min_trades),
        max_drawdown: Number(f.max_drawdown),
        dsr_min: Number(f.dsr_min),
        cost_multiplier_survive: Number(f.cost_multiplier_survive),
      },
      validation: {
        train_bars: Number(f.train_bars),
        test_bars: Number(f.test_bars),
        embargo_bars: Number(f.embargo_bars),
        holdout_start: f.holdout_start ? `${f.holdout_start}T00:00:00Z` : undefined,
      },
    };
    try {
      onCreated(await research.create(contract));
      setF(EMPTY);
    } catch (e) {
      setErrors(readableApiError((e as Error).message));
    }
  };

  const area = "input min-h-16 w-full";
  return (
    <Section title="New hypothesis (the contract is frozen when saved)">
      <div className="grid gap-3 md:grid-cols-2">
        <label className="flex flex-col gap-1 text-xs text-slate-500">Title<input className="input" value={f.title} onChange={set("title")} /></label>
        <label className="flex flex-col gap-1 text-xs text-slate-500">
          Strategy
          <select className="input" value={f.strategy} onChange={(e) => setF((x) => ({ ...x, strategy: e.target.value, fixed: {}, grid: {}, datasets: [] }))}>
            {strategies.map((s) => <option key={s.name}>{s.name}</option>)}
          </select>
          <span>{strat?.description}</span>
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-500">Economic / behavioural premise (≥ 40 chars)<textarea className={area} value={f.premise} onChange={set("premise")} /></label>
        <label className="flex flex-col gap-1 text-xs text-slate-500">Proposed mechanism (≥ 40 chars)<textarea className={area} value={f.mechanism} onChange={set("mechanism")} /></label>
        <div className="flex flex-col gap-1 text-xs text-slate-500">
          Universe ({strat?.n_assets ?? 1} dataset{(strat?.n_assets ?? 1) > 1 ? "s, Y first" : ""})
          <div className="flex flex-wrap gap-2">
            {datasets.map((d) => (
              <label key={d.id} className="flex items-center gap-1 text-sm text-slate-800 dark:text-slate-200">
                <input type="checkbox" checked={f.datasets.includes(d.id)} onChange={(e) => setF((x) => ({ ...x, datasets: e.target.checked ? [...x.datasets, d.id] : x.datasets.filter((i) => i !== d.id) }))} />
                {d.symbol} {d.is_synthetic && <SyntheticBadge />}
              </label>
            ))}
          </div>
        </div>
        <div className="flex flex-col gap-1 text-xs text-slate-500">
          Parameters: fixed value, or comma-separated values to explore (each variant is a counted trial)
          <div className="flex flex-wrap gap-2">
            {Object.entries(strat?.defaults ?? {}).map(([k, v]) => (
              <div key={k} className="flex flex-col">
                <span>{k} (default {String(v)})</span>
                <input className="input w-36" placeholder="fixed" value={f.fixed[k] ?? ""} onChange={(e) => setF((x) => ({ ...x, fixed: { ...x.fixed, [k]: e.target.value } }))} />
                <input className="input w-36" placeholder="grid: a, b, c" value={f.grid[k] ?? ""} onChange={(e) => setF((x) => ({ ...x, grid: { ...x.grid, [k]: e.target.value } }))} />
              </div>
            ))}
          </div>
        </div>
        <label className="flex flex-col gap-1 text-xs text-slate-500">Entry rule<input className="input" value={f.entry_rule} onChange={set("entry_rule")} /></label>
        <label className="flex flex-col gap-1 text-xs text-slate-500">Exit rule<input className="input" value={f.exit_rule} onChange={set("exit_rule")} /></label>
        <label className="flex flex-col gap-1 text-xs text-slate-500">Features (comma separated)<input className="input" value={f.features} onChange={set("features")} /></label>
        <div className="flex gap-2">
          <label className="flex flex-col gap-1 text-xs text-slate-500">Horizon (bars)<input className="input w-24" value={f.horizon_bars} onChange={set("horizon_bars")} /></label>
          <label className="flex flex-col gap-1 text-xs text-slate-500">
            Benchmark
            <select className="input" value={f.benchmark} onChange={set("benchmark")}><option>cash</option><option>buy_and_hold</option></select>
          </label>
        </div>
        <label className="flex flex-col gap-1 text-xs text-slate-500">Risks (one per line)<textarea className={area} value={f.risks} onChange={set("risks")} /></label>
        <label className="flex flex-col gap-1 text-xs text-slate-500">Invalidation: what would falsify it (one per line)<textarea className={area} value={f.invalidation} onChange={set("invalidation")} /></label>
      </div>
      <h3 className="mt-4 text-xs font-semibold uppercase text-slate-500">Success criteria, fixed before any result</h3>
      <div className="mt-1 flex flex-wrap gap-2">
        <label className="flex flex-col text-xs text-slate-500">Metric<select className="input" value={f.metric} onChange={set("metric")}><option>sharpe_net</option><option>mean_net</option><option>profit_factor</option><option>hit_rate</option></select></label>
        {(["min_value", "min_trades", "max_drawdown", "dsr_min", "cost_multiplier_survive"] as const).map((k) => (
          <label key={k} className="flex flex-col text-xs text-slate-500">{k}<input className="input w-24" value={f[k]} onChange={set(k)} /></label>
        ))}
      </div>
      <h3 className="mt-4 text-xs font-semibold uppercase text-slate-500">Out-of-sample design</h3>
      <div className="mt-1 flex flex-wrap gap-2">
        {(["train_bars", "test_bars", "embargo_bars"] as const).map((k) => (
          <label key={k} className="flex flex-col text-xs text-slate-500">{k}<input className="input w-24" value={f[k]} onChange={set(k)} /></label>
        ))}
        <label className="flex flex-col text-xs text-slate-500">Holdout starts (final test, isolated)<input className="input" type="date" value={f.holdout_start} onChange={set("holdout_start")} /></label>
      </div>
      <div className="mt-4 flex items-center gap-3">
        <button className="btn" onClick={submit}>Register hypothesis</button>
        <span className="text-xs text-slate-500">Vague wording is rejected; embargo must cover the horizon.</span>
      </div>
      {errors && (
        <div role="alert" className="mt-2 rounded border border-red-300 bg-red-50 p-3 text-sm text-red-800 dark:border-red-800 dark:bg-red-950 dark:text-red-200">
          <p className="font-semibold">The hypothesis was not saved. Fix these fields:</p>
          <ul className="mt-1 list-disc pl-5">{errors.split("\n").map((e) => <li key={e}>{e}</li>)}</ul>
        </div>
      )}
    </Section>
  );
}

export default function ResearchPage() {
  const [hyps, setHyps] = useState<Hypothesis[]>([]);
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [datasets, setDatasets] = useState<DatasetRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();
  useEffect(() => {
    research.hypotheses().then(setHyps, (e) => setError(e.message));
    research.strategies().then(setStrategies, () => undefined);
    research.datasets().then(setDatasets, () => undefined);
  }, []);
  const sorted = useMemo(() => hyps, [hyps]);
  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-lg font-semibold">Quant Research Lab</h1>
      <ErrorBox error={error} />
      <Section title="Hypotheses">
        {sorted.length === 0 && <p className="text-sm text-slate-500">No hypotheses yet. Open an anomaly and choose “Investigate”, or fill in the form below.</p>}
        <table className="min-w-full">
          <tbody>
            {sorted.map((h) => (
              <tr key={h.id} className="cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-800/50" onClick={() => navigate(`/research/${h.id}`)}>
                <td className="td font-medium"><Link to={`/research/${h.id}`}>{h.title}</Link></td>
                <td className="td text-xs">v{h.version}</td>
                <td className="td text-xs">{h.contract.strategy?.type}</td>
                <td className="td"><StatusBadge status={h.status} /></td>
                <td className="td text-xs">{fmtTs(h.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>
      <NewHypothesis strategies={strategies} datasets={datasets} onCreated={(h) => navigate(`/research/${h.id}`)} />
    </div>
  );
}

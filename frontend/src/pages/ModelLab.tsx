import { useCallback, useEffect, useMemo, useState } from "react";
import { CartesianGrid, ComposedChart, Line, ResponsiveContainer, Scatter, Tooltip, XAxis, YAxis } from "recharts";
import {
  models as api,
  research,
  type DatasetRow,
  type FeatureInfo,
  type Job,
  type ModelComparison,
  type ModelComparisonSummary,
  type ModelDataset,
  type ModelDetector,
  type ModelRow,
  type Overlap,
} from "../api";
import { Badge, ErrorBox, Section, SyntheticBadge, fmt, fmtTs } from "../components/ui";
import { chartColors } from "../components/theme";

const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(2)}%`);
const VERDICT_TONE: Record<string, "green" | "red" | "amber" | "slate"> = {
  oos_supported: "green",
  in_sample_only: "amber",
  no_evidence: "red",
  insufficient_sample: "slate",
};

function Verdict({ row }: { row: ModelRow }) {
  const p = row.predictive;
  if (!p) return <span className="text-xs text-slate-500">not evaluated</span>;
  if (p.error) return <Badge tone="red">error</Badge>;
  const o = p.out_of_sample ?? {};
  return (
    <div className="text-xs">
      <Badge tone={VERDICT_TONE[p.verdict ?? ""] ?? "slate"}>{(p.verdict ?? "").replace(/_/g, " ")}</Badge>
      {o.n ? (
        <div>
          OOS n={o.n}, excess {fmt(o.mean_excess_bps)} bps
          {p.oos_run_wide_adjusted_pvalue !== undefined && <>, adj. p {fmt(p.oos_run_wide_adjusted_pvalue)}</>}
        </div>
      ) : null}
    </div>
  );
}

function seedStability(r: ModelRow) {
  const s = r.stability?.seeds;
  if (!s) return "—";
  return s.note ? "deterministic" : fmt(s.jaccard_mean);
}

function windowStability(r: ModelRow) {
  const js = (r.stability?.window_variants ?? []).map((v) => v.jaccard).filter((x): x is number => typeof x === "number");
  return js.length ? fmt(Math.min(...js)) : "—";
}

function ComparisonTable({ ds }: { ds: ModelDataset }) {
  const head = ["Model", "Flagged bars", "Episodes", "Flag rate (nominal)", "BH-significant", "High-vol / low-vol rate", "Year-to-year CV", "Seed stability", "Window stability", "Runtime s", "Out-of-sample evidence"];
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full text-sm">
        <thead>
          <tr>{head.map((h) => <th key={h} className="th">{h}</th>)}</tr>
        </thead>
        <tbody>
          {(ds.rows ?? []).map((r) =>
            r.status !== "ok" ? (
              <tr key={r.model}>
                <td className="td font-medium">{r.model}</td>
                <td className="td text-xs text-amber-700 dark:text-amber-300" colSpan={head.length - 1}>
                  {r.status.replace("_", " ")}: {r.reason ?? r.error}
                </td>
              </tr>
            ) : (
              <tr key={r.model}>
                <td className="td font-medium">{r.model}</td>
                <td className="td">{r.n_flagged}</td>
                <td className="td">{r.n_episodes}</td>
                <td className="td">{pct(r.flag_rate)}{r.nominal_rate ? <span className="text-xs text-slate-500"> ({pct(r.nominal_rate)})</span> : null}</td>
                <td className="td">{r.n_bh_significant}</td>
                <td className="td">{pct(r.per_regime?.high_vol.rate)} / {pct(r.per_regime?.low_vol.rate)}</td>
                <td className="td">{fmt(r.rate_cv_across_years)}</td>
                <td className="td">{seedStability(r)}</td>
                <td className="td">{windowStability(r)}</td>
                <td className="td">{fmt(r.runtime_s)}</td>
                <td className="td"><Verdict row={r} /></td>
              </tr>
            ),
          )}
        </tbody>
      </table>
    </div>
  );
}

function OverlapMatrix({ ov }: { ov: NonNullable<ModelDataset["overlap"]> }) {
  if (ov.models.length < 2) return null;
  return (
    <div className="overflow-x-auto">
      <table className="text-sm">
        <thead>
          <tr>
            <th className="th">share of row's flags also flagged by →</th>
            {ov.models.map((m) => <th key={m} className="th">{m}</th>)}
          </tr>
        </thead>
        <tbody>
          {ov.models.map((m, i) => (
            <tr key={m}>
              <td className="td font-medium">{m}</td>
              {ov.matrix[i].map((o: Overlap | null, j) => (
                <td key={j} className="td">{o ? pct(o.share_a_in_b) : "—"}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function FlagChart({ ds, model }: { ds: ModelDataset; model: ModelRow | undefined }) {
  const c = chartColors();
  const price = useMemo(() => (ds.chart ? ds.chart.ts.map((t, i) => ({ x: Date.parse(t), close: ds.chart!.close[i] })) : []), [ds]);
  const flags = (model?.flags ?? []).map((f) => ({ x: Date.parse(f.ts), close: f.close }));
  if (!price.length) return null;
  return (
    <div className="h-72">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={c.grid} vertical={false} />
          <XAxis dataKey="x" type="number" domain={["dataMin", "dataMax"]} scale="time" tick={{ fontSize: 11, fill: c.axis }}
            tickFormatter={(v: number) => new Date(v).toISOString().slice(0, 10)} minTickGap={50} />
          <YAxis dataKey="close" domain={["auto", "auto"]} tick={{ fontSize: 11, fill: c.axis }} width={56} />
          <Tooltip labelFormatter={(v: number) => new Date(v).toISOString().slice(0, 16).replace("T", " ")} formatter={(v: number) => fmt(v)} />
          <Line data={price} dataKey="close" name="close" stroke={c.muted} strokeWidth={1.5} dot={false} isAnimationActive={false} />
          <Scatter data={flags} dataKey="close" name="flagged" fill={c.secondary} isAnimationActive={false} />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

function DatasetResult({ ds }: { ds: ModelDataset }) {
  const ok = (ds.rows ?? []).filter((r) => r.status === "ok");
  const [sel, setSel] = useState(ok[0]?.model ?? "");
  const row = ok.find((r) => r.model === sel);
  if (ds.status !== "ok") {
    return (
      <p className="text-sm text-amber-700 dark:text-amber-300">
        {ds.symbol} {ds.frequency}: skipped ({ds.reason})
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-3">
      <h3 className="font-medium">
        {ds.symbol} {ds.frequency} <span className="text-xs text-slate-500">{ds.n_bars} bars · quality {ds.quality_status}</span>
        {ds.is_synthetic && <> <SyntheticBadge /></>}
        {ds.truncated_to_last_bars && <span className="text-xs text-amber-700"> (last {ds.truncated_to_last_bars} bars only)</span>}
      </h3>
      <ComparisonTable ds={ds} />
      {ds.overlap && <OverlapMatrix ov={ds.overlap} />}
      {ok.length > 0 && (
        <>
          <label className="flex items-center gap-2 text-sm">
            Show flags of
            <select aria-label="Model to plot" className="input" value={sel} onChange={(e) => setSel(e.target.value)}>
              {ok.map((r) => <option key={r.model} value={r.model}>{r.model}</option>)}
            </select>
          </label>
          <FlagChart ds={ds} model={row} />
          {row && (
            <div className="grid gap-3 text-sm md:grid-cols-3">
              <div>
                <div className="font-medium">Flag rate by year</div>
                <ul className="text-xs">
                  {(row.per_year ?? []).map((y) => <li key={y.year}>{y.year}: {pct(y.rate)} ({y.flags}/{y.bars})</li>)}
                </ul>
              </div>
              <div>
                <div className="font-medium">Stability</div>
                <ul className="text-xs">
                  {(row.stability?.seeds.runs ?? []).map((s) => <li key={s.seed}>seed {s.seed}: overlap {fmt(s.jaccard)}</li>)}
                  {row.stability?.seeds.note && <li>{row.stability.seeds.note}</li>}
                  {(row.stability?.window_variants ?? []).map((v) => (
                    <li key={v.variant}>{v.variant}: {v.not_applicable ? v.not_applicable : `overlap ${fmt(v.jaccard)} (${v.n_flagged} flags)`}</li>
                  ))}
                </ul>
              </div>
              <div>
                <div className="font-medium">Most unusual feature at flagged bars</div>
                <ul className="text-xs">
                  {Object.entries(row.top_feature_counts ?? {}).map(([f, n]) => <li key={f}>{f}: {n}</li>)}
                  {Object.keys(row.top_feature_counts ?? {}).length === 0 && <li>univariate detector</li>}
                  {Object.entries(row.features_dropped ?? {}).map(([f, why]) => <li key={f} className="text-amber-700 dark:text-amber-300">{f} not used: {why}</li>)}
                </ul>
                {row.predictive?.verdict_text && <p className="mt-1 text-xs">{row.predictive.verdict_text}</p>}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function Benchmark({ b }: { b: NonNullable<ModelComparison["results"]["synthetic_benchmark"]> }) {
  if (b.skipped) return <p className="text-sm">Synthetic benchmark skipped ({b.skipped}).</p>;
  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs text-slate-500"><SyntheticBadge /> {b.note}</p>
      <table className="min-w-full text-sm">
        <thead>
          <tr>{["Model", "Recall (scored events)", "Injected events", "False alarms on control", "Nominal"].map((h) => <th key={h} className="th">{h}</th>)}</tr>
        </thead>
        <tbody>
          {(b.rows ?? []).map((r) => (
            <tr key={r.model}>
              <td className="td font-medium">{r.model}</td>
              <td className="td">{pct(r.recall_scored)}</td>
              <td className="td text-xs">
                {r.events.map((e) => (
                  <div key={`${e.symbol}-${e.bar}`}>
                    {e.detected ? "✓" : e.scored ? "✗" : "–"} {e.kind} ({e.symbol} bar {e.bar}){e.delay_bars ? `, +${e.delay_bars} bars` : ""}
                    {!e.scored && " · in warm-up"}
                  </div>
                ))}
              </td>
              <td className="td">{r.control_false_alarms.not_applicable ?? `${pct(r.control_false_alarms.rate)} (${r.control_false_alarms.episodes} episodes)`}</td>
              <td className="td">{pct(r.nominal_rate)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function FeatureCatalog({ features }: { features: FeatureInfo[] }) {
  return (
    <details>
      <summary className="cursor-pointer text-sm">Feature catalog ({features.length})</summary>
      <table className="mt-2 min-w-full text-xs">
        <thead>
          <tr>{["Feature", "Formula", "Window", "Availability", "Missing values", "Assumptions", "Biases"].map((h) => <th key={h} className="th">{h}</th>)}</tr>
        </thead>
        <tbody>
          {features.map((f) => (
            <tr key={f.name}>
              <td className="td font-mono">{f.name}</td>
              <td className="td">{f.formula}</td>
              <td className="td">{f.window}</td>
              <td className="td">{f.availability}</td>
              <td className="td">{f.missing}</td>
              <td className="td">{f.assumptions}</td>
              <td className="td">{f.biases}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}

const ACTIVE = new Set(["queued", "running", "cancelling"]);

export default function ModelLabPage() {
  const [detectors, setDetectors] = useState<ModelDetector[]>([]);
  const [features, setFeatures] = useState<FeatureInfo[]>([]);
  const [defaultFeatures, setDefaultFeatures] = useState<string[]>([]);
  const [datasets, setDatasets] = useState<DatasetRow[]>([]);
  const [runs, setRuns] = useState<ModelComparisonSummary[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [selected, setSelected] = useState<ModelComparison | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [synthetic, setSynthetic] = useState(false);
  const [form, setForm] = useState({
    datasets: [] as string[],
    models: ["isolation_forest", "local_outlier_factor", "return_shock", "volatility_regime"],
    features: [] as string[],
    contamination: 0.01,
    trainWindow: 1500,
    seeds: "0,1,2",
    horizons: "1,5,20",
    predictive: true,
    benchmark: false,
    maxSeconds: 1800,
    label: "",
  });

  useEffect(() => {
    api.detectors().then(setDetectors, (e) => setError(e.message));
    api.features().then((f) => {
      setFeatures(f.features);
      setDefaultFeatures(f.default);
      setForm((x) => ({ ...x, features: f.default }));
    }, () => undefined);
    research.datasets().then(setDatasets, () => undefined);
  }, []);

  const load = useCallback(() => {
    api.experiments().then(setRuns, (e) => setError(e.message));
    api.jobs().then((j) => setJobs(j.filter((x) => x.kind === "model_experiment" && ACTIVE.has(x.status))), () => undefined);
  }, []);
  useEffect(() => {
    load();
    const t = window.setInterval(load, 4000);
    return () => window.clearInterval(t);
  }, [load]);

  const open = (id: string) => api.experiment(id).then(setSelected, (e) => setError(e.message));
  const toggle = (list: string[], v: string) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);
  const shown = datasets.filter((d) => d.is_synthetic === synthetic);
  const ints = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean).map(Number).filter(Number.isInteger);

  const start = async () => {
    setError(null);
    setNotice(null);
    const byName = Object.fromEntries(detectors.map((d) => [d.name, d]));
    const body = {
      dataset_ids: form.datasets.filter((id) => shown.some((d) => d.id === id)),
      models: form.models.map((name) => ({
        detector: name,
        params: byName[name]?.multivariate
          ? { features: form.features, contamination: form.contamination, train_window: form.trainWindow }
          : {},
      })),
      seeds: ints(form.seeds),
      horizons: ints(form.horizons).filter((h) => h > 0),
      predictive: form.predictive,
      benchmark: form.benchmark,
      max_seconds: form.maxSeconds,
      label: form.label,
    };
    try {
      await api.run(body);
      setNotice("Comparison queued. It appears below when it finishes; you can cancel it while it runs.");
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-lg font-semibold">Model Lab</h1>
        <p className="max-w-4xl text-sm text-slate-600 dark:text-slate-400">
          Compare anomaly models on the same data. Rarity (how many bars a model flags, and how stable that is) and
          predictive value (whether its events anticipated returns out of sample, after a chronological split) are
          shown in separate columns, with no combined score. A more complex model is not assumed to be better.
        </p>
      </div>
      <ErrorBox error={error} />
      {notice && <div className="text-sm text-indigo-700 dark:text-indigo-300">{notice}</div>}

      <Section title="Configure a comparison">
        <div className="grid gap-4 text-sm lg:grid-cols-3">
          <div>
            <div className="mb-1 flex items-center gap-2 font-medium">
              Datasets
              <label className="flex items-center gap-1 text-xs font-normal">
                <input type="checkbox" checked={synthetic} onChange={(e) => setSynthetic(e.target.checked)} />
                synthetic
              </label>
            </div>
            <div className="max-h-56 overflow-y-auto">
              {shown.map((d) => (
                <label key={d.id} className="flex items-center gap-1">
                  <input type="checkbox" checked={form.datasets.includes(d.id)} onChange={() => setForm({ ...form, datasets: toggle(form.datasets, d.id) })} />
                  {d.symbol} {d.frequency} <span className="text-xs text-slate-500">{d.rows} bars</span>
                </label>
              ))}
              {shown.length === 0 && <p className="text-xs text-slate-500">No {synthetic ? "synthetic" : "real"} datasets.</p>}
            </div>
          </div>
          <div>
            <div className="mb-1 font-medium">Models</div>
            {detectors.map((d) => (
              <label key={d.name} className="flex items-start gap-1" title={d.description}>
                <input type="checkbox" className="mt-1" checked={form.models.includes(d.name)} onChange={() => setForm({ ...form, models: toggle(form.models, d.name) })} />
                <span>
                  {d.name} {d.multivariate ? <Badge tone="indigo">multivariate</Badge> : <Badge>baseline</Badge>}
                </span>
              </label>
            ))}
            <div className="mt-2 flex flex-wrap gap-2">
              <label className="flex flex-col">
                Contamination
                <input aria-label="Contamination" className="input w-24" type="number" step={0.005} min={0.001} max={0.1} value={form.contamination}
                  onChange={(e) => setForm({ ...form, contamination: Number(e.target.value) })} />
              </label>
              <label className="flex flex-col">
                Training window (bars)
                <input aria-label="Training window" className="input w-28" type="number" min={300} value={form.trainWindow}
                  onChange={(e) => setForm({ ...form, trainWindow: Number(e.target.value) })} />
              </label>
            </div>
          </div>
          <div>
            <div className="mb-1 font-medium">Features (multivariate models)</div>
            <div className="grid max-h-40 grid-cols-2 overflow-y-auto text-xs">
              {features.map((f) => (
                <label key={f.name} className="flex items-center gap-1" title={f.formula}>
                  <input type="checkbox" checked={form.features.includes(f.name)} onChange={() => setForm({ ...form, features: toggle(form.features, f.name) })} />
                  {f.name}{defaultFeatures.includes(f.name) ? "" : " *"}
                </label>
              ))}
            </div>
            <div className="mt-2 flex flex-wrap items-end gap-2">
              <label className="flex flex-col">Seeds<input aria-label="Seeds" className="input w-20" value={form.seeds} onChange={(e) => setForm({ ...form, seeds: e.target.value })} /></label>
              <label className="flex flex-col">Horizons<input aria-label="Horizons" className="input w-20" value={form.horizons} onChange={(e) => setForm({ ...form, horizons: e.target.value })} /></label>
              <label className="flex flex-col">Time budget s<input aria-label="Time budget" className="input w-24" type="number" value={form.maxSeconds} onChange={(e) => setForm({ ...form, maxSeconds: Number(e.target.value) })} /></label>
              <label className="flex flex-col">Label<input aria-label="Label" className="input w-32" value={form.label} onChange={(e) => setForm({ ...form, label: e.target.value })} /></label>
            </div>
            <label className="mt-2 flex items-center gap-1"><input type="checkbox" checked={form.predictive} onChange={(e) => setForm({ ...form, predictive: e.target.checked })} />evaluate out-of-sample predictive value</label>
            <label className="flex items-center gap-1"><input type="checkbox" checked={form.benchmark} onChange={(e) => setForm({ ...form, benchmark: e.target.checked })} />also run the synthetic known-anomaly benchmark</label>
          </div>
        </div>
        <div className="mt-3 flex items-center gap-3">
          <button className="btn" onClick={start}>Run comparison</button>
          <span className="text-xs text-slate-500">Runs in the background; no orders are sent and no language model is called.</span>
        </div>
      </Section>

      {jobs.length > 0 && (
        <Section title="Running">
          <ul className="space-y-1 text-sm">
            {jobs.map((j) => (
              <li key={j.id} className="flex items-center gap-2">
                <Badge tone="indigo">{j.status}</Badge> started {fmtTs(j.created_at)}
                {j.status !== "cancelling" && (
                  <button className="btn" onClick={() => api.cancel(j.id).then(load, (e) => setError(e.message))}>Cancel</button>
                )}
              </li>
            ))}
          </ul>
        </Section>
      )}

      <Section title="Comparisons">
        <table className="min-w-full text-sm">
          <thead>
            <tr>{["Started", "Status", "Label", "Models", "Datasets", "Seconds", ""].map((h) => <th key={h} className="th">{h}</th>)}</tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.id}>
                <td className="td text-xs">{fmtTs(r.created_at)}</td>
                <td className="td">
                  <Badge tone={r.status === "succeeded" ? "green" : r.status === "failed" ? "red" : "slate"}>{r.status}</Badge>
                  {r.budget_exhausted && <Badge tone="amber">budget reached</Badge>}
                  {r.is_synthetic && <> <SyntheticBadge /></>}
                </td>
                <td className="td">{r.label}</td>
                <td className="td text-xs">{r.models.join(", ")}</td>
                <td className="td text-xs">{r.datasets.join(", ") || (r.benchmark ? "synthetic benchmark" : "")}</td>
                <td className="td">{fmt(r.duration_s)}</td>
                <td className="td"><button className="btn" onClick={() => open(r.id)}>Open</button></td>
              </tr>
            ))}
          </tbody>
        </table>
        {runs.length === 0 && <p className="text-sm text-slate-500">No comparison yet.</p>}
      </Section>

      {selected && (
        <Section
          title={`Comparison ${selected.label || selected.id.slice(0, 8)}`}
          right={
            <span className="flex gap-3 text-sm">
              <a className="underline" href={api.csvUrl(selected.id)}>Export CSV</a>
              <a className="underline" href={api.jsonUrl(selected.id)} target="_blank" rel="noreferrer">Full record (JSON)</a>
            </span>
          }
        >
          <div className="flex flex-col gap-6">
            {selected.status === "cancelled" && <p className="text-sm text-amber-700">Cancelled: partial results are kept.</p>}
            {selected.results.budget_exhausted && <p className="text-sm text-amber-700">The time budget ran out; skipped datasets are listed.</p>}
            {selected.results.datasets.map((ds) => <DatasetResult key={ds.dataset_id} ds={ds} />)}
            {selected.results.synthetic_benchmark && (
              <div>
                <h3 className="mb-1 font-medium">Synthetic known-anomaly benchmark</h3>
                <Benchmark b={selected.results.synthetic_benchmark} />
              </div>
            )}
            <div>
              <h3 className="mb-1 font-medium">Limitations</h3>
              <ul className="list-disc pl-5 text-sm">{selected.results.limitations.map((l) => <li key={l}>{l}</li>)}</ul>
            </div>
          </div>
        </Section>
      )}

      <Section title="Features">
        <FeatureCatalog features={features} />
      </Section>
    </div>
  );
}

import { useCallback, useEffect, useMemo, useState } from "react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import {
  lab as api,
  type LabCondition,
  type LabDecision,
  type LabExperiment,
  type LabExperimentSummary,
  type LabGenerated,
  type LabMeta,
  type LabRanking,
} from "../api";
import { Badge, ErrorBox, KV, Section, SyntheticBadge, fmt, fmtTs } from "../components/ui";
import { chartColors } from "../components/theme";

const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(2)}%`);
const DECISION_TONE: Record<string, "green" | "red" | "amber" | "slate" | "indigo"> = {
  PAPER_ELIGIBLE: "green",
  OOS_SUPPORTED: "indigo",
  NEEDS_RESEARCH: "amber",
  INCONCLUSIVE: "slate",
  REJECTED: "red",
};
const STATUS_TONE: Record<string, "green" | "red" | "amber" | "slate" | "indigo"> = {
  COMPLETED: "green",
  RUNNING: "indigo",
  QUEUED: "slate",
  FAILED: "red",
  INVALID: "red",
  CANCELLED: "amber",
};
const ACTIVE = ["QUEUED", "RUNNING"];

export function Decision({ d }: { d: string | null | undefined }) {
  if (!d) return <span className="text-xs text-slate-500">—</span>;
  return <Badge tone={DECISION_TONE[d] ?? "slate"}>{d.replace(/_/g, " ")}</Badge>;
}

function Warnings({ items }: { items: string[] }) {
  if (!items.length) return null;
  return (
    <ul className="text-xs text-amber-700 dark:text-amber-300">
      {items.map((w) => <li key={w}>⚠ {w}</li>)}
    </ul>
  );
}

// -- create / import -------------------------------------------------------------------------------
function NewExperiments({ meta, onQueued }: { meta: LabMeta; onQueued: () => void }) {
  const [families, setFamilies] = useState<string[]>(Object.keys(meta.families));
  const [gen, setGen] = useState<LabGenerated | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [specText, setSpecText] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const generate = async () => {
    setError(null);
    setMsg(null);
    try {
      const g = await api.generate(families, 30);
      setGen(g);
      setPicked(new Set(g.proposed.map((p) => p.generator_key)));
    } catch (e) {
      setError(String(e));
    }
  };

  const queueSelected = async () => {
    if (!gen) return;
    setBusy(true);
    setError(null);
    let n = 0;
    try {
      for (const p of gen.proposed.filter((x) => picked.has(x.generator_key))) {
        await api.submit(p.spec, p.generator_key);
        n += 1;
      }
      setMsg(`${n} experiments queued`);
      setGen(null);
      onQueued();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const importSpec = async () => {
    setError(null);
    setMsg(null);
    let spec: Record<string, unknown>;
    try {
      spec = JSON.parse(specText);
    } catch {
      setError("The spec is not valid JSON");
      return;
    }
    try {
      const r = await api.submit(spec);
      setMsg(r.created ? "Experiment queued" : `Same spec and data already registered (${r.experiment.status}): no new run`);
      onQueued();
    } catch (e) {
      setError(String(e));
    }
  };

  const nPicked = gen?.proposed.filter((p) => picked.has(p.generator_key)) ?? [];
  return (
    <Section title="New experiments">
      <div className="flex flex-col gap-3 text-sm">
        <div className="flex flex-wrap items-center gap-3">
          {Object.entries(meta.families).map(([k, f]) => (
            <label key={k} className="flex items-center gap-1" title={f.label}>
              <input type="checkbox" checked={families.includes(k)}
                onChange={(e) => setFamilies(e.target.checked ? [...families, k] : families.filter((x) => x !== k))} />
              {k}
            </label>
          ))}
          <button className="btn" onClick={generate} disabled={!families.length}>Generate proposals</button>
        </div>
        {gen && (
          <div className="flex flex-col gap-2">
            <p className="text-xs text-slate-600 dark:text-slate-400">
              {gen.n_specs} proposals, {gen.n_variants} variants; {gen.skipped.length} skipped as near-duplicates or invalid. {gen.note}
            </p>
            <div className="overflow-x-auto">
              <table className="min-w-full text-sm">
                <thead>
                  <tr>{["", "Family", "Hypothesis", "Variants", "Baselines", "Why"].map((h) => <th key={h} className="th">{h}</th>)}</tr>
                </thead>
                <tbody>
                  {gen.proposed.map((p) => (
                    <tr key={p.generator_key}>
                      <td className="td">
                        <input type="checkbox" aria-label={`Pick ${p.title}`} checked={picked.has(p.generator_key)}
                          onChange={(e) => {
                            const s = new Set(picked);
                            if (e.target.checked) s.add(p.generator_key);
                            else s.delete(p.generator_key);
                            setPicked(s);
                          }} />
                      </td>
                      <td className="td">{p.family}</td>
                      <td className="td">{p.title} {p.is_synthetic && <SyntheticBadge />}</td>
                      <td className="td">{p.variants}</td>
                      <td className="td whitespace-normal text-xs">{p.baselines.join(", ")}</td>
                      <td className="td whitespace-normal text-xs">
                        {p.why}{" "}
                        <button className="underline" onClick={() => setSpecText(JSON.stringify(p.spec, null, 2))}>Edit spec</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {gen.skipped.length > 0 && (
              <details className="text-xs">
                <summary>Skipped ({gen.skipped.length})</summary>
                <ul>{gen.skipped.map((s, i) => <li key={i}>{s.family}: {s.title}: {s.reason}</li>)}</ul>
              </details>
            )}
            <div>
              <button className="btn" onClick={queueSelected} disabled={busy || !nPicked.length}>
                Queue {nPicked.length} selected ({nPicked.reduce((a, p) => a + p.variants, 0)} variants)
              </button>
            </div>
          </div>
        )}
        <details>
          <summary className="cursor-pointer">Import or edit a spec (JSON, {meta.spec_version})</summary>
          <textarea aria-label="Experiment spec JSON" className="input mt-2 h-48 w-full font-mono text-xs" value={specText}
            onChange={(e) => setSpecText(e.target.value)} placeholder='{"family": "momentum", "contract": {...}}' />
          <button className="btn mt-2" onClick={importSpec} disabled={!specText.trim()}>Import and queue</button>
        </details>
        {msg && <p className="text-emerald-700 dark:text-emerald-300">{msg}</p>}
        <ErrorBox error={error} />
      </div>
    </Section>
  );
}

// -- queue -----------------------------------------------------------------------------------------------
function Queue({ rows, onOpen, onCancel }: { rows: LabExperimentSummary[]; onOpen: (id: string) => void; onCancel: (id: string) => void }) {
  const head = ["Experiment", "Family", "Universe", "Status", "Stage", "Attempts", "Decision at completion", "Duration s", ""];
  return (
    <Section title={`Queue and history (${rows.length})`}>
      <div className="overflow-x-auto">
        <table className="min-w-full text-sm">
          <thead>
            <tr>{head.map((h) => <th key={h} className="th">{h}</th>)}</tr>
          </thead>
          <tbody>
            {rows.map((e) => (
              <tr key={e.id}>
                <td className="td">
                  {e.title} {e.is_synthetic && <SyntheticBadge />}
                  <div className="text-xs text-slate-500">{fmtTs(e.created_at)} · {e.generated_by} · {e.variants} variants</div>
                  {e.error && <div className="text-xs text-rose-700 dark:text-rose-300">{e.error}</div>}
                </td>
                <td className="td">{e.family}</td>
                <td className="td whitespace-normal text-xs">{e.universe.join(", ")}</td>
                <td className="td"><Badge tone={STATUS_TONE[e.status] ?? "slate"}>{e.status}</Badge></td>
                <td className="td whitespace-normal text-xs">{e.status === "RUNNING" ? e.stage ?? "starting" : "—"}</td>
                <td className="td">{e.attempts}</td>
                <td className="td"><Decision d={e.decision_at_completion} /></td>
                <td className="td">{fmt(e.duration_s)}</td>
                <td className="td whitespace-nowrap">
                  <button className="btn-ghost" onClick={() => onOpen(e.id)}>Open</button>{" "}
                  {ACTIVE.includes(e.status) && <button className="btn-ghost" onClick={() => onCancel(e.id)}>Cancel</button>}
                </td>
              </tr>
            ))}
            {!rows.length && <tr><td className="td text-slate-500" colSpan={head.length}>No experiments yet.</td></tr>}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

// -- ranking -----------------------------------------------------------------------------------------------
function Ranking({ meta, version, onOpen }: { meta: LabMeta; version: number; onOpen: (id: string) => void }) {
  const [includeSyn, setIncludeSyn] = useState(true);
  const [r, setR] = useState<LabRanking | null>(null);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [note, setNote] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pf, setPf] = useState<Record<string, any> | null>(null);

  useEffect(() => {
    api.ranking(includeSyn).then(setR).catch((e) => setError(String(e)));
  }, [includeSyn, version]);

  const record = async () => {
    try {
      const s = await api.recordSelection(note);
      setMsg(`Selection recorded (${s.n_experiments} experiments, id ${s.id.slice(0, 8)}). Nothing was promoted or allocated.`);
    } catch (e) {
      setError(String(e));
    }
  };
  const contribution = async () => {
    setError(null);
    try {
      setPf(await api.portfolio([...sel]));
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <Section title="Ranking" right={<a className="btn-ghost" href={api.rankingCsvUrl(includeSyn)}>Export ranking CSV</a>}>
      <div className="flex flex-col gap-3 text-sm">
        <p className="text-xs text-slate-600 dark:text-slate-400">
          {r?.multiple_testing}. {r?.n_trials_total ?? 0} variants tried in total. Ordered by decision first, then score
          ({meta.selection_version}); a critical control that fails cannot be offset by a high score. {meta.not_a_guarantee}
        </p>
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={includeSyn} onChange={(e) => setIncludeSyn(e.target.checked)} /> include synthetic data
        </label>
        <div className="overflow-x-auto">
          <table className="min-w-full text-sm">
            <thead>
              <tr>
                {["", "#", "Experiment", "Decision", "Score", "Raw p", "Lab-adjusted p", "OOS Sharpe net", "OOS bars", "Trades", "DSR", "Trials", "Baselines beaten", "Reasons and warnings", ""].map((h, i) => (
                  <th key={i} className="th">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {(r?.rows ?? []).map((x) => (
                <tr key={x.experiment_id}>
                  <td className="td">
                    <input type="checkbox" aria-label={`Select ${x.title}`} checked={sel.has(x.experiment_id)}
                      onChange={(e) => {
                        const s = new Set(sel);
                        if (e.target.checked) s.add(x.experiment_id);
                        else s.delete(x.experiment_id);
                        setSel(s);
                      }} />
                  </td>
                  <td className="td">{x.rank}</td>
                  <td className="td">{x.title} {x.is_synthetic && <SyntheticBadge />}</td>
                  <td className="td"><Decision d={x.decision} /></td>
                  <td className="td">{fmt(x.score)}</td>
                  <td className="td">{fmt(x.raw_p)}</td>
                  <td className="td">{fmt(x.adjusted_p)}</td>
                  <td className="td">{fmt(x.oos_sharpe_net)}</td>
                  <td className="td">{x.oos_bars}</td>
                  <td className="td">{x.n_trades}</td>
                  <td className="td">{fmt(x.dsr)}</td>
                  <td className="td">{x.n_trials}</td>
                  <td className="td whitespace-normal text-xs">{x.baselines_beaten.join(", ") || "none"}</td>
                  <td className="td whitespace-normal text-xs">
                    <ul>{x.reasons.map((m) => <li key={m}>{m}</li>)}</ul>
                    <Warnings items={x.warnings} />
                  </td>
                  <td className="td"><button className="btn-ghost" onClick={() => onOpen(x.experiment_id)}>Metrics</button></td>
                </tr>
              ))}
              {r && !r.rows.length && <tr><td className="td text-slate-500" colSpan={15}>No completed experiments.</td></tr>}
            </tbody>
          </table>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button className="btn" onClick={contribution} disabled={sel.size < 2}>Portfolio contribution of {sel.size} selected</button>
          <input className="input" aria-label="Selection note" placeholder="note for the record" value={note} onChange={(e) => setNote(e.target.value)} />
          <button className="btn" onClick={record}>Record selection</button>
        </div>
        {msg && <p className="text-emerald-700 dark:text-emerald-300">{msg}</p>}
        <ErrorBox error={error} />
        {pf && <Contribution pf={pf} />}
      </div>
    </Section>
  );
}

function Contribution({ pf }: { pf: Record<string, any> }) {
  if (pf.not_applicable) return <p className="text-sm text-amber-700 dark:text-amber-300">{pf.not_applicable}</p>;
  const ids: string[] = pf.strategies;
  const row = (name: string, s: Record<string, any>) => (
    <tr key={name}>
      <td className="td">{name}</td>
      <td className="td">{fmt(s.sharpe_ann)}</td>
      <td className="td">{pct(s.total)}</td>
      <td className="td">{pct(s.max_drawdown)}</td>
    </tr>
  );
  return (
    <div className="flex flex-col gap-2 text-sm" aria-label="Portfolio contribution">
      <h3 className="font-medium">Portfolio contribution ({pf.days} days, equal weight)</h3>
      <table className="text-sm">
        <thead><tr>{["", "Sharpe (ann.)", "Total", "Max drawdown"].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
        <tbody>
          {ids.map((i) => row(i.slice(0, 8), pf.individual[i]))}
          {row("combined", pf.combined)}
          {row("cash baseline", pf.baseline_cash)}
        </tbody>
      </table>
      <KV data={{
        "mean pairwise correlation": pf.correlation?.static_mean_pairwise,
        "correlation on stress days": pf.correlation?.stress?.mean_pairwise,
        "correlation high / low volatility": `${fmt(pf.correlation?.regime?.high_vol?.mean_pairwise)} / ${fmt(pf.correlation?.regime?.low_vol?.mean_pairwise)}`,
      }} />
      <div className="text-xs">
        {ids.map((i) => {
          const t = pf.loss_diversification?.[i];
          const c = pf.costs_and_turnover?.[i];
          return (
            <div key={i}>
              {i.slice(0, 8)}: on its worst {t?.days} days it lost {pct(t?.candidate_mean)} per day and the combination {pct(t?.combined_mean)};
              the others also lost on {pct(t?.others_also_lost_frac)} of them. Turnover {fmt(c?.turnover_ann)}/yr, costs {pct(c?.total_cost)}.
            </div>
          );
        })}
      </div>
      <p className="text-xs text-slate-500">{pf.note}</p>
    </div>
  );
}

// -- detail ----------------------------------------------------------------------------------------------
const TABS = ["Decision", "A Integrity", "B Baselines", "C Temporal (IS vs OOS)", "D Robustness", "E Statistics", "Versions"] as const;

function Conditions({ title, items }: { title: string; items: LabCondition[] }) {
  return (
    <div>
      <div className="font-medium capitalize">{title}</div>
      <ul className="text-xs">
        {items.map((c) => (
          <li key={c.condition}>
            {c.met ? "✓" : c.evaluable ? "✗" : "○"} {c.condition}: {c.detail}
          </li>
        ))}
      </ul>
    </div>
  );
}

function DecisionTab({ d }: { d: LabDecision }) {
  return (
    <div className="flex flex-col gap-3 text-sm">
      <div className="flex items-center gap-2"><Decision d={d.decision} /> score {fmt(d.score)} <span className="text-xs text-slate-500">{d.version}</span></div>
      <ul className="text-sm">{d.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
      <Warnings items={d.warnings} />
      {!Array.isArray(d.conditions) && (
        <div className="grid gap-3 md:grid-cols-2">
          {Object.entries(d.conditions).map(([k, v]) => <Conditions key={k} title={k} items={v} />)}
        </div>
      )}
      <table className="text-sm">
        <thead><tr>{["Component", "Value", "Weight", "Source"].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
        <tbody>
          {d.components.map((c) => (
            <tr key={c.name}><td className="td">{c.name}</td><td className="td">{fmt(c.value)}</td><td className="td">{fmt(d.weights?.[c.name])}</td><td className="td whitespace-normal text-xs">{c.source}</td></tr>
          ))}
        </tbody>
      </table>
      {d.note_on_p && <p className="text-xs text-slate-500">{d.note_on_p}</p>}
      <p className="text-xs text-slate-500">{d.note}</p>
    </div>
  );
}

function Table({ rows, cols }: { rows: Record<string, any>[]; cols?: string[] }) {
  const keys = cols ?? Array.from(new Set(rows.flatMap((r) => Object.keys(r))));
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full text-sm">
        <thead><tr>{keys.map((k) => <th key={k} className="th">{k.replace(/_/g, " ")}</th>)}</tr></thead>
        <tbody>
          {rows.map((r, i) => <tr key={i}>{keys.map((k) => <td key={k} className="td text-xs">{fmt(r[k])}</td>)}</tr>)}
        </tbody>
      </table>
    </div>
  );
}

function Curve({ curve }: { curve: { ts: string[]; equity: number[] } }) {
  const c = chartColors();
  const data = curve.ts.map((t, i) => ({ x: Date.parse(t), equity: curve.equity[i] }));
  return (
    <div className="h-56">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={c.grid} vertical={false} />
          <XAxis dataKey="x" type="number" domain={["dataMin", "dataMax"]} scale="time" tick={{ fontSize: 11, fill: c.axis }}
            tickFormatter={(v: number) => new Date(v).toISOString().slice(0, 10)} minTickGap={50} />
          <YAxis domain={["auto", "auto"]} tick={{ fontSize: 11, fill: c.axis }} width={56} />
          <Tooltip labelFormatter={(v: number) => new Date(v).toISOString().slice(0, 10)} formatter={(v: number) => fmt(v)} />
          <Line dataKey="equity" name="OOS net equity (walk-forward)" stroke={c.primary} dot={false} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function Detail({ id, onClose }: { id: string; onClose: () => void }) {
  const [e, setE] = useState<LabExperiment | null>(null);
  const [tab, setTab] = useState<(typeof TABS)[number]>("Decision");
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.experiment(id).then(setE).catch((x) => setError(String(x)));
  }, [id]);
  if (error) return <ErrorBox error={error} />;
  if (!e) return null;
  const p = e.results.pipeline;
  const d: LabDecision | undefined = p?.decision_at_completion;
  const st = p?.E_statistics;
  const rob = p?.D_robustness;
  const ct = p?.C_temporal;
  return (
    <Section title={e.title} right={
      <div className="flex gap-2">
        <a className="btn-ghost" href={api.exportJsonUrl(e.id)}>Export JSON</a>
        <button className="btn-ghost" onClick={onClose}>Close</button>
      </div>
    }>
      <div className="flex flex-col gap-3 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={STATUS_TONE[e.status] ?? "slate"}>{e.status}</Badge>
          {e.is_synthetic && <SyntheticBadge />}
          <span className="text-xs text-slate-500">{e.family} · {e.strategy} · {e.universe.join(", ")} · {e.variants} variants · seed {e.seed}</span>
        </div>
        {e.error && <ErrorBox error={e.error} />}
        {e.status === "INVALID" && <p className="text-rose-700 dark:text-rose-300">Critical checks failed: {(e.invalid_checks ?? []).join(", ")}. No result of this experiment is used for selection.</p>}
        <div role="tablist" className="flex flex-wrap gap-1">
          {TABS.map((t) => (
            <button key={t} role="tab" aria-selected={tab === t} className={`rounded px-2 py-1 text-xs ${tab === t ? "bg-slate-200 font-medium dark:bg-slate-800" : "hover:bg-slate-100 dark:hover:bg-slate-900"}`}
              onClick={() => setTab(t)}>{t}</button>
          ))}
        </div>
        {tab === "Decision" && (d ? <DecisionTab d={d} /> : <p className="text-slate-500">No decision: the experiment did not complete.</p>)}
        {tab === "A Integrity" && p && <Table rows={p.A_integrity.checks} cols={["check", "passed", "critical", "detail"]} />}
        {tab === "B Baselines" && p?.B_baselines && (
          <Table rows={p.B_baselines.map((b: Record<string, any>) => ({ ...b, baseline_mean_net: b.metrics?.mean_net, baseline_sharpe_net: b.metrics?.sharpe_net }))}
            cols={["baseline", "how", "beaten", "diff_mean_net", "diff_ci95", "diff_pvalue_one_sided", "placebo_pvalue", "baseline_mean_net", "baseline_sharpe_net", "not_applicable"]} />
        )}
        {tab === "C Temporal (IS vs OOS)" && ct && (
          <div className="flex flex-col gap-3">
            <KV data={{ "research bars": ct.research_bars, "out-of-sample bars": ct.oos_bars, "embargo bars": ct.embargo_bars, "final period starts": ct.holdout_start, "final period bars": ct.holdout_bars }} />
            <div className="font-medium">Walk-forward windows: in-sample score of the chosen parameters vs the next test window</div>
            <Table rows={ct.windows} cols={["train_from", "test_from", "test_to", "chosen_params", "train_score", "test_sharpe_net", "test_mean_net", "test_trades"]} />
            <div className="font-medium">In-sample grid (whole research period)</div>
            <Table rows={ct.in_sample_grid} />
            {ct.holdout ? (
              <div>
                <div className="font-medium">Final period</div>
                <p className={ct.holdout.independent ? "" : "text-amber-700 dark:text-amber-300"}>{ct.holdout.note} (access {ct.holdout.access_number})</p>
                <KV data={ct.holdout.metrics ?? {}} />
              </div>
            ) : <p className="text-xs text-slate-500">The final period was not evaluated (it stays reserved).</p>}
            {p?.oos_curve && <Curve curve={p.oos_curve} />}
          </div>
        )}
        {tab === "D Robustness" && rob && (
          <div className="flex flex-col gap-3">
            <div className="font-medium">Breaks under</div>
            {rob.breaks_under.length ? <Table rows={rob.breaks_under} /> : <p className="text-xs">No tested variation turned the out-of-sample mean negative.</p>}
            <div className="font-medium">Costs and slippage</div><Table rows={rob.costs} />
            <div className="font-medium">Execution delay</div><Table rows={rob.delays} />
            <div className="font-medium">Training length</div><Table rows={rob.training_length} />
            <div className="font-medium">Parameter grid ({pct(rob.parameters.positive_frac)} positive in-sample)</div><Table rows={rob.parameters.grid} />
            <div className="font-medium">Volatility regimes</div>
            <Table rows={Object.entries(rob.regimes).map(([k, v]) => ({ regime: k, ...(v as object) }))} />
            <div className="font-medium">Years</div><Table rows={rob.years} />
            <div className="font-medium">Universe subsets</div><Table rows={rob.universe_subsets} />
          </div>
        )}
        {tab === "E Statistics" && st && (
          <div className="flex flex-col gap-3">
            <KV data={{ ...st.oos, "Sharpe 95% CI": st.sharpe_ci95, "mean net 95% CI": st.mean_net_ci95, "HAC t": st.t_hac, "one-sided p (raw)": st.pvalue_one_sided,
              "independent trades": st.independent_trades, DSR: st.dsr, trials: st.n_trials, PBO: st.pbo, ...st.extremes }} />
            <ul className="text-xs text-slate-500">{(st.assumptions ?? []).map((a: string) => <li key={a}>{a}</li>)}</ul>
            <p className="text-xs text-slate-500">A p-value is not the probability that the strategy works or will be profitable.</p>
          </div>
        )}
        {tab === "Versions" && (
          <div className="flex flex-col gap-3">
            <KV data={{ "pipeline + spec": e.algorithm_version, "code version": e.code_version, "spec hash": e.spec_hash, "experiment hash": e.config_hash, attempts: e.attempts }} />
            <Table rows={e.data_refs} cols={["symbol", "frequency", "rows", "quality_status", "is_synthetic", "content_hash"]} />
            <div className="font-medium">History of this family, strategy and universe</div>
            <Table rows={e.lineage.map((l) => ({ ...l, changed_from_previous: l.changed_from_previous.join(", ") || "—", current: l.current ? "this one" : "" }))}
              cols={["created_at", "status", "decision_at_completion", "changed_from_previous", "code_version", "spec_hash", "current"]} />
            <details><summary className="cursor-pointer text-xs">Full spec</summary><pre className="text-xs">{JSON.stringify(e.config.spec, null, 2)}</pre></details>
          </div>
        )}
      </div>
    </Section>
  );
}

// -- page ------------------------------------------------------------------------------------------------
export default function ExperimentLabPage() {
  const [meta, setMeta] = useState<LabMeta | null>(null);
  const [rows, setRows] = useState<LabExperimentSummary[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api.experiments().then(setRows).catch((e) => setError(String(e)));
  }, []);
  useEffect(() => {
    api.meta().then(setMeta).catch((e) => setError(String(e)));
    load();
  }, [load]);
  // Poll while something is queued or running, and for a few seconds after a submission (the new row may not be
  // listed by the first refresh).
  const [watchUntil, setWatchUntil] = useState(0);
  const active = useMemo(() => rows.some((r) => ACTIVE.includes(r.status)), [rows]);
  useEffect(() => {
    if (!active && Date.now() > watchUntil) {
      setVersion((v) => v + 1);
      return;
    }
    const t = setInterval(() => {
      load();
      if (!active && Date.now() > watchUntil) setWatchUntil(0);
    }, 2500);
    return () => clearInterval(t);
  }, [active, load, watchUntil]);
  // Refresh the ranking as soon as an experiment finishes, not only when polling stops.
  const finished = useMemo(() => rows.filter((r) => !ACTIVE.includes(r.status)).map((r) => `${r.id}:${r.status}`).join(","), [rows]);
  useEffect(() => setVersion((v) => v + 1), [finished]);
  const queued = useCallback(() => {
    setWatchUntil(Date.now() + 10_000);
    load();
  }, [load]);

  const cancel = async (id: string) => {
    try {
      await api.cancel(id);
      load();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-lg font-semibold">Experiment Lab</h1>
        <p className="text-xs text-slate-600 dark:text-slate-400">
          Every experiment goes through the same pipeline: integrity, baselines, walk-forward with purge and embargo,
          robustness and statistics with lab-wide multiple-testing correction. Generated hypotheses are research
          proposals, not evidence; no decision promotes a strategy or changes paper trading.
          {meta && <> Versions: {meta.spec_version}, {meta.pipeline_version}, {meta.selection_version}.</>}
        </p>
      </div>
      <ErrorBox error={error} />
      {meta && <NewExperiments meta={meta} onQueued={queued} />}
      <Queue rows={rows} onOpen={setOpen} onCancel={cancel} />
      {open && <Detail key={open} id={open} onClose={() => setOpen(null)} />}
      {meta && <Ranking meta={meta} version={version} onOpen={setOpen} />}
    </div>
  );
}

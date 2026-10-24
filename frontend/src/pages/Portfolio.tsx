import { useCallback, useEffect, useMemo, useState } from "react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import {
  portfolio as api,
  type Correlations,
  type Job,
  type PortfolioRun,
  type PortfolioRunSummary,
  type PortfolioStrategy,
  type ProposalRun,
  type RiskDecisionOut,
  type RiskPolicyOut,
  type Scenario,
} from "../api";
import { Badge, ErrorBox, KV, Section, SyntheticBadge, fmt, fmtTs } from "../components/ui";
import { chartColors } from "../components/theme";

const pct = (v: number | null | undefined, d = 2) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(d)}%`);
const METHODS = ["risk_budget", "inverse_vol", "equal_weight", "mean_variance"];
const ACTIVE = new Set(["queued", "running", "cancelling"]);

/** Historical (observed in the backtest) vs estimate (from trailing data): always labelled. */
export function Basis({ kind }: { kind: "historical" | "estimate" | "hypothetical" | "synthetic" }) {
  const map = {
    historical: { tone: "slate" as const, text: "historical · observed" },
    estimate: { tone: "indigo" as const, text: "estimate · not a forecast" },
    hypothetical: { tone: "amber" as const, text: "hypothetical scenario" },
    synthetic: { tone: "amber" as const, text: "synthetic simulation" },
  };
  return <Badge tone={map[kind].tone}>{map[kind].text}</Badge>;
}

function StrategyTable({ rows, selected, onToggle }: { rows: PortfolioStrategy[]; selected: string[]; onToggle: (id: string) => void }) {
  if (!rows.length)
    return <p className="text-sm text-slate-500">No validated strategies yet. Hypotheses become strategies only after a validation run.</p>;
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full text-sm">
        <thead>
          <tr>{["", "Strategy", "Status", "OOS net Sharpe", "OOS max DD", "OOS bars", "Eligibility"].map((h) => <th key={h} className="th">{h}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id}>
              <td className="td">
                <input type="checkbox" aria-label={`select ${r.id}`} disabled={!r.eligible} checked={selected.includes(r.id)} onChange={() => onToggle(r.id)} />
              </td>
              <td className="td">
                <div className="font-medium">{r.name}</div>
                <div className="text-xs text-slate-500">{r.id} · {r.universe.join(", ")} {r.is_synthetic && <SyntheticBadge />}</div>
              </td>
              <td className="td text-xs">{r.approval_status}</td>
              <td className="td">{fmt(r.validation.oos_metrics.sharpe_net)}</td>
              <td className="td">{pct(r.validation.oos_metrics.max_drawdown)}</td>
              <td className="td">{r.validation.oos_metrics.bars ?? "—"}</td>
              <td className="td text-xs">
                {r.eligible ? <Badge tone="green">eligible</Badge> : <Badge tone="red">rejected</Badge>}
                {!r.eligible && <ul className="mt-1 list-disc pl-4">{r.reasons.map((x) => <li key={x}>{x}</li>)}</ul>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const EDITABLE: [keyof RiskPolicyOut["limits"], string, string][] = [
  ["strategy", "max_weight", "Max weight per strategy"],
  ["strategy", "max_drawdown", "Strategy drawdown stop"],
  ["group", "max_currency_net", "Max net exposure per currency"],
  ["group", "max_instrument_gross", "Max exposure per instrument"],
  ["portfolio", "max_leverage", "Max leverage"],
  ["portfolio", "target_vol", "Target volatility (ex-ante)"],
  ["portfolio", "max_ex_ante_vol", "Max ex-ante volatility"],
  ["portfolio", "max_daily_loss", "Max daily loss"],
  ["portfolio", "max_drawdown", "Portfolio drawdown limit"],
];

function PolicyPanel({ policy, onSaved }: { policy: RiskPolicyOut | null; onSaved: () => void }) {
  const [draft, setDraft] = useState<RiskPolicyOut["limits"] | null>(null);
  const [who, setWho] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => setDraft(policy ? structuredClone(policy.limits) : null), [policy]);
  if (!policy || !draft) return null;
  const save = async () => {
    setMsg(null);
    try {
      const p = await api.createPolicy({ limits: draft, approved_by: who, confirm });
      setMsg(`Saved as ${p.label}. It applies to new runs.`);
      setConfirm(false);
      onSaved();
    } catch (e) {
      setMsg((e as Error).message);
    }
  };
  return (
    <div className="flex flex-col gap-2 text-sm">
      <p>
        Active policy <span className="font-mono">{policy.label}</span>, created by {policy.created_by} on {fmtTs(policy.created_at)}.
        Limits are applied by the deterministic risk manager. Agents cannot change them; a new version needs a named person and an explicit confirmation.
      </p>
      <div className="grid gap-2 md:grid-cols-3">
        {EDITABLE.map(([lvl, key, label]) => (
          <label key={`${lvl}.${key}`} className="flex flex-col">
            <span className="text-xs text-slate-500">{label} ({lvl})</span>
            <input aria-label={label} className="input" type="number" step={0.01} value={draft[lvl][key] ?? ""}
              onChange={(e) => setDraft({ ...draft, [lvl]: { ...draft[lvl], [key]: Number(e.target.value) } })} />
          </label>
        ))}
      </div>
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col">
          <span className="text-xs text-slate-500">Approved by</span>
          <input aria-label="Approved by" className="input" value={who} onChange={(e) => setWho(e.target.value)} />
        </label>
        <label className="flex items-center gap-1">
          <input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} /> I confirm this new policy version
        </label>
        <button className="btn" disabled={who.length < 2 || !confirm} onClick={save}>Save new version</button>
      </div>
      {msg && <p className="text-xs">{msg}</p>}
    </div>
  );
}

function EquityChart({ run }: { run: PortfolioRun }) {
  const c = chartColors();
  const names = Object.keys(run.results.methods).slice(0, 4);
  const data = useMemo(() => {
    const byTs = new Map<string, Record<string, number | string>>();
    names.forEach((m) => run.results.methods[m].curve.forEach((p) => {
      const row = byTs.get(p.ts) ?? { ts: p.ts };
      row[m] = p.equity;
      byTs.set(p.ts, row);
    }));
    return [...byTs.values()].sort((a, b) => String(a.ts).localeCompare(String(b.ts)));
  }, [run, names]);
  return (
    <div className="h-72">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={c.grid} vertical={false} />
          <XAxis dataKey="ts" tick={{ fontSize: 11, fill: c.axis }} minTickGap={60} />
          <YAxis domain={["auto", "auto"]} tick={{ fontSize: 11, fill: c.axis }} width={48} />
          <Tooltip formatter={(v: number) => fmt(v)} />
          <Legend />
          {names.map((m, i) => (
            <Line key={m} dataKey={m} name={m} stroke={c.series[i]} strokeWidth={2} dot={false} isAnimationActive={false} />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function CorrMatrix({ ids, m, label }: { ids: string[]; m: number[][] | null | undefined; label: string }) {
  const c = chartColors();
  if (!m) return <p className="text-xs text-slate-500">{label}: not enough observations.</p>;
  return (
    <div className="overflow-x-auto">
      <div className="mb-1 text-xs font-medium">{label}</div>
      <table className="text-xs">
        <thead>
          <tr><th className="th" />{ids.map((s) => <th key={s} className="th">{s.replace(/^(SYN:|H:)/, "")}</th>)}</tr>
        </thead>
        <tbody>
          {ids.map((s, i) => (
            <tr key={s}>
              <td className="td font-medium">{s.replace(/^(SYN:|H:)/, "")}</td>
              {m[i].map((v, j) => (
                <td key={j} className="td text-center tabular-nums" title={`${s} vs ${ids[j]}: ${v.toFixed(3)}`}
                  style={{ backgroundColor: `${v >= 0 ? c.pos : c.neg}${Math.round(Math.min(Math.abs(v), 1) * 0.6 * 255).toString(16).padStart(2, "0")}` }}>
                  {v.toFixed(2)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CorrelationSummary({ c }: { c: Correlations }) {
  return (
    <ul className="text-xs">
      <li>Static mean pairwise correlation: {fmt(c.static_mean_pairwise)} ({c.observations} common days)</li>
      <li>Rolling ({"63"}-day) mean pairwise: from {fmt(c.rolling?.min)} to {fmt(c.rolling?.max)}</li>
      <li>High-vol regime: {fmt(c.regime?.high_vol.mean_pairwise)} · low-vol regime: {fmt(c.regime?.low_vol.mean_pairwise)} ({c.regime?.rule})</li>
      <li>Stress ({c.stress?.rule}): {fmt(c.stress?.mean_pairwise)}. {c.stress?.note}</li>
    </ul>
  );
}

function ScenarioTable({ rows }: { rows: Scenario[] }) {
  return (
    <table className="min-w-full text-sm">
      <thead>
        <tr>{["Scenario", "Kind", "P&L (fraction of capital)", "Detail"].map((h) => <th key={h} className="th">{h}</th>)}</tr>
      </thead>
      <tbody>
        {rows.map((s) => (
          <tr key={s.name}>
            <td className="td font-medium">{s.name}</td>
            <td className="td"><Basis kind={s.kind} /></td>
            <td className="td tabular-nums">{pct(s.pnl)}</td>
            <td className="td text-xs">
              {s.description}
              {s.start && <> · {s.start} → {s.end}</>}
              {s.basis && <> · {s.basis}</>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Decisions({ runId }: { runId: string }) {
  const [rows, setRows] = useState<RiskDecisionOut[]>([]);
  const [only, setOnly] = useState("blocked");
  useEffect(() => {
    api.decisions(runId, only || undefined).then((d) => setRows(d.items), () => setRows([]));
  }, [runId, only]);
  return (
    <div className="flex flex-col gap-2">
      <label className="flex items-center gap-2 text-sm">
        Show
        <select aria-label="Decision filter" className="input" value={only} onChange={(e) => setOnly(e.target.value)}>
          <option value="blocked">blocked only</option>
          <option value="">all rules checked</option>
        </select>
        <span className="text-xs text-slate-500">latest 500</span>
      </label>
      <div className="max-h-96 overflow-auto">
        <table className="min-w-full text-xs">
          <thead>
            <tr>{["As of", "Level", "Scope", "Rule", "Observed", "Limit", "Decision", "Action", "Reason", "Policy"].map((h) => <th key={h} className="th">{h}</th>)}</tr>
          </thead>
          <tbody>
            {rows.map((d, i) => (
              <tr key={i}>
                <td className="td">{fmtTs(d.as_of).slice(0, 10)}</td>
                <td className="td">{d.level}</td>
                <td className="td">{d.scope}</td>
                <td className="td font-mono">{d.rule}</td>
                <td className="td">{d.observed ?? "—"}</td>
                <td className="td">{d.limit}</td>
                <td className="td"><Badge tone={d.decision === "blocked" ? "red" : "green"}>{d.decision}</Badge></td>
                <td className="td">{d.action}</td>
                <td className="td">{d.reason}</td>
                <td className="td font-mono">{d.policy_version}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!rows.length && <p className="text-xs text-slate-500">No decisions match.</p>}
      </div>
    </div>
  );
}

export function RunDetail({ run }: { run: PortfolioRun }) {
  const r = run.results;
  if (run.status !== "succeeded") {
    return (
      <div className="text-sm">
        <p className="text-rose-700 dark:text-rose-300">Run {run.status}: {run.error}</p>
        {(r?.rejected ?? []).map((x) => <p key={x.id} className="text-xs">{x.id}: {x.reasons.join("; ")}</p>)}
      </div>
    );
  }
  const ids = r.strategies.map((s) => s.id);
  const risk = r.risk;
  return (
    <div className="flex flex-col gap-4">
      {r.is_synthetic && (
        <div className="rounded-md border border-amber-300 bg-amber-50 p-2 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200">
          <SyntheticBadge /> {r.synthetic_note}
        </div>
      )}
      <p className="text-xs text-slate-500">
        {r.period.start} → {r.period.end} ({r.period.days} days) · policy {r.policy_version} · primary method {r.primary_method} ·{" "}
        {r.primary.n_rebalances} rebalances · {r.decisions_stored} risk decisions stored
        {r.rejected.length > 0 && <> · rejected: {r.rejected.map((x) => x.id).join(", ")}</>}
      </p>

      <Section title="Construction methods compared" right={<Basis kind="historical" />}>
        <div className="overflow-x-auto">
          <table className="min-w-full text-sm">
            <thead>
              <tr>{["Method", "Net Sharpe", "Volatility", "Max DD", "Total return", "Avg leverage", "Rebalance cost", "Weight change / rebalance", "Sharpe first / last segment", "Risk-manager blocks"].map((h) => <th key={h} className="th">{h}</th>)}</tr>
            </thead>
            <tbody>
              {Object.entries(r.methods).map(([m, x]) => (
                <tr key={m}>
                  <td className="td font-medium" title={x.description}>{m}</td>
                  <td className="td">{fmt(x.metrics.sharpe_net)}</td>
                  <td className="td">{pct(x.metrics.volatility_ann)}</td>
                  <td className="td">{pct(x.metrics.max_drawdown)}</td>
                  <td className="td">{pct(x.metrics.total_return_net)}</td>
                  <td className="td">{fmt(x.metrics.avg_leverage)}</td>
                  <td className="td">{pct(x.metrics.rebalance_cost_total, 3)}</td>
                  <td className="td">{fmt(x.stability.mean_abs_change)}</td>
                  <td className="td">{fmt(x.segments.first.sharpe_net)} / {fmt(x.segments.last.sharpe_net)}</td>
                  <td className="td text-xs">{Object.entries(x.blocked).map(([k, v]) => `${k} ${v}`).join(", ") || "—"}</td>
                </tr>
              ))}
              <tr>
                <td className="td text-xs">naive equal weight (no vol target, no risk manager)</td>
                <td className="td">{fmt(r.benchmarks.naive_equal_weight.sharpe_net)}</td>
                <td className="td">{pct(r.benchmarks.naive_equal_weight.volatility_ann)}</td>
                <td className="td">{pct(r.benchmarks.naive_equal_weight.max_drawdown)}</td>
                <td className="td">{pct(r.benchmarks.naive_equal_weight.total_return_net)}</td>
                <td className="td" colSpan={5} />
              </tr>
            </tbody>
          </table>
        </div>
        <EquityChart run={run} />
      </Section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title={`Allocation at ${risk.as_of ?? "—"}`} right={<Basis kind="estimate" />}>
          <table className="min-w-full text-sm">
            <thead>
              <tr>{["Strategy", "Weight", "Share of risk", "Marginal risk"].map((h) => <th key={h} className="th">{h}</th>)}</tr>
            </thead>
            <tbody>
              {ids.map((s) => (
                <tr key={s}>
                  <td className="td">{s}</td>
                  <td className="td">{pct(risk.weights[s])}</td>
                  <td className="td">{pct(risk.risk_contributions[s], 1)}</td>
                  <td className="td">{fmt(risk.marginal_risk[s])}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-2 text-xs">Ex-ante volatility {pct(risk.ex_ante_vol)} (trailing covariance; not a forecast).</p>
          <KV data={Object.fromEntries(Object.entries(risk.diversification).map(([k, v]) => [k.replace(/_/g, " "), v]))} />
        </Section>
        <Section title="Look-through exposure" right={<Basis kind="estimate" />}>
          <div className="grid gap-3 text-sm md:grid-cols-2">
            <div>
              <div className="font-medium">By instrument</div>
              <ul className="text-xs">{Object.entries(risk.exposures.instrument).map(([k, v]) => <li key={k}>{k}: {pct(v)}</li>)}</ul>
            </div>
            <div>
              <div className="font-medium">Net by currency</div>
              <ul className="text-xs">{Object.entries(risk.exposures.currency_net).map(([k, v]) => <li key={k}>{k}: {pct(v)}</li>)}</ul>
            </div>
          </div>
          <p className="mt-2 text-xs">Gross {pct(risk.exposures.gross)} · net {pct(risk.exposures.net)}</p>
          <ul className="mt-2 text-xs">
            {risk.liquidity.map((l) => (
              <li key={l.instrument}>{l.instrument}: {l.note ?? `capacity ≈ ${fmt(l.capacity_capital)} of capital`}</li>
            ))}
          </ul>
        </Section>
      </div>

      <Section title="Correlation and diversification" right={<Basis kind="historical" />}>
        <CorrelationSummary c={r.historical_risk.correlations} />
        <div className="mt-2 grid gap-4 lg:grid-cols-2">
          <CorrMatrix ids={ids} m={r.historical_risk.correlations.static} label="Whole sample" />
          <CorrMatrix ids={ids} m={r.historical_risk.correlations.stress?.matrix} label="Turbulent periods" />
        </div>
        <table className="mt-3 min-w-full text-sm">
          <thead>
            <tr>{["Strategy", "Correlation with the rest", "Vol with / without", "Effective bets with / without", "Assessment"].map((h) => <th key={h} className="th">{h}</th>)}</tr>
          </thead>
          <tbody>
            {risk.marginal_analysis.map((m) => (
              <tr key={m.strategy}>
                <td className="td">{m.strategy}</td>
                <td className="td">{fmt(m.corr_with_rest)}</td>
                <td className="td">{pct(m.vol_with)} / {pct(m.vol_without)}</td>
                <td className="td">{fmt(m.enb_with)} / {fmt(m.enb_without)}</td>
                <td className="td">{m.adds_risk_without_diversifying ? <Badge tone="amber">{m.note}</Badge> : <Badge tone="green">{m.note}</Badge>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section title="Stress tests">
        <p className="mb-2 text-xs text-slate-500">{r.stress.disclaimer}</p>
        <ScenarioTable rows={[...r.stress.historical, ...r.stress.hypothetical]} />
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          {r.stress.monte_carlo.map((m, i) => (
            <div key={i} className="text-sm">
              <div className="font-medium">{m.name ?? "Monte Carlo"} <Basis kind="synthetic" /></div>
              {m.daily ? (
                <ul className="text-xs">
                  <li>{m.paths} paths × {m.horizon_days} days</li>
                  <li>1-day VaR 95% / 99%: {pct(m.daily.var95)} / {pct(m.daily.var99)} · ES 99%: {pct(m.daily.es99)}</li>
                  <li>Horizon return p1 / p5 / median: {pct(m.horizon?.p01)} / {pct(m.horizon?.p05)} / {pct(m.horizon?.median)}</li>
                  <li>Max drawdown median / 1% worst: {pct(m.max_drawdown?.median)} / {pct(m.max_drawdown?.p99_worst)}</li>
                </ul>
              ) : null}
              <p className="text-xs text-slate-500">{m.note}</p>
            </div>
          ))}
        </div>
      </Section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Cost scenarios" right={<Basis kind="historical" />}>
          <table className="min-w-full text-sm">
            <thead><tr>{["Costs ×", "Net Sharpe", "Total return"].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
            <tbody>
              {r.cost_sensitivity.map((c) => (
                <tr key={c.multiplier}><td className="td">{c.multiplier}</td><td className="td">{fmt(c.sharpe_net)}</td><td className="td">{pct(c.total_return_net)}</td></tr>
              ))}
            </tbody>
          </table>
        </Section>
        <Section title="Look-ahead checks">
          <ul className="text-sm">
            {r.leakage_checks.map((c) => (
              <li key={c.check}><Badge tone={c.passed ? "green" : "red"}>{c.passed ? "pass" : "fail"}</Badge> {c.check}: {c.description}</li>
            ))}
          </ul>
          <ul className="mt-2 text-xs text-slate-500">{r.notes.map((n) => <li key={n}>{n}</li>)}</ul>
        </Section>
      </div>

      <Section title="Risk manager decisions">
        <Decisions runId={run.id} />
      </Section>
    </div>
  );
}

function Proposals({ includeSynthetic, onOpen }: { includeSynthetic: boolean; onOpen: (id: string) => void }) {
  const [list, setList] = useState<ProposalRun[]>([]);
  const [open, setOpen] = useState<ProposalRun | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const load = useCallback(() => api.proposals().then(setList, () => undefined), []);
  useEffect(() => {
    load();
    const t = window.setInterval(load, 5000);
    return () => window.clearInterval(t);
  }, [load]);
  const ask = async () => {
    try {
      await api.propose(includeSynthetic);
      setMsg("The agent is preparing proposals; each one is computed by the engine and checked by the risk manager.");
    } catch (e) {
      setMsg((e as Error).message);
    }
  };
  return (
    <div className="flex flex-col gap-2 text-sm">
      <div className="flex items-center gap-2">
        <button className="btn" onClick={ask}>Ask the agent for proposals</button>
        <span className="text-xs text-slate-500">The agent proposes; it cannot approve a portfolio or change limits.</span>
      </div>
      {msg && <p className="text-xs">{msg}</p>}
      {list.slice(0, 5).map((p) => (
        <div key={p.id} className="rounded border border-slate-200 p-2 dark:border-slate-800">
          <div className="flex items-center gap-2 text-xs">
            {fmtTs(p.created_at)} · {p.status} · {p.results?.eligible ?? 0} eligible of {p.results?.candidates ?? 0}
            {p.results?.is_synthetic && <SyntheticBadge />}
            <button className="underline" onClick={() => api.proposal(p.id).then(setOpen)}>agent log</button>
          </div>
          <ul className="mt-1">
            {(p.results?.proposals ?? []).map((x) => (
              <li key={x.key}>
                <button className="font-medium underline" onClick={() => onOpen(x.experiment_id)}>{x.key}</button>{" "}
                <Badge tone={x.verdict === "within limits" ? "green" : "amber"}>{x.verdict}</Badge>{" "}
                <span className="text-xs">{x.rationale} · Sharpe {fmt(x.metrics.sharpe_net)} (historical)</span>
              </li>
            ))}
          </ul>
          {open?.id === p.id && (
            <ol className="mt-2 list-decimal pl-5 text-xs">
              {(open.messages ?? []).map((m) => (
                <li key={m.seq}><span className="font-mono">{m.sender} → {m.receiver}</span> [{m.kind}] {m.subject}: {m.payload.verdict ?? m.payload.rationale ?? m.payload.reasons?.join("; ") ?? m.payload.status}</li>
              ))}
            </ol>
          )}
        </div>
      ))}
    </div>
  );
}

export default function PortfolioPage() {
  const [synthetic, setSynthetic] = useState(false);
  const [strategies, setStrategies] = useState<PortfolioStrategy[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [policy, setPolicy] = useState<RiskPolicyOut | null>(null);
  const [runs, setRuns] = useState<PortfolioRunSummary[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [run, setRun] = useState<PortfolioRun | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [form, setForm] = useState({ methods: ["risk_budget", "inverse_vol", "equal_weight"], rebalance: "monthly", lookback: 252, cov: "ledoit_wolf", costs: 1 });

  useEffect(() => {
    api.strategies(synthetic).then((s) => {
      setStrategies(s);
      setSelected(s.filter((x) => x.eligible).map((x) => x.id));
    }, (e) => setError(e.message));
  }, [synthetic]);
  const loadPolicy = useCallback(() => api.activePolicy().then(setPolicy, () => undefined), []);
  useEffect(() => { loadPolicy(); }, [loadPolicy]);
  const load = useCallback(() => {
    api.runs().then(setRuns, (e) => setError(e.message));
    api.jobs().then((j) => setJobs(j.filter((x) => x.kind.startsWith("portfolio") && ACTIVE.has(x.status))), () => undefined);
  }, []);
  useEffect(() => {
    load();
    const t = window.setInterval(load, 4000);
    return () => window.clearInterval(t);
  }, [load]);

  const openRun = (id: string) => api.run(id).then(setRun, (e) => setError(e.message));
  const toggle = (list: string[], v: string) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);
  const start = async () => {
    setError(null);
    setNotice(null);
    try {
      await api.start({ strategy_ids: selected, methods: form.methods, rebalance: form.rebalance, lookback_days: form.lookback,
        cov_method: form.cov, cost_multiplier: form.costs, criteria: { allow_synthetic: synthetic } });
      setNotice("Portfolio run queued. It appears in the list below when it finishes.");
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-lg font-semibold">Portfolio &amp; Risk</h1>
        <p className="max-w-4xl text-sm text-slate-600 dark:text-slate-400">
          Combine strategies that passed validation, compare construction methods, and see what the risk manager allowed and
          why. Historical figures are what the backtest observed; estimates come from trailing data and are not forecasts.
          Nothing here sends orders.
        </p>
      </div>
      <ErrorBox error={error} />
      {notice && <div className="text-sm text-indigo-700 dark:text-indigo-300">{notice}</div>}

      <Section title="Strategies" right={
        <label className="flex items-center gap-1 text-xs">
          <input type="checkbox" checked={synthetic} onChange={(e) => setSynthetic(e.target.checked)} />
          include synthetic strategies (engine testing only)
        </label>
      }>
        {synthetic && <p className="mb-2 text-xs text-amber-700 dark:text-amber-300"><SyntheticBadge /> Synthetic strategies come from a seeded factor model. Their results are not evidence about any market.</p>}
        <StrategyTable rows={strategies} selected={selected} onToggle={(id) => setSelected(toggle(selected, id))} />
      </Section>

      <Section title="Run a portfolio backtest">
        <div className="flex flex-wrap items-end gap-4 text-sm">
          <div>
            <div className="mb-1 font-medium">Methods</div>
            {METHODS.map((m) => (
              <label key={m} className="mr-3 inline-flex items-center gap-1">
                <input type="checkbox" checked={form.methods.includes(m)} onChange={() => setForm({ ...form, methods: toggle(form.methods, m) })} />
                {m}{m === "mean_variance" && <span className="text-xs text-slate-500"> (optional, unstable)</span>}
              </label>
            ))}
          </div>
          <label className="flex flex-col">Rebalance
            <select aria-label="Rebalance" className="input" value={form.rebalance} onChange={(e) => setForm({ ...form, rebalance: e.target.value })}>
              {["monthly", "weekly", "daily", "20d"].map((x) => <option key={x}>{x}</option>)}
            </select>
          </label>
          <label className="flex flex-col">Lookback (days)
            <input aria-label="Lookback" className="input w-24" type="number" min={30} value={form.lookback} onChange={(e) => setForm({ ...form, lookback: Number(e.target.value) })} />
          </label>
          <label className="flex flex-col">Covariance
            <select aria-label="Covariance" className="input" value={form.cov} onChange={(e) => setForm({ ...form, cov: e.target.value })}>
              {["ledoit_wolf", "sample", "ewma"].map((x) => <option key={x}>{x}</option>)}
            </select>
          </label>
          <label className="flex flex-col">Costs ×
            <input aria-label="Cost multiplier" className="input w-20" type="number" min={0} step={0.5} value={form.costs} onChange={(e) => setForm({ ...form, costs: Number(e.target.value) })} />
          </label>
          <button className="btn" disabled={!selected.length || !form.methods.length} onClick={start}>Run</button>
        </div>
        {jobs.length > 0 && <p className="mt-2 text-xs">Running: {jobs.map((j) => `${j.kind} (${j.status})`).join(", ")}</p>}
      </Section>

      <Section title="Risk policy">
        <PolicyPanel policy={policy} onSaved={loadPolicy} />
      </Section>

      <Section title="Agent proposals">
        <Proposals includeSynthetic={synthetic} onOpen={openRun} />
      </Section>

      <Section title="Runs">
        <table className="min-w-full text-sm">
          <thead>
            <tr>{["When", "Strategies", "Methods", "Status", "Net Sharpe", "Max DD", "Policy", ""].map((h) => <th key={h} className="th">{h}</th>)}</tr>
          </thead>
          <tbody>
            {runs.map((x) => (
              <tr key={x.id}>
                <td className="td text-xs">{fmtTs(x.created_at)}</td>
                <td className="td text-xs">{x.strategies.length} {x.is_synthetic && <SyntheticBadge />} {x.label}</td>
                <td className="td text-xs">{x.methods.join(", ")}</td>
                <td className="td"><Badge tone={x.status === "succeeded" ? "green" : "red"}>{x.status}</Badge></td>
                <td className="td">{fmt(x.sharpe_net)}</td>
                <td className="td">{pct(x.max_drawdown)}</td>
                <td className="td font-mono text-xs">{x.policy_version}</td>
                <td className="td"><button className="underline" onClick={() => openRun(x.id)}>open</button></td>
              </tr>
            ))}
          </tbody>
        </table>
        {!runs.length && <p className="text-xs text-slate-500">No portfolio runs yet.</p>}
      </Section>

      {run && <RunDetail run={run} />}
    </div>
  );
}

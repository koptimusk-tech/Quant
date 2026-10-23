import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { research, type Experiment } from "../api";
import { Badge, ErrorBox, KV, Section, SyntheticBadge, fmt } from "../components/ui";
import { chartColors } from "../components/theme";

const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(2)}%`);
const day = (s: string) => s.slice(0, 10);

function EquityCharts({ equity }: { equity: { ts: string; equity_gross: number; equity_net: number; drawdown: number }[] }) {
  const c = chartColors();
  const data = equity.map((p) => ({ ...p, t: day(p.ts) }));
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Section title="Equity curve, out-of-sample windows stitched (start = 1)">
        <div className="h-64">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
              <CartesianGrid stroke={c.grid} vertical={false} />
              <XAxis dataKey="t" tick={{ fontSize: 11, fill: c.axis }} minTickGap={50} />
              <YAxis tick={{ fontSize: 11, fill: c.axis }} domain={["auto", "auto"]} width={50} />
              <Tooltip formatter={(v: number) => fmt(v)} />
              <Legend formatter={(v: string) => <span style={{ color: c.axis }}>{v}</span>} />
              <ReferenceLine y={1} stroke={c.muted} />
              <Line dataKey="equity_gross" name="gross (before costs)" stroke={c.secondary} strokeWidth={2} dot={false} isAnimationActive={false} />
              <Line dataKey="equity_net" name="net (after costs)" stroke={c.primary} strokeWidth={2} dot={false} isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </Section>
      <Section title="Drawdown (net)">
        <div className="h-64">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
              <CartesianGrid stroke={c.grid} vertical={false} />
              <XAxis dataKey="t" tick={{ fontSize: 11, fill: c.axis }} minTickGap={50} />
              <YAxis tick={{ fontSize: 11, fill: c.axis }} tickFormatter={(v) => `${(v * 100).toFixed(1)}%`} width={50} />
              <Tooltip formatter={(v: number) => pct(v)} />
              <Area dataKey="drawdown" stroke={c.primary} fill={c.primary} fillOpacity={0.15} strokeWidth={2} isAnimationActive={false} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </Section>
    </div>
  );
}

function GridTable({ grid }: { grid: { params: Record<string, unknown>; sharpe_net_in_sample: number; mean_net_in_sample: number; n_trades: number }[] }) {
  const max = Math.max(...grid.map((g) => Math.abs(g.sharpe_net_in_sample)), 1e-9);
  return (
    <table className="min-w-full text-sm">
      <thead><tr><th className="th">Parameters</th><th className="th">Sharpe net (in-sample)</th><th className="th">Trades</th></tr></thead>
      <tbody>
        {grid.map((g, i) => {
          const v = g.sharpe_net_in_sample;
          // Diverging encoding: indigo for positive, amber for negative, intensity by magnitude.
          const bg = v >= 0 ? `rgba(79,70,229,${(0.08 + 0.5 * v / max).toFixed(2)})` : `rgba(217,119,6,${(0.08 + 0.5 * -v / max).toFixed(2)})`;
          return (
            <tr key={i}>
              <td className="td font-mono text-xs">{JSON.stringify(g.params)}</td>
              <td className="td tabular-nums" style={{ background: bg }}>{fmt(v)}</td>
              <td className="td tabular-nums">{g.n_trades}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export default function ValidationReport() {
  const { id } = useParams();
  const [e, setE] = useState<Experiment | null>(null);
  const [error, setError] = useState<string | null>(null);
  const c = chartColors();
  useEffect(() => {
    if (id) research.experiment(id).then(setE, (x) => setError(x.message));
  }, [id]);
  if (error) return <ErrorBox error={error} />;
  if (!e) return <p className="text-sm text-slate-500">Loading…</p>;
  const r = e.results;
  const synthetic = e.data_refs.some((d) => d.is_synthetic);
  const header = (
    <div className="flex flex-wrap items-center gap-2">
      {e.hypothesis_id && <Link to={`/research/${e.hypothesis_id}`} className="text-sm text-indigo-700 dark:text-indigo-300">← Hypothesis</Link>}
      <h1 className="text-lg font-semibold">{e.kind === "final_test" ? "Final test (holdout)" : "Backtesting & Validation"}</h1>
      {synthetic && <SyntheticBadge />}
      {r.decision && <Badge tone={r.decision === "VALIDATION_PASSED" ? "green" : "red"}>{r.decision}</Badge>}
      <span className="text-xs text-slate-500">{e.algorithm_version} · {e.code_version} · {e.data_refs.map((d) => `${d.symbol}#${String(d.content_hash).slice(0, 8)}`).join(", ")}</span>
    </div>
  );

  if (e.kind === "final_test") {
    return (
      <div className="flex flex-col gap-4">
        {header}
        {r.contaminated && <ErrorBox error={`Access #${r.access_number}: ${r.note}`} />}
        <Section title={`Holdout metrics (${r.holdout_bars} bars, params ${JSON.stringify(r.params)})`}><KV data={r.metrics} /></Section>
        {r.equity?.length > 0 && <EquityCharts equity={r.equity} />}
      </div>
    );
  }

  const m = r.oos.metrics;
  const rob = r.robustness;
  const ovf = r.overfitting;
  const hist = (r.oos.returns_hist.counts as number[]).map((n, i) => ({ x: +(((r.oos.returns_hist.bins[i] + r.oos.returns_hist.bins[i + 1]) / 2) * 100).toFixed(3), n }));
  return (
    <div className="flex flex-col gap-4">
      {header}
      <Section title="Hard gates">
        <div className="grid gap-3 md:grid-cols-4">
          {r.gates.map((g: { gate: string; passed: boolean; reasons: string[] }) => (
            <div key={g.gate} className="rounded-md border border-slate-200 p-3 dark:border-slate-800">
              <div className="mb-1 flex items-center gap-2 text-sm font-semibold">Gate {g.gate} {g.passed ? <Badge tone="green">✓ pass</Badge> : <Badge tone="red">✗ block</Badge>}</div>
              <ul className="space-y-0.5 text-xs">{g.reasons.map((x) => <li key={x}>{x}</li>)}</ul>
            </div>
          ))}
        </div>
      </Section>

      <div className="grid gap-4 md:grid-cols-4">
        {[
          ["Sharpe net (OOS)", fmt(m.sharpe_net)],
          ["Sharpe gross (OOS)", fmt(m.sharpe_gross)],
          ["Return net / gross", `${pct(m.total_return_net)} / ${pct(m.total_return_gross)}`],
          ["Max drawdown", pct(m.max_drawdown)],
          ["Trades / hit rate", `${m.n_trades} / ${pct(m.hit_rate)}`],
          ["Deflated Sharpe", fmt(ovf.dsr)],
          ["PBO", ovf.pbo === null ? "n/a" : fmt(ovf.pbo)],
          ["Trials counted", String(ovf.n_trials)],
        ].map(([k, v]) => (
          <div key={k} className="card">
            <div className="text-xs text-slate-500">{k}</div>
            <div className="text-xl font-semibold tabular-nums">{v}</div>
          </div>
        ))}
      </div>

      <EquityCharts equity={r.oos.equity} />

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Cost sensitivity (OOS, all cost components × multiplier)">
          <div className="h-56">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={rob.cost_sensitivity.map((x: { multiplier: number; sharpe_net: number }) => ({ m: `${x.multiplier}×`, s: x.sharpe_net }))} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
                <CartesianGrid stroke={c.grid} vertical={false} />
                <XAxis dataKey="m" tick={{ fontSize: 11, fill: c.axis }} />
                <YAxis tick={{ fontSize: 11, fill: c.axis }} width={40} />
                <Tooltip formatter={(v: number) => [fmt(v), "Sharpe net"]} />
                <ReferenceLine y={0} stroke={c.axis} />
                <Bar dataKey="s" fill={c.primary} radius={[4, 4, 0, 0]} isAnimationActive={false} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Section>
        <Section title="Distribution of OOS net bar returns (%)">
          <div className="h-56">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={hist} margin={{ top: 8, right: 16, bottom: 0, left: 0 }} barCategoryGap={1}>
                <CartesianGrid stroke={c.grid} vertical={false} />
                <XAxis dataKey="x" tick={{ fontSize: 11, fill: c.axis }} minTickGap={30} />
                <YAxis tick={{ fontSize: 11, fill: c.axis }} width={40} allowDecimals={false} />
                <Tooltip formatter={(v: number) => [v, "bars"]} labelFormatter={(x) => `${x}%`} />
                <Bar dataKey="n" fill={c.primary} radius={[4, 4, 0, 0]} isAnimationActive={false} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Section>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Parameter sensitivity (research period, in-sample)"><GridTable grid={rob.param_grid} /></Section>
        <Section title="Overfitting and robustness indicators">
          <KV data={{
            "Deflated Sharpe (prob.)": ovf.dsr, "expected max Sharpe of unskilled trials (per bar)": ovf.sr0_per_period, "trials (family)": ovf.n_trials,
            PBO: ovf.pbo, "placebo p (random timing)": rob.placebo_pvalue, "bootstrap Sharpe 95% CI": rob.bootstrap?.sharpe_ci_low !== undefined ? `${fmt(rob.bootstrap.sharpe_ci_low)} … ${fmt(rob.bootstrap.sharpe_ci_high)}` : "n/a",
            "OOS windows positive": pct(rob.positive_window_frac), "parameter variants positive": pct(rob.robust_param_frac),
            "top-5 trades share of PnL": m.top5_trades_share, "high-vol regime Sharpe": rob.regimes.high_vol.sharpe_net, "low-vol regime Sharpe": rob.regimes.low_vol.sharpe_net,
            "benchmark Sharpe (same OOS bars)": r.benchmark?.sharpe_net ?? "cash", note: ovf.note,
          }} />
        </Section>
      </div>

      <Section title="Walk-forward windows (parameters chosen on train only)">
        <div className="overflow-x-auto">
          <table className="min-w-full text-sm">
            <thead><tr>{["Train from", "Test", "Chosen params", "Train score", "Test Sharpe net", "Test trades"].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
            <tbody>
              {r.windows.map((w: Record<string, any>, i: number) => (
                <tr key={i}>
                  <td className="td">{day(w.train_from)}</td>
                  <td className="td">{day(w.test_from)} → {day(w.test_to)}</td>
                  <td className="td font-mono text-xs">{JSON.stringify(w.chosen_params)}</td>
                  <td className="td tabular-nums">{fmt(w.train_score)}</td>
                  <td className="td tabular-nums">{fmt(w.test_sharpe_net)}</td>
                  <td className="td tabular-nums">{w.test_trades}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Leakage checks">
          <ul className="space-y-1 text-sm">
            {r.leakage_checks.map((x: { check: string; passed: boolean; description: string }) => (
              <li key={x.check}>{x.passed ? "✓" : "✗"} <b>{x.check}</b>: {x.description}</li>
            ))}
          </ul>
        </Section>
        <Section title="All OOS metrics"><KV data={m} /></Section>
      </div>
      <p className="text-xs text-slate-500">Research bars {r.research_bars}, holdout bars {r.holdout_bars} (holdout from {r.holdout_start}, not used here). Results depend on the declared cost assumptions and data; they are not a promise of future returns.</p>
    </div>
  );
}

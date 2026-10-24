import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { alpha, type AlphaHypothesisDetail } from "../api";
import { Badge, ErrorBox, KV, Section, fmt, fmtTs } from "../components/ui";
import { EvidenceBadge } from "./AlphaResearch";
import { StatusBadge } from "./Research";

const SEV_TONE = { block: "red", major: "amber", minor: "slate" } as const;
const COLS: [string, string][] = [
  ["total_return_gross", "Gross return"],
  ["total_return_net", "Net return"],
  ["total_cost", "Costs"],
  ["sharpe_net", "Sharpe net"],
  ["sortino_net", "Sortino net"],
  ["max_drawdown", "Max DD"],
  ["exposure_avg", "Exposure"],
  ["n_trades", "Trades"],
];

export default function AlphaHypothesisPage() {
  const { id } = useParams();
  const [d, setD] = useState<AlphaHypothesisDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const load = useCallback(() => {
    if (id) alpha.hypothesis(id).then(setD, (e) => setError(e.message));
  }, [id]);
  useEffect(() => {
    load();
    const t = window.setInterval(load, 4000);
    return () => window.clearInterval(t);
  }, [load]);
  if (!d) return error ? <ErrorBox error={error} /> : <p className="text-sm text-slate-500">Loading…</p>;
  const h = d.hypothesis;
  const c = h.contract;
  const idea = d.messages.find((m) => m.kind === "idea");
  const launch = async () => {
    setError(null);
    try {
      await alpha.experiment(h.id);
      setNotice("Experiment queued; this page refreshes.");
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <Link to="/alpha" className="text-sm text-indigo-700 dark:text-indigo-300">← Alpha Research</Link>
        <h1 className="text-lg font-semibold">{h.title}</h1>
        <StatusBadge status={h.status} />
        <Badge>v{h.version}</Badge>
        <Link className="text-sm underline" to={`/research/${h.id}`}>open in Research Lab</Link>
        <button className="btn ml-auto" onClick={launch}>Launch experiment</button>
      </div>
      <ErrorBox error={error} />
      {notice && <div className="text-sm text-indigo-700 dark:text-indigo-300">{notice}</div>}

      <div className="grid gap-3 md:grid-cols-3">
        <Section title="Research priority">
          <p className="text-2xl font-semibold">{idea ? fmt(idea.payload.priority) : "—"}</p>
          <p className="text-xs text-slate-500">Heuristic ordering for the experiment budget. Not evidence.</p>
        </Section>
        <Section title="Predictive evidence">
          <EvidenceBadge level={d.predictive_evidence.level} />
          <p className="mt-1 text-sm">{d.predictive_evidence.text}</p>
        </Section>
        <Section title="Validated net profitability">
          <Badge tone={d.validated_net_profitability.established ? "green" : "slate"}>
            {d.validated_net_profitability.established ? "established" : "not established"}
          </Badge>
          <p className="mt-1 text-sm">{d.validated_net_profitability.text}</p>
        </Section>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Origin and rules">
          <KV data={{
            origin: c.origin, "originating observation": c.origin_observation, premise: c.premise, mechanism: c.mechanism,
            variables: c.features, strategy: c.strategy, "entry rule": c.entry_rule, "exit rule": c.exit_rule,
            "expected direction": c.expected_direction, horizon_bars: c.horizon_bars, benchmark: c.benchmark,
            "refuted if": c.invalidation, limitations: c.limitations,
          }} />
        </Section>
        <Section title="Pre-registered criteria (fixed before any test)">
          <KV data={{ "primary metric": c.success?.metric, minimum: c.success?.min_value, ...c.success, ...c.validation,
            "secondary metrics": c.secondary_metrics, costs: Object.keys(c.costs ?? {}).length ? c.costs : "asset-class defaults (estimates)" }} />
        </Section>
      </div>

      <Section title="Adversarial reviewer objections">
        <ul className="space-y-1 text-sm">
          {d.objections.map((o, i) => (
            <li key={`${o.code}-${i}`}>
              <Badge tone={SEV_TONE[o.severity as keyof typeof SEV_TONE] ?? "slate"}>{o.severity}</Badge>{" "}
              <span className="font-mono text-xs">{o.code}</span> {o.message}
            </li>
          ))}
        </ul>
        {d.objections.length === 0 && <p className="text-sm text-slate-500">No objections recorded.</p>}
      </Section>

      <Section title="Experiments compared (out-of-sample walk-forward windows)">
        <div className="overflow-x-auto">
          <table className="min-w-full text-sm">
            <thead>
              <tr>
                <th className="th">Run</th>
                <th className="th">Decision</th>
                {COLS.map(([, l]) => <th key={l} className="th">{l}</th>)}
                <th className="th">Sharpe 95% CI</th>
                <th className="th">vs benchmark</th>
                <th className="th">DSR</th>
                <th className="th">Placebo p</th>
                <th className="th">Windows +</th>
                <th className="th">Leakage</th>
              </tr>
            </thead>
            <tbody>
              {d.validations.map((v) => (
                <tr key={v.experiment_id}>
                  <td className="td text-xs"><Link className="underline" to={`/validations/${v.experiment_id}`}>{fmtTs(v.created_at)}</Link></td>
                  <td className="td text-xs">{v.decision ?? v.error ?? v.status}</td>
                  {COLS.map(([k]) => <td key={k} className="td">{fmt(v.metrics[k])}</td>)}
                  <td className="td text-xs">[{fmt(v.sharpe_ci[0])}, {fmt(v.sharpe_ci[1])}]</td>
                  <td className="td">{fmt(v.excess_vs_benchmark_mean_net)}</td>
                  <td className="td">{fmt(v.dsr)}</td>
                  <td className="td">{fmt(v.placebo_pvalue)}</td>
                  <td className="td">{fmt(v.positive_window_frac)}</td>
                  <td className="td">{v.leakage_checks_passed === null ? "" : v.leakage_checks_passed ? "✓" : "✗"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {d.validations.length === 0 && <p className="text-sm text-slate-500">Not tested yet.</p>}
      </Section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Why it was accepted or rejected">
          <ul className="space-y-1 text-sm">
            {d.decisions.map((x, i) => (
              <li key={i}><Badge>{x.action}</Badge> {x.reason}</li>
            ))}
            {d.gates.slice(0, 4).map((g) => (
              <li key={g.gate + g.created_at}>
                <Badge tone={g.decision === "pass" ? "green" : "red"}>Gate {g.gate} {g.decision}</Badge> {g.reasons.join("; ")}
              </li>
            ))}
          </ul>
        </Section>
        <Section title="Paper-trading eligibility">
          <ul className="space-y-1 text-sm">
            {d.eligibility.requirements.map((r) => (
              <li key={r.requirement}>{r.met ? "✓" : "✗"} <span className="font-mono text-xs">{r.requirement}</span> {r.detail}</li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-slate-500">
            Eligibility needs every requirement plus an explicit approval by a person with risk limits. It only marks a
            candidate; no orders are sent and live execution stays disabled.
          </p>
        </Section>
      </div>
    </div>
  );
}

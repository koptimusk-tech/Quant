import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { research, type HypothesisDetail } from "../api";
import { Badge, ErrorBox, KV, Section, fmt, fmtTs } from "../components/ui";
import { StatusBadge } from "./Research";

export default function HypothesisDetailPage() {
  const { id } = useParams();
  const [d, setD] = useState<HypothesisDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const load = useCallback(() => {
    if (id) research.hypothesis(id).then(setD, (e) => setError(e.message));
  }, [id]);
  useEffect(() => {
    load();
    const t = window.setInterval(load, 4000);
    return () => window.clearInterval(t);
  }, [load]);
  if (!d) return error ? <ErrorBox error={error} /> : <p className="text-sm text-slate-500">Loading…</p>;
  const h = d.hypothesis;
  const c = h.contract;
  const act = async (fn: () => Promise<unknown>, msg: string) => {
    setError(null);
    try {
      await fn();
      setNotice(msg);
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const latestGates = d.gates.filter((g) => g.experiment_id === d.gates[0]?.experiment_id).sort((a, b) => a.gate.localeCompare(b.gate));
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <Link to="/research" className="text-sm text-indigo-700 dark:text-indigo-300">← Research Lab</Link>
        <h1 className="text-lg font-semibold">{h.title}</h1>
        <StatusBadge status={h.status} />
        <Badge>v{h.version}</Badge>
        <div className="ml-auto flex gap-2">
          <button className="btn" onClick={() => act(() => research.validate(h.id, true), "Validation queued; this page refreshes.")}>Run validation</button>
          <button
            className="btn-ghost"
            onClick={() => {
              if (window.confirm(h.final_test_accesses ? "The holdout was already examined. Another look is recorded and the test is no longer independent. Continue?" : "Examine the isolated holdout? Do this once, after you are done iterating."))
                act(() => research.finalTest(h.id), "Final test recorded.");
            }}
          >
            Final test ({h.final_test_accesses} accesses)
          </button>
          <button className="btn-ghost" onClick={() => act(() => research.transition(h.id, "REJECTED", "rejected by researcher"), "Rejected.")}>Reject</button>
        </div>
      </div>
      <ErrorBox error={error} />
      {notice && <div className="text-sm text-indigo-700 dark:text-indigo-300">{notice}</div>}

      {latestGates.length > 0 && (
        <Section title="Hard gates (latest validation)">
          <div className="grid gap-3 md:grid-cols-4">
            {latestGates.map((g) => (
              <div key={g.id} className="rounded-md border border-slate-200 p-3 dark:border-slate-800">
                <div className="mb-1 flex items-center gap-2 text-sm font-semibold">
                  Gate {g.gate} {g.decision === "pass" ? <Badge tone="green">✓ pass</Badge> : <Badge tone="red">✗ block</Badge>}
                </div>
                <ul className="space-y-0.5 text-xs">{g.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
              </div>
            ))}
          </div>
          <p className="mt-2 text-xs text-slate-500">Gates are evaluated by the deterministic policy engine; no user or agent can override them. Gates E (portfolio risk) and F (operational authorisation) arrive in later phases.</p>
        </Section>
      )}

      <Section title="Experiments (all runs, including failed ones)">
        <table className="min-w-full">
          <tbody>
            {d.experiments.map((e) => (
              <tr key={e.id}>
                <td className="td">{e.kind}</td>
                <td className="td"><Badge tone={e.status === "succeeded" ? "green" : e.status === "failed" ? "red" : "slate"}>{e.status}</Badge></td>
                <td className="td text-xs">{fmtTs(e.created_at)}</td>
                <td className="td text-xs">{e.decision ?? ""} {e.contaminated ? <Badge tone="amber">holdout contaminated</Badge> : null}</td>
                <td className="td text-xs">{e.oos_sharpe_net !== null && e.oos_sharpe_net !== undefined ? `OOS Sharpe net ${fmt(e.oos_sharpe_net)}` : ""}</td>
                <td className="td text-xs">{e.status === "succeeded" && <Link className="underline" to={`/validations/${e.id}`}>report</Link>}{e.error}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {d.experiments.length === 0 && <p className="text-sm text-slate-500">Not validated yet.</p>}
      </Section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Contract">
          <KV data={{ premise: c.premise, mechanism: c.mechanism, strategy: c.strategy, entry: c.entry_rule, exit: c.exit_rule, horizon_bars: c.horizon_bars, benchmark: c.benchmark, features: c.features, risks: c.risks, invalidation: c.invalidation, "contract hash": h.contract_hash.slice(0, 16) }} />
        </Section>
        <Section title="Pre-registered success criteria and validation design">
          <KV data={{ ...c.success, ...c.validation }} />
        </Section>
      </div>
      {d.versions.length > 1 && (
        <Section title="Versions in this family (trials are counted together)">
          <ul className="text-sm">{d.versions.map((v) => <li key={v.id}><Link className="underline" to={`/research/${v.id}`}>v{v.version} {v.title}</Link> <StatusBadge status={v.status} /></li>)}</ul>
        </Section>
      )}
    </div>
  );
}

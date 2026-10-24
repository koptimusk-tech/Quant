import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { alpha, type AgentMessage, type AlphaHypothesisRow, type AlphaRun, type FactorModule } from "../api";
import { Badge, ErrorBox, Section, fmt, fmtTs } from "../components/ui";
import { StatusBadge } from "./Research";

export function EvidenceBadge({ level }: { level: string }) {
  const tone = level === "walk_forward_passed" ? "green" : level === "no_evidence" || level === "error" ? "red" : "slate";
  const label: Record<string, string> = {
    not_tested: "not tested",
    no_evidence: "no OOS evidence",
    walk_forward_passed: "passed walk-forward",
    error: "run failed",
  };
  return <Badge tone={tone}>{label[level] ?? level}</Badge>;
}

const ORIGIN: Record<string, string> = {
  anomaly: "anomaly",
  prior: "documented effect",
  factor: "factor",
  relationship: "relationship scan",
  manual: "manual",
};

export default function AlphaResearchPage() {
  const [runs, setRuns] = useState<AlphaRun[]>([]);
  const [rows, setRows] = useState<AlphaHypothesisRow[]>([]);
  const [factors, setFactors] = useState<FactorModule[]>([]);
  const [gaps, setGaps] = useState<AgentMessage[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [budget, setBudget] = useState({ max_experiments: 6, max_hypotheses: 12, include_synthetic: false });

  const load = useCallback(() => {
    alpha.runs().then((r) => {
      setRuns(r);
      if (r[0]) alpha.run(r[0].id).then((d) => setGaps(d.messages.filter((m) => m.kind === "data_gap")), () => undefined);
    }, (e) => setError(e.message));
    alpha.hypotheses().then(setRows, (e) => setError(e.message));
    alpha.factors(budget.include_synthetic).then(setFactors, () => undefined);
  }, [budget.include_synthetic]);

  useEffect(() => {
    load();
    const t = window.setInterval(load, 5000);
    return () => window.clearInterval(t);
  }, [load]);

  const start = async () => {
    setError(null);
    try {
      await alpha.startRun(budget);
      setNotice("Research cycle queued. Results appear below when it finishes.");
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-lg font-semibold">Alpha Research</h1>
        <p className="max-w-4xl text-sm text-slate-600 dark:text-slate-400">
          Four deterministic agents propose, formalise, challenge and schedule hypotheses. Every number they quote is
          copied from a stored record; results come only from the validation engine and the policy gates. Research
          priority, predictive evidence and validated net profitability are shown separately: a high priority is not
          evidence, and passing walk-forward is not a profitable strategy.
        </p>
      </div>
      <ErrorBox error={error} />
      {notice && <div className="text-sm text-indigo-700 dark:text-indigo-300">{notice}</div>}

      <Section title="Run a research cycle">
        <div className="flex flex-wrap items-end gap-3 text-sm">
          <label className="flex flex-col">
            Max experiments
            <input aria-label="Max experiments" className="input w-28" type="number" min={0} max={30} value={budget.max_experiments}
              onChange={(e) => setBudget({ ...budget, max_experiments: Number(e.target.value) })} />
          </label>
          <label className="flex flex-col">
            Max hypotheses
            <input aria-label="Max hypotheses" className="input w-28" type="number" min={0} max={50} value={budget.max_hypotheses}
              onChange={(e) => setBudget({ ...budget, max_hypotheses: Number(e.target.value) })} />
          </label>
          <label className="flex items-center gap-1">
            <input type="checkbox" checked={budget.include_synthetic}
              onChange={(e) => setBudget({ ...budget, include_synthetic: e.target.checked })} />
            include synthetic data
          </label>
          <button className="btn" onClick={start}>Run research cycle</button>
          <span className="text-xs text-slate-500">No language model is called (0 tokens). Live execution stays disabled.</span>
        </div>
      </Section>

      <Section title="Research cycles">
        <table className="min-w-full text-sm">
          <thead>
            <tr>
              {["Started", "Status", "Ideas", "Registered", "Duplicates", "Rejected", "Data gaps", "Experiments", "Passed", "Failed", "Variants", "Seconds"].map((h) => (
                <th key={h} className="th">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.id}>
                <td className="td text-xs">{fmtTs(r.created_at)}</td>
                <td className="td"><Badge tone={r.status === "succeeded" ? "green" : r.status === "failed" ? "red" : "slate"}>{r.status}</Badge></td>
                <td className="td">{r.n_ideas ?? ""}</td>
                <td className="td">{r.n_registered ?? ""}</td>
                <td className="td">{r.n_duplicates ?? ""}</td>
                <td className="td">{r.n_rejected_by_review ?? ""}</td>
                <td className="td">{r.n_data_gaps ?? ""}</td>
                <td className="td">{r.n_experiments ?? ""}</td>
                <td className="td">{r.n_validation_passed ?? ""}</td>
                <td className="td">{r.n_validation_failed ?? ""}</td>
                <td className="td">{r.n_variants_backtested ?? ""}</td>
                <td className="td">{fmt(r.duration_s)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {runs.length === 0 && <p className="text-sm text-slate-500">No research cycle yet.</p>}
      </Section>

      <Section title="Hypotheses">
        <div className="overflow-x-auto">
          <table className="min-w-full text-sm">
            <thead>
              <tr>
                {["Hypothesis", "Origin", "State", "Research priority", "Predictive evidence", "Validated net profitability", "Objections"].map((h) => (
                  <th key={h} className="th">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((h) => (
                <tr key={h.id}>
                  <td className="td">
                    <Link className="underline" to={`/alpha/${h.id}`}>{h.title}</Link>
                    <div className="max-w-md text-xs text-slate-500">{h.origin_observation}</div>
                  </td>
                  <td className="td text-xs">{ORIGIN[h.origin] ?? h.origin}{h.family ? ` · ${h.family}` : ""}</td>
                  <td className="td"><StatusBadge status={h.status} /></td>
                  <td className="td text-xs">{h.research_priority === null ? "—" : fmt(h.research_priority)}<div className="text-slate-500">heuristic</div></td>
                  <td className="td text-xs"><EvidenceBadge level={h.predictive_evidence.level} /><div className="max-w-xs">{h.predictive_evidence.text}</div></td>
                  <td className="td text-xs">
                    <Badge tone={h.validated_net_profitability.established ? "green" : "slate"}>
                      {h.validated_net_profitability.established ? "established" : "not established"}
                    </Badge>
                  </td>
                  <td className="td text-xs">
                    {h.objections.major} major · {h.objections.minor} minor
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {rows.length === 0 && <p className="text-sm text-slate-500">No hypotheses yet.</p>}
      </Section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Factor modules">
          <ul className="space-y-2 text-sm">
            {factors.map((f) => (
              <li key={f.name}>
                <span className="font-medium">{f.title}</span>{" "}
                {f.available ? <Badge tone="green">testable</Badge> : <Badge tone="amber">not testable</Badge>}
                <div className="text-xs text-slate-500">{f.formula}</div>
                {f.available ? (
                  <div className="text-xs">Universe: {f.universes.map((u) => `${u.symbols.join(", ")} (${u.common_bars} bars)`).join("; ")}</div>
                ) : (
                  <div className="text-xs text-amber-700 dark:text-amber-300">{f.reason}</div>
                )}
              </li>
            ))}
          </ul>
        </Section>
        <Section title="What the lab cannot test yet (latest cycle)">
          <ul className="space-y-1 text-sm">
            {gaps.map((g) => (
              <li key={g.id}>
                <Badge>{g.payload.family}</Badge> {g.payload.reason}
              </li>
            ))}
          </ul>
          {gaps.length === 0 && <p className="text-sm text-slate-500">Nothing recorded.</p>}
        </Section>
      </div>
    </div>
  );
}

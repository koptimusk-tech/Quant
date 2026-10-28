import { isValidElement, useCallback, useEffect, useState } from "react";
import { review as api } from "../api";
import { Badge, ErrorBox, Section, fmt, fmtTs } from "../components/ui";

type Row = Record<string, any>;

const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(2)}%`);
const ci = (v: number[] | null | undefined, f: (x: number) => string = fmt) => (v ? `[${f(v[0])}, ${f(v[1])}]` : "—");

// No green for any state: none of them means the strategy is profitable or ready to trade.
const STATE_TONE: Record<string, "red" | "amber" | "slate" | "indigo"> = {
  SUPPORTED_FOR_FURTHER_RESEARCH: "indigo", COMPLETED: "slate", REJECTED: "red", PAUSED_REVIEW: "amber",
  INCONCLUSIVE: "slate", CONTINUE_PAPER: "indigo",
};
// Historical, prospective and synthetic evidence each have their own colour and wording.
const EVIDENCE: Record<string, { tone: "amber" | "indigo" | "slate"; text: string }> = {
  PROSPECTIVE: { tone: "indigo", text: "PROSPECTIVE · simulated on new data" },
  HISTORICAL: { tone: "amber", text: "HISTORICAL · replay of known data" },
  SYNTHETIC: { tone: "slate", text: "SYNTHETIC · not market evidence" },
  NEW_DATA_WITHOUT_PROTOCOL: { tone: "slate", text: "NEW DATA · no protocol" },
};
const CHECK_TONE: Record<string, "red" | "amber" | "slate" | "indigo"> = {
  PASS: "slate", WARN: "amber", FAIL: "red", NOT_EVALUABLE: "amber",
};
const CAUSE_TONE: Record<string, "red" | "amber" | "slate" | "indigo"> = {
  CONFIRMED: "red", COMPATIBLE: "amber", NOT_EVALUABLE: "slate", RULED_OUT_BY_DATA: "slate",
};

function State({ s }: { s: string | null | undefined }) {
  if (!s) return <span className="text-xs text-slate-500">—</span>;
  return <Badge tone={STATE_TONE[s] ?? "slate"}>{s.replace(/_/g, " ")}</Badge>;
}

function Evidence({ c }: { c: string }) {
  const e = EVIDENCE[c] ?? { tone: "slate" as const, text: c };
  return <Badge tone={e.tone}>{e.text}</Badge>;
}

// Objects are shown as their scalar fields; lists by their size (the full values are in the report).
function compact(v: unknown): string {
  if (Array.isArray(v)) return `${v.length} item(s)`;
  if (v && typeof v === "object") {
    const parts = Object.entries(v as Row).filter(([, x]) => typeof x === "number" || typeof x === "boolean" || x === null)
      .slice(0, 5).map(([k, x]) => `${k.replace(/_/g, " ")} ${fmt(x)}`);
    return parts.join(" · ") || "see report";
  }
  return fmt(v);
}

function Metric({ m }: { m: Row | undefined }) {
  if (!m) return <span>—</span>;
  if (m.status !== "OK") return <span className="text-amber-700 dark:text-amber-400" title={m.note}>INSUFFICIENT DATA (n={m.n})</span>;
  return <span title={m.note}>{compact(m.value)} <span className="text-slate-400">n={m.n}</span></span>;
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

function Progress({ bars, horizon }: { bars: number; horizon: number | null }) {
  if (!horizon) return <span className="text-xs text-slate-500">{bars} bars · no protocol</span>;
  const p = Math.min(1, bars / horizon);
  return (
    <div className="min-w-[90px]" title="Progress of the protocol's planned horizon (not a performance measure)">
      <div className="h-1.5 rounded bg-slate-200 dark:bg-slate-700">
        <div className="h-1.5 rounded bg-slate-500" style={{ width: `${p * 100}%` }} />
      </div>
      <span className="text-[11px] text-slate-500">{bars} / {horizon} bars</span>
    </div>
  );
}

function Facts({ data }: { data: Record<string, unknown> }) {
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
      {Object.entries(data).map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-slate-500">{k}</dt>
          <dd className="min-w-0 tabular-nums [overflow-wrap:anywhere]">{isValidElement(v) ? v : fmt(v)}</dd>
        </div>
      ))}
    </dl>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-xs text-slate-500">
      {label}
      {children}
    </label>
  );
}

const input = "rounded border border-slate-300 bg-white px-2 py-1 text-sm text-slate-900 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100";

function MetricGroup({ title, group }: { title: string; group: Row }) {
  return (
    <div>
      <h3 className="mb-1 text-xs font-semibold uppercase text-slate-500">{title}</h3>
      <Table cols={[["Metric", (r) => r.k.replace(/_/g, " ")], ["Value", (r) => <Metric m={r.m} />], ["Notes", (r) => r.m.unit || ""]]}
        rows={Object.entries(group).map(([k, m]) => ({ k, m }))} />
    </div>
  );
}

function Detail({ id, onChange }: { id: string; onChange: () => void }) {
  const [d, setD] = useState<Row | null>(null);
  const [hist, setHist] = useState<Row[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [by, setBy] = useState("");
  const [state, setState] = useState("");
  const [why, setWhy] = useState("");
  const [regen, setRegen] = useState<Row | null>(null);

  const load = useCallback(() => {
    api.decision(id).then((x) => {
      setD(x);
      setState(x.report.sections["11_final_state"].engine.state);
      return api.history(x.summary.subject).then(setHist);
    }).catch((e) => setErr(String(e)));
  }, [id]);
  useEffect(load, [load]);

  if (!d) return <ErrorBox error={err} />;
  const rep = d.report;
  const sec = rep.sections;
  const eng = sec["11_final_state"].engine;
  const cur = sec["11_final_state"].current;
  const cmp = sec["6_comparison_backtest_and_baseline"];
  const unc = sec["7_uncertainty"];
  const crit = sec["9_criteria"];
  const diag = sec["8_operational_incidents"].diagnosis;
  const s = d.summary;

  const submit = () => {
    setErr(null);
    api.manual(id, { by, state, rationale: why }).then(() => { setWhy(""); load(); onChange(); }).catch((e) => setErr(String(e)));
  };

  return (
    <div className="space-y-4">
      <Section title={`Status: ${rep.strategy} v${rep.version ?? "—"}`} right={<Evidence c={rep.evidence_class} />}>
        <p className="mb-2 text-xs text-slate-500">{rep.not_an_authorisation}</p>
        <Facts data={{
          "Engine decision": <State s={eng.state} />, "Current state": <span><State s={cur.state} /> <span className="text-xs text-slate-500">{cur.source}</span></span>,
          Mode: rep.label, "Decided": fmtTs(rep.decided_at), "Engine version": rep.engine_version,
          "Record": <span className="font-mono text-xs">{rep.record_hash.slice(0, 16)} {rep.integrity_ok ? "" : "(hash mismatch)"}</span>,
          "Protocol progress": <Progress bars={s.bars} horizon={s.horizon_bars} />,
          "Period observed": `${fmtTs(s.first_bar)} → ${fmtTs(s.last_bar)}`,
          Trades: `${s.trades} (${s.independent_trades ?? "—"} independent)`,
        }} />
        <div className="mt-2 flex gap-3 text-xs">
          <a className="underline" href={api.reportJsonUrl(id)}>Report JSON</a>
          <a className="underline" href={api.reportMdUrl(id)}>Report Markdown</a>
          <button className="underline" onClick={() => api.regenerate(id).then(setRegen).catch((e) => setErr(String(e)))}>Regenerate and compare</button>
          {regen && <span>{regen.reproducible === true ? "reproduced: same outputs" : regen.reproducible === false ? `differs: ${regen.differences?.join(", ")}` : regen.reason}</span>}
        </div>
      </Section>

      <Section title="Decision and justification">
        <p className="mb-2 text-sm"><State s={eng.state} /> {eng.reason}</p>
        <Table cols={[["Rule", (r) => r.rule.replace(/_/g, " ")], ["Applies", (r) => (r.applies ? "yes" : "no")], ["Detail", (r) => r.detail]]} rows={eng.rules} />
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <div>
            <h3 className="text-xs font-semibold uppercase text-slate-500">What we can conclude</h3>
            <ul className="list-disc pl-4 text-sm">{rep.what_we_can_and_cannot_conclude.can_conclude.map((x: string) => <li key={x}>{x}</li>)}</ul>
          </div>
          <div>
            <h3 className="text-xs font-semibold uppercase text-slate-500">What we cannot conclude</h3>
            <ul className="list-disc pl-4 text-sm">{rep.what_we_can_and_cannot_conclude.cannot_conclude.map((x: string) => <li key={x}>{x}</li>)}</ul>
          </div>
        </div>
        <h3 className="mt-3 text-xs font-semibold uppercase text-slate-500">Uncertainties</h3>
        <ul className="list-disc pl-4 text-xs">{eng.uncertainties.map((x: string) => <li key={x}>{x}</li>)}</ul>
        <h3 className="mt-3 text-xs font-semibold uppercase text-slate-500">Recommended actions</h3>
        <ul className="list-disc pl-4 text-xs">{eng.recommended_actions.map((x: string) => <li key={x}>{x}</li>)}</ul>
      </Section>

      <Section title="Quality and integrity of the results" right={<Badge tone={sec["4_data_quality"].integrity.status === "BLOCKED" ? "red" : sec["4_data_quality"].integrity.status === "LIMITED" ? "amber" : "slate"}>{sec["4_data_quality"].integrity.status}</Badge>}>
        <Table cols={[["Check", (r) => r.check.replace(/_/g, " ")], ["Status", (r) => <Badge tone={CHECK_TONE[r.status]}>{r.status}{r.material ? " · material" : ""}</Badge>], ["Detail", (r) => r.detail]]}
          rows={sec["4_data_quality"].integrity.checks} />
        <p className="mt-1 text-xs text-slate-500">{sec["4_data_quality"].integrity.note}</p>
      </Section>

      <Section title="Metrics">
        <div className="grid gap-4 lg:grid-cols-3">
          <MetricGroup title="Performance" group={sec["5_performance_and_risk"].performance} />
          <MetricGroup title="Risk" group={sec["5_performance_and_risk"].risk} />
          <MetricGroup title="Execution (simulated)" group={sec["5_performance_and_risk"].execution} />
        </div>
        <p className="mt-1 text-xs text-slate-500">{sec["5_performance_and_risk"].note}</p>
      </Section>

      <Section title="Comparison with the baseline">
        {cmp.baseline ? (
          <Facts data={{
            Baseline: cmp.baseline.baseline, "Mean excess per bar": fmt(cmp.baseline.excess_mean),
            "Excess 95% CI": ci(cmp.baseline.excess_ci95), "One-sided p (HAC)": fmt(cmp.baseline.test?.p_one_sided),
            "Holm-adjusted p": fmt(unc.multiple_testing?.adjusted_p), "Effective observations": fmt(unc.effective_n),
          }} />
        ) : <p className="text-xs text-slate-500">{cmp.note}</p>}
        <p className="mt-2 text-xs">{unc.layers?.statistical_evidence}</p>
        <p className="text-xs">{unc.layers?.economic_evidence}</p>
        <p className="mt-1 text-xs text-slate-500">{unc.p_note}</p>
      </Section>

      <Section title="Comparison with the backtest" right={<Badge tone={cmp.comparable ? "slate" : "amber"}>{cmp.comparable ? "comparable" : "not comparable"}</Badge>}>
        {cmp.compatibility && (
          <Table cols={[["Compatibility", (r) => r.check.replace(/_/g, " ")], ["", (r) => (r.compatible ? "yes" : r.compatible === false ? "NO" : "—")], ["Detail", (r) => r.detail]]} rows={cmp.compatibility} />
        )}
        {cmp.note && <p className="mt-1 text-xs text-slate-500">{cmp.note}</p>}
        {cmp.backtest && (
          <Facts data={{
            "Paper total": pct(cmp.backtest.returns?.paper_total), "Backtest total": pct(cmp.backtest.returns?.backtest_total),
            "Difference per bar 95% CI (bps)": ci(cmp.backtest.returns?.diff_ci95_bps), Correlation: fmt(cmp.backtest.returns?.correlation),
            "Drawdown paper / backtest": `${pct(cmp.backtest.drawdown?.paper)} / ${pct(cmp.backtest.drawdown?.backtest)}`,
            "Costs paper / backtest": fmt(cmp.backtest.costs?.ratio), "Signal agreement": fmt(cmp.backtest.signal_frequency?.agreement),
          }} />
        )}
        {(cmp.deviations ?? []).length > 0 && (
          <Table cols={[["Deviation", (r) => r.item], ["Measured", (r) => (typeof r.measured === "string" ? r.measured : JSON.stringify(r.measured))],
            ["Possible explanations", (r) => r.possible_explanations.join("; ")], ["Data to tell them apart", (r) => r.data_to_distinguish], ["Cause", (r) => r.cause]]} rows={cmp.deviations} />
        )}
        {cmp.research_oos_context && <p className="mt-2 text-xs text-slate-500">Research out-of-sample (context only, another period): Sharpe {fmt(cmp.research_oos_context.sharpe_net)}, {fmt(cmp.research_oos_context.n_trades)} trades. {cmp.research_oos_context.note}</p>}
      </Section>

      <Section title="Acceptance criteria (fixed before the start)">
        <Table cols={[["Criterion", (r) => r.criterion.replace(/_/g, " ")], ["Kind", (r) => r.kind], ["Met", (r) => (!r.evaluable ? "not evaluable" : r.met ? "yes" : "no")], ["Detail", (r) => r.detail]]}
          rows={crit.protocol_criteria} empty="No protocol: no predefined criteria." />
        <h3 className="mt-3 text-xs font-semibold uppercase text-slate-500">Readiness checks</h3>
        <Table cols={[["Check", (r) => r.check.replace(/_/g, " ")], ["Met", (r) => (r.met === null ? "not evaluable" : r.met ? "yes" : "no")], ["Detail", (r) => r.detail], ["Source", (r) => r.source]]}
          rows={crit.readiness_checks} empty="—" />
        {(sec["2_original_protocol"].amendments ?? []).length > 0 && (
          <>
            <h3 className="mt-3 text-xs font-semibold uppercase text-slate-500">Registered amendments (not used for the decision)</h3>
            <Table cols={[["#", (r) => r.number], ["By", (r) => r.by], ["When", (r) => (r.post_hoc ? "after results were seen" : "before results")], ["Changes", (r) => JSON.stringify(r.changes)],
              ["Rule state original → amended", (r) => `${r.rule_state_original} → ${r.rule_state_amended}`], ["Why", (r) => r.justification]]} rows={sec["2_original_protocol"].amendments} />
          </>
        )}
      </Section>

      <Section title="Alerts, incidents and diagnosis">
        <Facts data={sec["8_operational_incidents"].incidents} />
        <Table cols={[["When", (r) => fmtTs(r.at)], ["Bar", (r) => fmtTs(r.sim_ts)], ["Event", (r) => r.kind], ["Detail", (r) => r.detail]]}
          rows={sec["8_operational_incidents"].timeline} empty="No state changes recorded." />
        {diag?.triggered && (
          <>
            <p className="mt-3 text-xs">Results worse than expected: {diag.why.join("; ")}</p>
            <Table cols={[["Possible cause", (r) => r.cause], ["Status", (r) => <Badge tone={CAUSE_TONE[r.status]}>{r.status.replace(/_/g, " ")}</Badge>], ["Evidence", (r) => r.evidence], ["Data to tell", (r) => r.data_to_distinguish]]} rows={diag.causes} />
            <p className="mt-1 text-xs text-slate-500">{diag.note}</p>
          </>
        )}
      </Section>

      <Section title="Limitations">
        <ul className="list-disc pl-4 text-xs">{sec["10_limitations"].map((x: string) => <li key={x}>{x}</li>)}</ul>
      </Section>

      <Section title="Decision history">
        <Table cols={[["When", (r) => fmtTs(r.at)], ["Source", (r) => r.kind], ["State", (r) => <State s={r.state} />], ["By", (r) => r.by], ["Reason", (r) => r.reason], ["Note", (r) => r.warning ?? ""]]} rows={hist} />
        {d.is_latest && (
          <div className="mt-3 grid gap-2 md:grid-cols-4">
            <Field label="Person"><input aria-label="Person" className={input} value={by} onChange={(e) => setBy(e.target.value)} /></Field>
            <Field label="Classification (same or more conservative)">
              <select aria-label="Classification" className={input} value={state} onChange={(e) => setState(e.target.value)}>
                {d.manual_allowed.map((x: string) => <option key={x} value={x}>{x}</option>)}
              </select>
            </Field>
            <Field label="Rationale"><input aria-label="Rationale" className={input} value={why} onChange={(e) => setWhy(e.target.value)} /></Field>
            <button className="btn self-end" onClick={submit} disabled={!by || why.length < 30}>Record manual review</button>
          </div>
        )}
        <ErrorBox error={err} />
      </Section>
    </div>
  );
}

export default function StrategyReviewPage() {
  const [rows, setRows] = useState<Row[]>([]);
  const [summ, setSumm] = useState<Row | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [by, setBy] = useState("");
  const [busy, setBusy] = useState(false);
  const [f, setF] = useState<Record<string, string>>({ strategy: "", market: "", asset: "", start: "", end: "", state: "", evidence_class: "" });

  const load = useCallback(() => {
    api.strategies(f).then(setRows).catch((e) => setErr(String(e)));
    api.summary().then(setSumm).catch((e) => setErr(String(e)));
  }, [f]);
  useEffect(load, [load]);

  const run = () => {
    setBusy(true);
    setErr(null);
    api.run(by).then(load).catch((e) => setErr(String(e))).finally(() => setBusy(false));
  };
  const set = (k: string) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setF({ ...f, [k]: e.target.value });

  return (
    <div className="space-y-4">
      <Section title="Strategy Review & Readiness" right={summ && <Badge tone={summ.status === "INSUFFICIENT_EVIDENCE" ? "amber" : "slate"}>{summ.status.replace(/_/g, " ")}</Badge>}>
        <p className="text-xs text-slate-500">
          Deterministic review of the paper trading results under the protocols fixed before each program started. Simulation only: no state, including
          COMPLETED and SUPPORTED FOR FURTHER RESEARCH, authorises real trading or sends any order.
        </p>
        {summ && (
          <div className="mt-2 text-xs">
            <p>{summ.note}</p>
            {summ.to_collect.length > 0 && <ul className="mt-1 list-disc pl-4">{summ.to_collect.map((x: string) => <li key={x}>Still to collect: {x}</li>)}</ul>}
          </div>
        )}
        <div className="mt-3 flex items-end gap-2">
          <Field label="Run by"><input aria-label="Run by" className={input} value={by} onChange={(e) => setBy(e.target.value)} /></Field>
          <button className="btn" onClick={run} disabled={busy || by.length < 2}>{busy ? "Reviewing…" : "Run review"}</button>
        </div>
        <ErrorBox error={err} />
      </Section>

      <Section title="Strategies evaluated">
        <div className="mb-3 grid gap-2 md:grid-cols-7">
          <Field label="Strategy"><input aria-label="Strategy filter" className={input} value={f.strategy} onChange={set("strategy")} /></Field>
          <Field label="Market"><input aria-label="Market filter" className={input} value={f.market} onChange={set("market")} placeholder="equity, crypto…" /></Field>
          <Field label="Asset"><input aria-label="Asset filter" className={input} value={f.asset} onChange={set("asset")} /></Field>
          <Field label="From"><input aria-label="From" type="date" className={input} value={f.start} onChange={set("start")} /></Field>
          <Field label="To"><input aria-label="To" type="date" className={input} value={f.end} onChange={set("end")} /></Field>
          <Field label="State">
            <select aria-label="State filter" className={input} value={f.state} onChange={set("state")}>
              <option value="">any</option>
              {["CONTINUE_PAPER", "PAUSED_REVIEW", "REJECTED", "INCONCLUSIVE", "COMPLETED", "SUPPORTED_FOR_FURTHER_RESEARCH"].map((x) => <option key={x} value={x}>{x}</option>)}
            </select>
          </Field>
          <Field label="Evidence">
            <select aria-label="Evidence filter" className={input} value={f.evidence_class} onChange={set("evidence_class")}>
              <option value="">any</option>
              {Object.keys(EVIDENCE).map((x) => <option key={x} value={x}>{x}</option>)}
            </select>
          </Field>
        </div>
        <Table empty="No reviewed strategy. Run a review; if no paper session exists, the result is insufficient evidence."
          cols={[
            ["Strategy", (r) => `${r.strategy} v${r.version ?? "—"}`], ["Evidence", (r) => <Evidence c={r.evidence_class} />],
            ["State", (r) => <span><State s={r.current?.state ?? r.state} />{r.current?.source === "manual review" ? " (reviewed)" : ""}</span>],
            ["Protocol", (r) => <Progress bars={r.bars} horizon={r.horizon_bars} />], ["Period", (r) => `${fmtTs(r.first_bar)} → ${fmtTs(r.last_bar)}`],
            ["Trades", (r) => `${r.trades} (${r.independent_trades ?? "—"} ind.)`], ["Return", (r) => <Metric m={r.cumulative_return} />],
            ["Excess vs baseline 95% CI", (r) => ci(r.excess_ci95)], ["vs backtest (bps CI)", (r) => (r.comparable ? ci(r.backtest_diff_ci95_bps) : "not comparable")],
            ["Integrity", (r) => <Badge tone={r.integrity === "BLOCKED" ? "red" : r.integrity === "LIMITED" ? "amber" : "slate"}>{r.integrity}</Badge>],
            ["Open incidents", (r) => r.open_incidents], ["Criteria met", (r) => `${r.criteria_met}/${r.criteria_total}`],
            ["", (r) => <button className="underline" onClick={() => setSel(r.decision_id)}>Open</button>],
          ]} rows={rows} />
      </Section>

      {sel && <Detail key={sel} id={sel} onChange={load} />}
    </div>
  );
}

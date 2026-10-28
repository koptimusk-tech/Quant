import { useCallback, useEffect, useMemo, useState } from "react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { program as api } from "../api";
import { Badge, ErrorBox, KV, Section, SyntheticBadge, fmt, fmtTs } from "../components/ui";
import { chartColors } from "../components/theme";

type Row = Record<string, any>;

const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(2)}%`);
const ci = (v: number[] | null | undefined, f: (x: number) => string = fmt) => (v ? `[${f(v[0])}, ${f(v[1])}]` : "—");

const STATE_TONE: Record<string, "green" | "red" | "amber" | "slate" | "indigo"> = {
  COMPLETED: "green", REJECTED: "red", PAUSED_REVIEW: "amber", INCONCLUSIVE: "slate", CONTINUE_PAPER: "indigo",
};
// Each mode has its own colour and wording so historical, prospective and synthetic results are never confused.
const MODE_STYLE: Record<string, { tone: "amber" | "green" | "slate"; border: string; text: string }> = {
  A: { tone: "amber", border: "border-amber-400", text: "HISTORICAL REPLAY · not prospective" },
  B: { tone: "green", border: "border-emerald-500", text: "PROSPECTIVE PAPER · simulated" },
  C: { tone: "slate", border: "border-slate-400 border-dashed", text: "SYNTHETIC SIMULATION · not market evidence" },
};

export function ProgramState({ s }: { s: string | null | undefined }) {
  if (!s) return <span className="text-xs text-slate-500">—</span>;
  return <Badge tone={STATE_TONE[s] ?? "slate"}>{s.replace(/_/g, " ")}</Badge>;
}

export function ModeBadge({ mode }: { mode: string }) {
  const m = MODE_STYLE[mode] ?? MODE_STYLE.C;
  return <Badge tone={m.tone}>{m.text}</Badge>;
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

function Curves({ ts, series, height = 220 }: { ts: string[]; series: Record<string, (number | null)[]>; height?: number }) {
  const c = chartColors();
  const names = Object.keys(series);
  const data = ts.map((t, i) => Object.fromEntries([["t", t], ...names.map((n) => [n, series[n][i]])]));
  return (
    <div style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={c.grid} vertical={false} />
          <XAxis dataKey="t" tick={{ fontSize: 11, fill: c.axis }} minTickGap={50} />
          <YAxis domain={["auto", "auto"]} tick={{ fontSize: 11, fill: c.axis }} width={56} />
          <Tooltip formatter={(v: number) => fmt(v)} />
          <Legend wrapperStyle={{ fontSize: 11 }} />
          {names.map((n, i) => <Line key={n} dataKey={n} stroke={c.series[i]} dot={false} isAnimationActive={false} />)}
        </LineChart>
      </ResponsiveContainer>
    </div>
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

// -- 1 · registry ----------------------------------------------------------------------------------------
function Registry({ onChange }: { onChange: () => void }) {
  const [elig, setElig] = useState<Row[]>([]);
  const [cands, setCands] = useState<Row[]>([]);
  const [form, setForm] = useState({ lab: "", name: "", by: "", justification: "", exploratory: false, allocation: "0.3" });
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(() => {
    api.eligibility().then(setElig).catch((e) => setErr(String(e)));
    api.candidates().then(setCands).catch((e) => setErr(String(e)));
  }, []);
  useEffect(load, [load]);
  const admit = async () => {
    setErr(null);
    try {
      await api.admit({ lab_experiment_id: form.lab, name: form.name, admitted_by: form.by, justification: form.justification,
        exploratory: form.exploratory, allocation: Number(form.allocation) });
      load();
      onChange();
    } catch (e) {
      setErr(String(e));
      load();
    }
  };
  return (
    <Section title="1 · Candidate registry">
      <p className="mb-2 text-xs text-slate-500">
        A candidate is a frozen, hashed snapshot of a completed lab experiment. Admission needs the lab decision OOS_SUPPORTED or better and a
        robustness state ROBUST_UNDER_TESTED_ASSUMPTIONS, or an explicit exploratory decision by a named person. A good backtest alone never admits a candidate.
      </p>
      <h3 className="mb-1 text-xs font-semibold">Lab experiments and their admission checks</h3>
      <Table rows={elig} empty="No completed lab experiment."
        cols={[["experiment", (r) => <span>{r.title} {r.is_synthetic && <SyntheticBadge />}</span>], ["decision", (r) => r.decision ?? "—"],
          ["robustness", (r) => r.robustness_state ?? "none"],
          ["evidence requirements", (r) => (r.meets_evidence_requirements ? <Badge tone="green">met</Badge> : <Badge tone="red">not met</Badge>)],
          ["failed checks", (r) => r.checks.filter((c: Row) => !c.passed).map((c: Row) => c.detail).join("; ") || "none"],
          ["", (r) => <button className="btn-ghost" onClick={() => setForm({ ...form, lab: r.lab_experiment_id, name: r.title ?? "", exploratory: !r.meets_evidence_requirements })}>Select</button>]]} />
      <div className="mt-3 grid grid-cols-1 gap-2 md:grid-cols-3">
        <Field label="Lab experiment id"><input className="input" value={form.lab} onChange={(e) => setForm({ ...form, lab: e.target.value })} /></Field>
        <Field label="Candidate name"><input className="input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></Field>
        <Field label="Admitted by (a person)"><input className="input" value={form.by} onChange={(e) => setForm({ ...form, by: e.target.value })} /></Field>
        <Field label="Allocation"><input className="input" value={form.allocation} onChange={(e) => setForm({ ...form, allocation: e.target.value })} /></Field>
        <div className="md:col-span-2">
          <Field label="Justification (written before the run)"><textarea className="input" rows={2} value={form.justification} onChange={(e) => setForm({ ...form, justification: e.target.value })} /></Field>
        </div>
        <label className="flex items-center gap-2 text-xs">
          <input type="checkbox" checked={form.exploratory} onChange={(e) => setForm({ ...form, exploratory: e.target.checked })} />
          Exploratory admission (evidence requirements not met; stays labelled exploratory)
        </label>
        <div><button className="btn" onClick={admit}>Admit candidate</button></div>
      </div>
      <ErrorBox error={err} />
      <h3 className="mb-1 mt-3 text-xs font-semibold">Registry (admitted and refused)</h3>
      <Table rows={cands} empty="No candidate yet."
        cols={[["name", (r) => `${r.name}${r.version ? ` v${r.version}` : ""}`], ["status", (r) => r.status],
          ["admission", (r) => (r.admission_kind === "exploratory" ? <Badge tone="amber">exploratory</Badge> : r.admission_kind ?? "—")],
          ["strategy", (r) => (r.strategy ? `${r.strategy} ${fmt(r.params)}` : "—")], ["universe", (r) => (r.universe ?? []).join(", ")],
          ["data", (r) => (r.is_synthetic ? <SyntheticBadge /> : r.status === "admitted" ? "real" : "—")], ["modes", (r) => (r.allowed_modes ?? []).join(", ")],
          ["integrity", (r) => (r.status !== "admitted" ? "—" : r.integrity_ok ? "hash ok" : <Badge tone="red">altered</Badge>)],
          ["by / reasons", (r) => r.admitted_by ?? (r.reasons ?? []).join("; ")], ["id", (r) => <code>{r.id}</code>]]} />
    </Section>
  );
}

// -- 2 · protocols -----------------------------------------------------------------------------------------
function Protocols({ onStarted }: { onStarted: (id: string) => void }) {
  const [protos, setProtos] = useState<Row[]>([]);
  const [form, setForm] = useState({ candidate_id: "", mode: "C", created_by: "", rationale: "", min_trades: "20", target_sharpe_se: "1.0", baseline: "no_trade", max_duration_bars: "1500" });
  const [preview, setPreview] = useState<Row | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(() => {
    api.protocols().then(setProtos).catch((e) => setErr(String(e)));
  }, []);
  useEffect(load, [load]);
  const body = () => ({ ...form, min_trades: Number(form.min_trades), target_sharpe_se: Number(form.target_sharpe_se), max_duration_bars: Number(form.max_duration_bars) });
  const run = async (f: () => Promise<unknown>) => {
    setErr(null);
    try {
      await f();
    } catch (e) {
      setErr(String(e));
    }
  };
  return (
    <Section title="2 · Protocols (fixed before the program starts)">
      <p className="mb-2 text-xs text-slate-500">
        The horizon is derived from the candidate (its trade frequency and the precision asked for), not a fixed length. A protocol is frozen and hashed when created, and governs one program.
      </p>
      <div className="grid grid-cols-1 gap-2 md:grid-cols-4">
        <Field label="Candidate id"><input className="input" value={form.candidate_id} onChange={(e) => setForm({ ...form, candidate_id: e.target.value })} /></Field>
        <Field label="Mode">
          <select className="input" value={form.mode} onChange={(e) => setForm({ ...form, mode: e.target.value })}>
            <option value="A">A · historical replay</option><option value="B">B · prospective paper</option><option value="C">C · synthetic simulation</option>
          </select>
        </Field>
        <Field label="Created by (a person)"><input className="input" value={form.created_by} onChange={(e) => setForm({ ...form, created_by: e.target.value })} /></Field>
        <Field label="Baseline">
          <select className="input" value={form.baseline} onChange={(e) => setForm({ ...form, baseline: e.target.value })}>
            <option value="no_trade">no_trade</option><option value="buy_and_hold">buy_and_hold</option>
          </select>
        </Field>
        <Field label="Minimum trades"><input className="input" value={form.min_trades} onChange={(e) => setForm({ ...form, min_trades: e.target.value })} /></Field>
        <Field label="Target Sharpe standard error"><input className="input" value={form.target_sharpe_se} onChange={(e) => setForm({ ...form, target_sharpe_se: e.target.value })} /></Field>
        <Field label="Maximum duration (bars)"><input className="input" value={form.max_duration_bars} onChange={(e) => setForm({ ...form, max_duration_bars: e.target.value })} /></Field>
        <Field label="Why these settings"><input className="input" value={form.rationale} onChange={(e) => setForm({ ...form, rationale: e.target.value })} /></Field>
      </div>
      <div className="mt-2 flex gap-2">
        <button className="btn-ghost" onClick={() => run(async () => setPreview(await api.preview(body())))}>Preview horizon</button>
        <button className="btn" onClick={() => run(async () => { await api.createProtocol(body()); setPreview(null); load(); })}>Freeze protocol</button>
      </div>
      {preview && (
        <div className="mt-2 rounded border border-slate-200 p-2 text-xs dark:border-slate-700">
          <KV data={{ "horizon (bars)": preview.horizon.horizon_bars, "bars for the trades": preview.horizon.bars_for_trades, "bars for the precision": preview.horizon.bars_for_precision,
            floor: preview.horizon.floor, "trade rate per bar": preview.horizon.trade_rate_per_bar, formula: preview.horizon.formula,
            "replay period": preview.replay ? `${fmtTs(preview.replay.start)} → ${fmtTs(preview.replay.end)} (${preview.replay.bars_available} bars)` : "new data only",
            "expected calendar days": preview.horizon.expected_calendar_days }} />
        </div>
      )}
      <ErrorBox error={err} />
      <Table rows={protos} empty="No protocol yet."
        cols={[["candidate", (r) => `${r.candidate_name} v${r.candidate_version}`], ["mode", (r) => <ModeBadge mode={r.mode} />],
          ["horizon", (r) => `${r.horizon_bars} bars`], ["frozen at", (r) => fmtTs(r.frozen_at)], ["by", (r) => r.created_by],
          ["integrity", (r) => (r.integrity_ok ? "hash ok" : <Badge tone="red">altered</Badge>)],
          ["program", (r) => (r.program_id ? <code>{r.program_id}</code> : (
            <button className="btn-ghost" onClick={() => run(async () => { const p = await api.start({ protocol_id: r.id, started_by: form.created_by }); load(); onStarted(p.id); })}>Start program</button>
          ))]]} />
    </Section>
  );
}

// -- 3..10 · one program ------------------------------------------------------------------------------------
function ProgramDetail({ id, onChange }: { id: string; onChange: () => void }) {
  const [d, setD] = useState<Row | null>(null);
  const [versions, setVersions] = useState<Row[]>([]);
  const [rep, setRep] = useState<Row | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [op, setOp] = useState({ by: "", reason: "", resolution: "", confirm: false, bars: "250", decision: "", rationale: "" });
  const load = useCallback(() => {
    api.get(id).then((x) => {
      setD(x);
      api.versions(x.summary.candidate_id).then(setVersions).catch(() => setVersions([]));
    }).catch((e) => setErr(String(e)));
  }, [id]);
  useEffect(load, [load]);
  const act = async (f: () => Promise<unknown>) => {
    setErr(null);
    try {
      await f();
      load();
      onChange();
    } catch (e) {
      setErr(String(e));
    }
  };
  const meta = useMemo(() => (d ? MODE_STYLE[d.summary.mode] ?? MODE_STYLE.C : MODE_STYLE.C), [d]);
  if (!d) return <ErrorBox error={err} />;
  const s = d.summary;
  const t = d.tracking ?? {};
  const cmp = d.comparison;
  const rule = d.rule;
  const allowed: string[] = d.review_allowed ?? [];
  return (
    <div className={`space-y-3 border-l-4 pl-3 ${meta.border}`}>
      <Section title={`3 · Session status: ${s.candidate_name} v${s.candidate_version}`} right={<ModeBadge mode={s.mode} />}>
        <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
          <span>Program {s.status}</span>
          <span>· rules: <ProgramState s={rule.state} /></span>
          <span>· classification: {d.classification.state ? <ProgramState s={d.classification.state} /> : <Badge tone="amber">pending review</Badge>}</span>
          {s.admission_kind === "exploratory" && <Badge tone="amber">exploratory candidate</Badge>}
          {s.is_synthetic && <SyntheticBadge />}
          {t.provisional && <Badge tone="amber">provisional</Badge>}
        </div>
        <KV data={{ "bars processed": `${t.bars ?? 0} of ${t.horizon_bars} planned`, trades: `${t.trades ?? 0} of ${t.min_trades}`, "session status": d.session.status,
          "pause reason": d.session.pause_reason, "kill switch": d.session.kill_switch ? "ACTIVE" : "off", cursor: fmtTs(d.session.cursor),
          "provisional because": (t.provisional_reasons ?? []).join("; ") || "—", protocol: s.protocol_id }} />
        <div className="mt-3 grid grid-cols-1 gap-2 md:grid-cols-4">
          <Field label="Person"><input className="input" value={op.by} onChange={(e) => setOp({ ...op, by: e.target.value })} /></Field>
          <Field label="Reason"><input className="input" value={op.reason} onChange={(e) => setOp({ ...op, reason: e.target.value })} /></Field>
          <Field label="How the cause was resolved"><input className="input" value={op.resolution} onChange={(e) => setOp({ ...op, resolution: e.target.value })} /></Field>
          <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={op.confirm} onChange={(e) => setOp({ ...op, confirm: e.target.checked })} />I confirm</label>
        </div>
        <div className="mt-2 flex flex-wrap gap-2">
          <input className="input w-20" aria-label="Bars to process" value={op.bars} onChange={(e) => setOp({ ...op, bars: e.target.value })} />
          <button className="btn" onClick={() => act(() => api.run(id, Number(op.bars)))}>Process bars</button>
          <button className="btn-ghost" onClick={() => act(() => api.pause(id, { by: op.by, reason: op.reason }))}>Pause</button>
          <button className="btn-ghost" onClick={() => act(() => api.resume(id, { by: op.by, reason: op.reason, resolution: op.resolution, confirm: op.confirm }))}>Resume (audited)</button>
          <button className="btn-ghost" onClick={() => act(() => api.stop(id, { by: op.by, reason: op.reason, confirm: op.confirm }))}>Stop early</button>
          <button className="btn-ghost" onClick={() => act(() => api.kill(id, { by: op.by, reason: op.reason, confirm: op.confirm, active: !d.session.kill_switch }))}>
            {d.session.kill_switch ? "Release kill switch" : "Kill switch"}
          </button>
        </div>
        <ErrorBox error={err} />
      </Section>

      <Section title="4 · Mode and data feed">
        <p className="mb-2 text-xs">{d.protocol.mode_note}</p>
        {d.protocol.replay && <p className="mb-2 text-xs text-slate-500">Replay period {fmtTs(d.protocol.replay.start)} → {fmtTs(d.protocol.replay.end)}: {d.protocol.replay.note}.</p>}
        <KV data={{ "prospective bars": s.prospective_bars, "missing or skipped bars": d.missing_bars, "protocol frozen at": fmtTs(d.protocol.frozen_at) }} />
        <Table rows={(d.feed_log ?? []).slice(-15).reverse()} empty="No bar processed yet."
          cols={[["bar", (r) => fmtTs(r.bar)], ["closed", (r) => fmtTs(r.closed)], ["available", (r) => fmtTs(r.available)], ["processed", (r) => fmtTs(r.processed_at)],
            ["delay (h)", (r) => fmt(r.processing_delay_h)], ["prospective", (r) => (r.prospective ? <Badge tone="green">yes</Badge> : "no")]]} />
      </Section>

      <Section title={`5 · P&L and risk (${meta.text})`}>
        {t.curves ? (
          <>
            <Curves ts={t.curves.ts} series={{ paper: t.curves.paper, "backtest (same bars)": t.curves.backtest, [`baseline: ${t.baseline}`]: t.curves.baseline }} />
            <Curves ts={t.curves.ts} series={{ drawdown: t.curves.drawdown, "gross exposure": t.curves.gross }} height={140} />
            <Table rows={[{ k: "paper", ...t.paper }, { k: "backtest", ...t.backtest }, { k: `baseline (${t.baseline})`, ...t.baseline_stats }]}
              cols={[["", (r) => r.k], ["total", (r) => pct(r.total_return)], ["mean / bar", (r) => fmt(r.mean)], ["95% CI (block bootstrap)", (r) => ci(r.mean_ci95)],
                ["vol (ann.)", (r) => pct(r.vol_ann)], ["Sharpe", (r) => fmt(r.sharpe_ann)], ["Sharpe SE", (r) => fmt(r.sharpe_se)], ["max DD", (r) => pct(r.max_drawdown)], ["hit rate", (r) => pct(r.hit_rate)]]} />
            <p className="mt-2 text-xs">Excess over the baseline per bar: {fmt(t.excess_vs_baseline?.mean)} · 95% CI {ci(t.excess_vs_baseline?.mean_ci95)}</p>
            {t.rolling && <Curves ts={t.rolling.ts} series={{ [`rolling Sharpe (${t.rolling.window} bars)`]: t.rolling.sharpe }} height={140} />}
            <p className="mt-1 text-xs text-slate-500">{t.note}</p>
          </>
        ) : <p className="text-xs text-slate-500">No bar processed yet.</p>}
      </Section>

      <Section title="6 · Comparison with the backtest">
        {!cmp ? <p className="text-xs text-slate-500">Nothing to compare yet.</p> : (
          <>
            <Table rows={cmp.conventions} cols={[["convention", (r) => r.check], ["", (r) => (r.passed ? <Badge tone="green">same</Badge> : <Badge tone="red">differs</Badge>)], ["detail", (r) => r.detail]]} />
            {!cmp.comparable ? <p className="text-xs text-rose-600">{cmp.note}</p> : (
              <>
                <KV data={{ "return paper / backtest": `${pct(cmp.returns.paper_total)} / ${pct(cmp.returns.backtest_total)}`, "mean difference per bar (bps), 95% CI": ci(cmp.returns.diff_ci95_bps),
                  correlation: cmp.returns.correlation, "tracking error (ann.)": cmp.returns.tracking_error_ann, "volatility paper / backtest": `${pct(cmp.volatility.paper_ann)} / ${pct(cmp.volatility.backtest_ann)}`,
                  "max drawdown paper / backtest": `${pct(cmp.drawdown.paper)} / ${pct(cmp.drawdown.backtest)}`, "signal changes per 100 bars": `${fmt(cmp.signal_frequency.paper_changes_per_100_bars)} / ${fmt(cmp.signal_frequency.backtest_changes_per_100_bars)}`,
                  "signal agreement": cmp.signal_frequency.agreement, "gross exposure paper / backtest": `${fmt(cmp.exposure.paper_avg_gross)} / ${fmt(cmp.exposure.backtest_avg_gross)}` }} />
                <h3 className="mb-1 mt-2 text-xs font-semibold">Per trade ({cmp.per_trade.units})</h3>
                <Table rows={[{ k: "paper", ...(cmp.per_trade.paper ?? {}) }, { k: "backtest", ...(cmp.per_trade.backtest ?? {}) }]}
                  cols={[["", (r) => r.k], ["n", (r) => fmt(r.n)], ["mean", (r) => fmt(r.mean)], ["p10", (r) => fmt(r.p10)], ["median", (r) => fmt(r.p50)], ["p90", (r) => fmt(r.p90)], ["win rate", (r) => pct(r.win_rate)]]} />
                <h3 className="mb-1 mt-2 text-xs font-semibold">Per asset ({cmp.per_asset_units}) and per volatility regime</h3>
                <Table rows={cmp.per_asset} cols={[["asset", (r) => r.instrument], ["paper", (r) => pct(r.paper_gross)], ["backtest (approx.)", (r) => pct(r.backtest_gross_approx)], ["paper costs", (r) => pct(r.paper_costs)]]} />
                <Table rows={cmp.regimes} cols={[["regime", (r) => r.regime], ["bars", (r) => r.bars], ["paper mean", (r) => fmt(r.paper_mean)], ["backtest mean", (r) => fmt(r.backtest_mean)], ["diff (bps)", (r) => fmt(r.diff_bps)]]} />
                <h3 className="mb-1 mt-2 text-xs font-semibold">Unreconciled differences</h3>
                <Table rows={cmp.unreconciled} empty="None beyond the tolerance."
                  cols={[["item", (r) => r.item], ["size", (r) => fmt(r.unreconciled_bps ?? r.agreement)], ["explained by costs (bps)", (r) => fmt(r.explained_by_costs_bps)], ["cause", (r) => r.cause]]} />
                <p className="mt-1 text-xs text-slate-500">{cmp.note}</p>
              </>
            )}
          </>
        )}
      </Section>

      <Section title="7 · Costs and execution quality">
        {cmp?.costs ? (
          <>
            <KV data={{ "paper costs": cmp.costs.paper_total, "backtest costs (same bars)": cmp.costs.backtest_total, "paper / backtest": cmp.costs.ratio, ...cmp.costs.paper_breakdown,
              "cost per fill, median (bps)": cmp.costs.paper_cost_bps_per_fill?.p50, "cost per fill, p90 (bps)": cmp.costs.paper_cost_bps_per_fill?.p90 }} />
            <KV data={{ orders: cmp.fills.orders, fills: cmp.fills.fills, "fill rate": cmp.fills.fill_rate, partial: cmp.fills.partial, rejected: cmp.fills.rejected, expired: cmp.fills.expired,
              "shortfall vs decision close (bps)": cmp.fills.avg_shortfall_vs_decision_close_bps }} />
            <p className="mt-1 text-xs text-slate-500">{cmp.costs.note}</p>
          </>
        ) : <p className="text-xs text-slate-500">No fills yet.</p>}
      </Section>

      <Section title="8 · Alerts and pauses">
        <Table rows={d.pauses} empty="No pause."
          cols={[["#", (r) => r.id], ["cause", (r) => <Badge tone={r.resolved_at ? "slate" : "amber"}>{r.cause}</Badge>], ["message", (r) => r.message], ["evidence", (r) => fmt(r.evidence)],
            ["at", (r) => fmtTs(r.at)], ["bar", (r) => fmtTs(r.sim_ts)], ["resolved", (r) => (r.resolved_at ? `${fmtTs(r.resolved_at)} by ${r.resolved_by}: ${r.resolution}` : "open")]]} />
        {d.errors.length > 0 && <Table rows={d.errors} cols={[["engine error", (r) => r.error], ["at", (r) => fmtTs(r.at)], ["after bar", (r) => fmtTs(r.bar_after)]]} />}
        <h3 className="mb-1 mt-2 text-xs font-semibold">Latest session events</h3>
        <Table rows={d.events.slice(0, 40)} cols={[["#", (r) => r.seq], ["bar", (r) => fmtTs(r.sim_ts)], ["level", (r) => r.level], ["category", (r) => r.category], ["code", (r) => r.code], ["message", (r) => r.message]]} />
      </Section>

      <Section title="9 · Versions and history">
        <Table rows={versions} empty="—"
          cols={[["version", (r) => `v${r.version}`], ["admitted", (r) => `${fmtTs(r.admitted_at)} by ${r.admitted_by}`], ["params", (r) => fmt(r.params)],
            ["record hash", (r) => <code>{String(r.record_hash).slice(0, 12)}</code>],
            ["programs (kept apart)", (r) => r.programs.map((p: Row) => `${p.mode} ${p.status} ${p.rule_state ?? ""}`).join(" · ") || "none"]]} />
        <h3 className="mb-1 mt-2 text-xs font-semibold">State changes and the events behind them</h3>
        <Table rows={[...d.timeline].reverse()}
          cols={[["at", (r) => fmtTs(r.at)], ["bar", (r) => fmtTs(r.sim_ts)], ["change", (r) => r.kind], ["detail", (r) => r.detail], ["up to event #", (r) => r.event_seq], ["evidence", (r) => fmt(r.evidence)]]} />
      </Section>

      <Section title="10 · Final report" right={<a className="btn-ghost" href={api.exportUrl(id)}>Export JSON</a>}>
        <p className="mb-2 text-xs">Rules: <ProgramState s={rule.state} /> {rule.reasons.join("; ")}</p>
        <Table rows={rule.criteria}
          cols={[["criterion", (r) => r.criterion], ["kind", (r) => r.kind], ["met", (r) => (!r.evaluable ? "not evaluable" : r.met ? "yes" : "no")], ["detail", (r) => r.detail]]} />
        <p className="mt-2 text-xs text-slate-500">{rule.note}</p>
        <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-3">
          <Field label="Classification (as conservative as the rules, or more)">
            <select className="input" value={op.decision} onChange={(e) => setOp({ ...op, decision: e.target.value })}>
              <option value="">choose…</option>
              {allowed.map((a) => <option key={a} value={a}>{a}</option>)}
            </select>
          </Field>
          <Field label="Rationale"><input className="input" value={op.rationale} onChange={(e) => setOp({ ...op, rationale: e.target.value })} /></Field>
          <div className="flex items-end"><button className="btn" onClick={() => act(() => api.review(id, { by: op.by, decision: op.decision, rationale: op.rationale }))}>Record review</button></div>
        </div>
        <Table rows={d.reviews} empty="No review yet."
          cols={[["at", (r) => fmtTs(r.at)], ["by", (r) => r.by], ["decision", (r) => <ProgramState s={r.decision} />], ["rules then", (r) => r.rule_state], ["rationale", (r) => r.rationale]]} />
        <button className="btn-ghost mt-2" onClick={() => api.report(id).then(setRep).catch((e) => setErr(String(e)))}>Show limitations</button>
        {rep && <ul className="mt-1 list-disc pl-5 text-xs">{rep.limitations.map((x: string) => <li key={x}>{x}</li>)}</ul>}
      </Section>
    </div>
  );
}

export default function PaperValidationPage() {
  const [programs, setPrograms] = useState<Row[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(() => {
    api.programs().then(setPrograms).catch((e) => setErr(String(e)));
  }, []);
  useEffect(load, [load]);
  return (
    <div className="space-y-3">
      <div>
        <h1 className="text-lg font-semibold">Paper Trading Validation</h1>
        <p className="text-xs text-slate-500">
          Simulation only: no order reaches any broker, exchange or account. Each program runs one candidate version under a protocol fixed before it starts.
          Historical replay, prospective paper and synthetic results are labelled and kept apart. A classification is not a forecast and authorises nothing.
        </p>
      </div>
      <ErrorBox error={err} />
      <Registry onChange={load} />
      <Protocols onStarted={(id) => { load(); setOpen(id); }} />
      <Section title="Programs">
        <Table rows={programs} empty="No program yet."
          cols={[["candidate", (r) => `${r.candidate_name} v${r.candidate_version}`], ["mode", (r) => <ModeBadge mode={r.mode} />], ["status", (r) => r.status],
            ["rules", (r) => <ProgramState s={r.rule_state} />], ["classification", (r) => (r.classification?.state ? <ProgramState s={r.classification.state} /> : "pending review")],
            ["open pauses", (r) => r.open_pauses], ["bars", (r) => fmt(r.bars)], ["", (r) => <button className="btn-ghost" onClick={() => setOpen(r.id)}>Open</button>]]} />
      </Section>
      {open && <ProgramDetail key={open} id={open} onChange={load} />}
    </div>
  );
}

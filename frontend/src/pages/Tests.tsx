import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, research, type DatasetRow, type DetectorInfo, type Hypothesis, type Job, type Schedule } from "../api";
import { Badge, ErrorBox, Section, SyntheticBadge, fmtTs } from "../components/ui";

// Map a stored dataset back to a downloadable ingest request (used by "download before scanning").
function ingestItem(d: DatasetRow): Record<string, unknown> | null {
  const [kind, exchange] = d.provider_id.split(":");
  if (kind === "local_file") return null;
  return {
    adapter: kind,
    symbol: d.symbol,
    asset_class: d.asset_class,
    frequency: d.frequency,
    adapter_config: kind === "ccxt" && exchange ? { exchange } : {},
  };
}

export default function TestsPage() {
  const [datasets, setDatasets] = useState<DatasetRow[]>([]);
  const [detectors, setDetectors] = useState<DetectorInfo[]>([]);
  const [hyps, setHyps] = useState<Hypothesis[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [enabled, setEnabled] = useState<Record<string, boolean>>({});
  const [params, setParams] = useState<Record<string, Record<string, string>>>({});
  const [pairs, setPairs] = useState<[string, string][]>([]);
  const [pairY, setPairY] = useState("");
  const [pairX, setPairX] = useState("");
  const [every, setEvery] = useState("24");
  const [download, setDownload] = useState(true);
  const [hyp, setHyp] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(() => {
    api.jobs().then(setJobs, () => undefined);
    api.schedules().then(setSchedules, () => undefined);
    research.hypotheses().then(setHyps, () => undefined);
  }, []);

  useEffect(() => {
    research.datasets().then(setDatasets, (e) => setError(String(e.message)));
    research.detectors().then((d) => {
      setDetectors(d);
      setEnabled(Object.fromEntries(d.filter((x) => !x.pairwise).map((x) => [x.name, true])));
      setParams(Object.fromEntries(d.map((x) => [x.name, Object.fromEntries(Object.entries(x.defaults).map(([k, v]) => [k, String(v)]))])));
    }, () => undefined);
    refresh();
    const t = window.setInterval(refresh, 4000);
    return () => window.clearInterval(t);
  }, [refresh]);

  const byInstrument = (iid: string) => datasets.find((d) => d.instrument_id === iid);
  const detectorConfig = () => {
    const out: Record<string, Record<string, number | string>> = {};
    for (const d of detectors) {
      if (d.pairwise ? pairs.length === 0 : !enabled[d.name]) continue;
      out[d.name] = Object.fromEntries(Object.entries(params[d.name] ?? {}).map(([k, v]) => [k, isNaN(Number(v)) ? v : Number(v)]));
    }
    return out;
  };

  const run = async (fn: () => Promise<unknown>, msg: string) => {
    setError(null);
    try {
      await fn();
      setNotice(msg);
      refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const runNow = () =>
    run(
      () =>
        api.scan({
          instrument_ids: selected,
          detectors: detectorConfig(),
          pairs: pairs.map(([y, x]) => [byInstrument(y)!.id, byInstrument(x)!.id]),
          run_async: true,
          label: "tests page",
        }),
      "Scan queued. Results appear in the Market Scanner when the job finishes.",
    );

  const schedule = () => {
    const hours = Math.max(Number(every) || 24, 1 / 60);
    const items = selected.map((iid) => byInstrument(iid)).filter(Boolean).map((d) => ingestItem(d!)).filter(Boolean);
    const body =
      download && items.length
        ? { kind: "ingest_and_scan", payload: { items, detectors: detectorConfig(), instrument_pairs: pairs, label: "scheduled" } }
        : { kind: "scan", payload: { instrument_ids: selected, detectors: detectorConfig(), instrument_pairs: pairs, label: "scheduled" } };
    const names = selected.map((i) => byInstrument(i)?.symbol).join(", ");
    return run(
      () => api.createSchedule({ name: `Scan ${names} every ${every}h`, interval_seconds: Math.round(hours * 3600), ...body }),
      "Schedule created.",
    );
  };

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-lg font-semibold">Tests: choose, run and schedule</h1>
      <ErrorBox error={error} />
      {notice && <div className="text-sm text-indigo-700 dark:text-indigo-300">{notice}</div>}

      <Section title="1. Instruments (latest data snapshot)">
        {datasets.length === 0 && (
          <p className="text-sm text-slate-500">
            No data yet. Download some in <Link className="underline" to="/data">Data Health</Link>.
          </p>
        )}
        <div className="flex flex-wrap gap-3">
          {datasets.map((d) => (
            <label key={d.id} className="flex items-center gap-1 text-sm">
              <input
                type="checkbox"
                aria-label={`select ${d.symbol}`}
                checked={selected.includes(d.instrument_id)}
                onChange={(e) => setSelected((s) => (e.target.checked ? [...s, d.instrument_id] : s.filter((x) => x !== d.instrument_id)))}
              />
              {d.symbol} <span className="text-xs text-slate-500">{d.provider_id} · {d.frequency}</span>
              {d.is_synthetic && <SyntheticBadge />}
            </label>
          ))}
        </div>
      </Section>

      <Section title="2. Anomaly tests">
        <div className="grid gap-3 md:grid-cols-2">
          {detectors.filter((d) => !d.pairwise).map((d) => (
            <div key={d.name} className="rounded-md border border-slate-200 p-3 dark:border-slate-800">
              <label className="flex items-center gap-2 text-sm font-medium">
                <input type="checkbox" checked={!!enabled[d.name]} onChange={(e) => setEnabled((x) => ({ ...x, [d.name]: e.target.checked }))} />
                {d.name} <span className="text-xs font-normal text-slate-500">v{d.version}</span>
              </label>
              <p className="mt-1 text-xs text-slate-500">{d.description}</p>
              <div className="mt-2 flex flex-wrap gap-2">
                {Object.keys(d.defaults).map((k) => (
                  <label key={k} className="flex flex-col text-xs text-slate-500">
                    {k}
                    <input className="input w-24" value={params[d.name]?.[k] ?? ""} onChange={(e) => setParams((p) => ({ ...p, [d.name]: { ...p[d.name], [k]: e.target.value } }))} />
                  </label>
                ))}
              </div>
            </div>
          ))}
        </div>
        <div className="mt-3 flex flex-wrap items-end gap-2 text-sm">
          <span className="text-xs text-slate-500">Pair divergence (Y vs X):</span>
          <select className="input" aria-label="pair Y" value={pairY} onChange={(e) => setPairY(e.target.value)}>
            <option value="">Y…</option>
            {datasets.map((d) => <option key={d.id} value={d.instrument_id}>{d.symbol}</option>)}
          </select>
          <select className="input" aria-label="pair X" value={pairX} onChange={(e) => setPairX(e.target.value)}>
            <option value="">X…</option>
            {datasets.map((d) => <option key={d.id} value={d.instrument_id}>{d.symbol}</option>)}
          </select>
          <button className="btn-ghost" disabled={!pairY || !pairX || pairY === pairX} onClick={() => setPairs((p) => [...p, [pairY, pairX]])}>Add pair</button>
          {pairs.map(([y, x], i) => (
            <Badge key={i}>
              {byInstrument(y)?.symbol} / {byInstrument(x)?.symbol}{" "}
              <button aria-label="remove pair" onClick={() => setPairs((p) => p.filter((_, j) => j !== i))}>×</button>
            </Badge>
          ))}
        </div>
        <div className="mt-4 flex flex-wrap items-end gap-3">
          <button className="btn" disabled={!selected.length && !pairs.length} onClick={runNow}>Run now</button>
          <label className="flex flex-col text-xs text-slate-500">
            Every (hours)
            <input className="input w-20" type="number" min="0.02" step="any" value={every} onChange={(e) => setEvery(e.target.value)} />
          </label>
          <label className="flex items-center gap-1 text-xs text-slate-500">
            <input type="checkbox" checked={download} onChange={(e) => setDownload(e.target.checked)} /> download new bars first
          </label>
          <button className="btn-ghost" disabled={!selected.length} onClick={schedule}>Schedule</button>
        </div>
      </Section>

      <Section title="3. Hypothesis validation (walk-forward, robustness, hard gates)">
        <div className="flex flex-wrap items-end gap-3">
          <select className="input" aria-label="hypothesis" value={hyp} onChange={(e) => setHyp(e.target.value)}>
            <option value="">Choose a hypothesis…</option>
            {hyps.map((h) => <option key={h.id} value={h.id}>{h.title} (v{h.version}, {h.status})</option>)}
          </select>
          <button className="btn" disabled={!hyp} onClick={() => run(() => research.validate(hyp, true), "Validation queued.")}>Run validation</button>
          <Link className="text-sm text-indigo-700 underline dark:text-indigo-300" to="/research">Create a hypothesis</Link>
        </div>
        <p className="mt-2 text-xs text-slate-500">Each run is a registered trial and lowers the Deflated Sharpe of the whole hypothesis family.</p>
      </Section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Jobs">
          <table className="min-w-full">
            <tbody>
              {jobs.map((j) => (
                <tr key={j.id}>
                  <td className="td">{j.kind}</td>
                  <td className="td"><Badge tone={j.status === "succeeded" ? "green" : j.status === "failed" ? "red" : "slate"}>{j.status}</Badge></td>
                  <td className="td text-xs">{fmtTs(j.created_at)}</td>
                  <td className="td text-xs">
                    {typeof j.result?.experiment_id === "string" && (j.kind === "validate" ? <Link className="underline" to={`/validations/${j.result.experiment_id}`}>report</Link> : <Link className="underline" to="/">scanner</Link>)}
                    {j.status === "queued" && <button className="btn-ghost" onClick={() => run(() => fetch(`/api/v1/jobs/${j.id}/cancel`, { method: "POST" }), "Job cancelled.")}>Cancel</button>}
                  </td>
                  <td className="td max-w-xs truncate text-xs text-rose-700 dark:text-rose-300" title={j.error ?? ""}>{j.error ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
        <Section title="Schedules">
          {schedules.length === 0 && <p className="text-sm text-slate-500">No schedules.</p>}
          <ul className="flex flex-col gap-2 text-sm">
            {schedules.map((s) => (
              <li key={s.id} className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{s.name}</span>
                <Badge>{s.kind}</Badge>
                <span className="text-xs text-slate-500">next {fmtTs(s.next_run_at)}</span>
                <button className="btn-ghost ml-auto" onClick={() => run(() => api.patchSchedule(s.id, { enabled: !s.enabled }), s.enabled ? "Paused." : "Resumed.")}>
                  {s.enabled ? "Pause" : "Resume"}
                </button>
                <button className="btn-ghost" onClick={() => run(() => api.deleteSchedule(s.id), "Deleted.")}>Delete</button>
              </li>
            ))}
          </ul>
        </Section>
      </div>
    </div>
  );
}

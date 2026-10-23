import { useCallback, useEffect, useState } from "react";
import { api, type CatalogEntry, type DataHealth, type Job, type Schedule } from "../api";
import { Badge, ErrorBox, QualityBadge, Section, SyntheticBadge, fmt, fmtTs } from "../components/ui";

const ASSET_CLASSES = ["equity", "etf", "future", "fx", "crypto", "index", "other"];

interface DownloadForm {
  adapter: string;
  symbols: string;
  asset_class: string;
  frequency: string;
  start: string;
  exchange: string;
  every_hours: string;
}

const DEFAULT_FORM: DownloadForm = {
  adapter: "yahoo",
  symbols: "SPY, QQQ",
  asset_class: "etf",
  frequency: "1d",
  start: "2015-01-01",
  exchange: "binance",
  every_hours: "24",
};

function items(f: DownloadForm) {
  return f.symbols
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .map((symbol) => ({
      adapter: f.adapter,
      symbol,
      asset_class: f.asset_class,
      frequency: f.frequency,
      start: f.start ? `${f.start}T00:00:00Z` : undefined,
      adapter_config: f.adapter === "ccxt" ? { exchange: f.exchange } : {},
    }));
}

export default function DataHealthPage() {
  const [health, setHealth] = useState<DataHealth | null>(null);
  const [catalog, setCatalog] = useState<CatalogEntry[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [form, setForm] = useState<DownloadForm>(DEFAULT_FORM);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(() => {
    api.health().then(setHealth, (e) => setError(String(e.message ?? e)));
    api.jobs().then(setJobs, () => undefined);
    api.schedules().then(setSchedules, () => undefined);
  }, []);

  useEffect(() => {
    refresh();
    api.catalog().then(setCatalog, () => undefined);
    const t = window.setInterval(refresh, 5000);
    return () => window.clearInterval(t);
  }, [refresh]);

  const set = (k: keyof DownloadForm) => (e: { target: { value: string } }) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const selected = catalog.find((c) => c.kind === form.adapter);

  const download = async () => {
    setError(null);
    try {
      const r = await api.ingest(items(form), true);
      setNotice(`Download job ${r.job?.id.slice(0, 8)} queued.`);
      refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const schedule = async () => {
    setError(null);
    try {
      const hours = Math.max(Number(form.every_hours) || 24, 1 / 60);
      await api.createSchedule({
        name: `${form.adapter}: ${form.symbols} every ${form.every_hours}h`,
        kind: "ingest_and_scan",
        payload: { items: items(form).map((i) => ({ ...i, start: undefined })), label: "scheduled" },
        interval_seconds: Math.round(hours * 3600),
      });
      setNotice("Schedule created: it downloads new bars incrementally and scans them.");
      refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-lg font-semibold">Data Health</h1>
      <ErrorBox error={error} />
      {notice && <div className="text-sm text-indigo-700 dark:text-indigo-300">{notice}</div>}

      <Section title="Download market data">
        <div className="flex flex-wrap items-end gap-3 text-xs text-slate-500">
          <label className="flex flex-col gap-1">
            Provider
            <select className="input" aria-label="Provider" value={form.adapter} onChange={set("adapter")}>
              {catalog.filter((c) => c.kind !== "local_file").map((c) => (
                <option key={c.kind} value={c.kind}>{c.name}</option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            Symbols (comma separated)
            <input className="input w-56" aria-label="Symbols" value={form.symbols} onChange={set("symbols")} />
          </label>
          <label className="flex flex-col gap-1">
            Asset class
            <select className="input" value={form.asset_class} onChange={set("asset_class")}>
              {ASSET_CLASSES.map((a) => <option key={a}>{a}</option>)}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            Bar
            <select className="input" value={form.frequency} onChange={set("frequency")}>
              {(selected?.frequencies ?? ["1d"]).map((f) => <option key={f}>{f}</option>)}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            From
            <input className="input" type="date" value={form.start} onChange={set("start")} />
          </label>
          {form.adapter === "ccxt" && (
            <label className="flex flex-col gap-1">
              Exchange (CCXT id)
              <input className="input w-28" value={form.exchange} onChange={set("exchange")} />
            </label>
          )}
          <button className="btn" onClick={download}>Download now</button>
          <label className="flex flex-col gap-1">
            Every (hours)
            <input className="input w-20" type="number" min="0.02" step="any" value={form.every_hours} onChange={set("every_hours")} />
          </label>
          <button className="btn-ghost" onClick={schedule}>Schedule download + scan</button>
        </div>
        {selected && (
          <p className="mt-3 text-xs text-slate-500">
            <b>{selected.cost}.</b> {selected.license_notes} {selected.history_notes} {selected.delay_notes} {selected.limits_notes}{" "}
            Install: <code>{selected.install}</code>
          </p>
        )}
        <p className="mt-1 text-xs text-slate-500">CSV, Parquet and MetaTrader 4/5 exports can be uploaded through <code>POST /api/v1/data/upload</code>.</p>
      </Section>

      <Section title="Providers">
        <table className="min-w-full">
          <thead>
            <tr>{["Provider", "Datasets", "Last success", "Latency", "Last error"].map((h) => <th key={h} className="th">{h}</th>)}</tr>
          </thead>
          <tbody>
            {health?.providers.map((p) => (
              <tr key={p.id}>
                <td className="td font-medium">{p.id} {p.is_synthetic && <SyntheticBadge />}</td>
                <td className="td">{p.datasets}</td>
                <td className="td">{fmtTs(p.last_success_at)}</td>
                <td className="td">{p.last_latency_ms ? `${Math.round(p.last_latency_ms)} ms` : "—"}</td>
                <td className="td max-w-md truncate text-rose-700 dark:text-rose-300" title={p.last_error ?? ""}>{p.last_error ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {health && !health.providers.length && <p className="text-sm text-slate-500">No providers used yet.</p>}
      </Section>

      <Section title="Datasets (latest snapshot per instrument)">
        <div className="overflow-x-auto">
          <table className="min-w-full">
            <thead>
              <tr>{["Instrument", "Provider", "Bar", "Rows", "Range", "Coverage", "Gaps", "Age (h)", "Quality", "Issues"].map((h) => <th key={h} className="th">{h}</th>)}</tr>
            </thead>
            <tbody>
              {health?.datasets.map((d) => (
                <tr key={d.dataset_id}>
                  <td className="td font-medium">{d.symbol} {d.is_synthetic && <SyntheticBadge />}</td>
                  <td className="td">{d.provider_id}</td>
                  <td className="td">{d.frequency}</td>
                  <td className="td tabular-nums">{d.rows}</td>
                  <td className="td">{fmtTs(d.start_ts).slice(0, 10)} → {fmtTs(d.end_ts).slice(0, 10)}</td>
                  <td className="td tabular-nums">{d.coverage === null ? "—" : `${(d.coverage * 100).toFixed(1)}%`}</td>
                  <td className="td tabular-nums" title={d.largest_gaps.map((g) => `${g.start}..${g.end} (${g.bars})`).join("\n")}>{fmt(d.n_gaps)}</td>
                  <td className="td tabular-nums">{fmt(d.age_hours)} {d.stale && <Badge tone="amber">stale</Badge>}</td>
                  <td className="td"><QualityBadge status={d.quality_status} /></td>
                  <td className="td text-xs" title={d.issues.map((i) => `${i.severity}: ${i.message} (${i.count})`).join("\n")}>
                    {d.issues.map((i) => i.check).join(", ")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Schedules">
          {schedules.length === 0 && <p className="text-sm text-slate-500">No schedules.</p>}
          <ul className="flex flex-col gap-2 text-sm">
            {schedules.map((s) => (
              <li key={s.id} className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{s.name}</span>
                <span className="text-xs text-slate-500">next {fmtTs(s.next_run_at)}</span>
                <button className="btn-ghost ml-auto" onClick={() => api.patchSchedule(s.id, { enabled: !s.enabled }).then(refresh)}>
                  {s.enabled ? "Pause" : "Resume"}
                </button>
                <button className="btn-ghost" onClick={() => api.deleteSchedule(s.id).then(refresh)}>Delete</button>
              </li>
            ))}
          </ul>
        </Section>
        <Section title="Recent jobs">
          <table className="min-w-full">
            <tbody>
              {jobs.map((j) => (
                <tr key={j.id}>
                  <td className="td">{j.kind}</td>
                  <td className="td"><Badge tone={j.status === "succeeded" ? "green" : j.status === "failed" ? "red" : "slate"}>{j.status}</Badge></td>
                  <td className="td text-xs">{fmtTs(j.created_at)}</td>
                  <td className="td max-w-xs truncate text-xs text-rose-700 dark:text-rose-300" title={j.error ?? ""}>{j.error ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      </div>
    </div>
  );
}

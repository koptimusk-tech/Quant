import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type AnomalyPage, type Facets, type SavedQuery } from "../api";
import { DEFINITIONS, PriorityBadge, ScoreExplanations, SignificanceBadge, StageBadge } from "../components/scores";
import { Badge, ErrorBox, ScoreBar, SyntheticBadge, fmt, fmtTs } from "../components/ui";

export interface Filters {
  search: string;
  asset_class: string[];
  anomaly_type: string[];
  frequency: string[];
  detector: string[];
  status: string[];
  market: string;
  min_stat_score: string;
  synthetic: "" | "true" | "false";
  latest_scan_only: boolean;
  sort: string;
  order: "asc" | "desc";
}

export const EMPTY_FILTERS: Filters = {
  search: "",
  asset_class: [],
  anomaly_type: [],
  frequency: [],
  detector: [],
  status: [],
  market: "",
  min_stat_score: "",
  synthetic: "",
  latest_scan_only: false,
  sort: "event_ts",
  order: "desc",
};

const PAGE = 50;

function MultiSelect({ label, options, value, onChange }: { label: string; options: string[]; value: string[]; onChange: (v: string[]) => void }) {
  return (
    <label className="flex flex-col gap-1 text-xs text-slate-500">
      {label}
      <select
        multiple
        aria-label={label}
        className="input h-20 min-w-32"
        value={value}
        onChange={(e) => onChange(Array.from(e.target.selectedOptions).map((o) => o.value))}
      >
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </label>
  );
}

export default function Scanner() {
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [page, setPage] = useState(0);
  const [data, setData] = useState<AnomalyPage | null>(null);
  const [facets, setFacets] = useState<Facets | null>(null);
  const [saved, setSaved] = useState<SavedQuery[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const navigate = useNavigate();

  const params = useMemo(
    () => ({
      ...filters,
      synthetic: filters.synthetic || undefined,
      min_stat_score: filters.min_stat_score || undefined,
      latest_scan_only: filters.latest_scan_only || undefined,
      limit: PAGE,
      offset: page * PAGE,
    }),
    [filters, page],
  );

  const load = useCallback(() => {
    api.anomalies(params).then(setData, (e) => setError(String(e.message ?? e)));
  }, [params]);

  useEffect(load, [load]);
  useEffect(() => {
    api.facets().then(setFacets, () => undefined);
    api.savedQueries().then(setSaved, () => undefined);
  }, []);

  const update = (patch: Partial<Filters>) => {
    setPage(0);
    setFilters((f) => ({ ...f, ...patch }));
  };

  const saveQuery = async () => {
    const name = window.prompt("Name for this query");
    if (!name) return;
    await api.saveQuery(name, filters as unknown as Record<string, unknown>);
    setSaved(await api.savedQueries());
  };

  const runScan = async () => {
    setBusy(true);
    setNotice(null);
    try {
      const ds = await api.datasets();
      if (!ds.length) {
        setNotice("No datasets yet. Download data in Data Health first.");
        return;
      }
      await api.scan({ dataset_ids: ds.map((d) => d.id), run_async: true });
      setNotice("Scan queued. Results appear here when the job finishes (see Data Health → Jobs).");
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  };

  const sortBy = (col: string) => update({ sort: col, order: filters.sort === col && filters.order === "desc" ? "asc" : "desc" });
  const arrow = (col: string) => (filters.sort === col ? (filters.order === "desc" ? " ↓" : " ↑") : "");

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-semibold">Market Scanner</h1>
        <span className="text-xs text-slate-500">Anomalies are observations to investigate, not trading signals.</span>
        <div className="ml-auto flex gap-2">
          <button className="btn" onClick={runScan} disabled={busy}>
            Scan latest datasets
          </button>
        </div>
      </div>
      <ErrorBox error={error} />
      {notice && <div className="text-sm text-indigo-700 dark:text-indigo-300">{notice}</div>}

      <div className="card flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-xs text-slate-500">
          Search
          <input
            className="input w-48"
            placeholder="symbol, type, detector"
            value={filters.search}
            onChange={(e) => update({ search: e.target.value })}
          />
        </label>
        <MultiSelect label="Asset class" options={facets?.asset_class ?? []} value={filters.asset_class} onChange={(v) => update({ asset_class: v })} />
        <MultiSelect label="Type" options={facets?.anomaly_type ?? []} value={filters.anomaly_type} onChange={(v) => update({ anomaly_type: v })} />
        <MultiSelect label="Horizon (bar)" options={facets?.frequency ?? []} value={filters.frequency} onChange={(v) => update({ frequency: v })} />
        <MultiSelect label="Detector" options={facets?.detector ?? []} value={filters.detector} onChange={(v) => update({ detector: v })} />
        <MultiSelect label="Validation status" options={facets?.status ?? []} value={filters.status} onChange={(v) => update({ status: v })} />
        <div className="flex flex-col gap-2">
          <label className="flex flex-col gap-1 text-xs text-slate-500">
            Market
            <select className="input" value={filters.market} onChange={(e) => update({ market: e.target.value })}>
              <option value="">all</option>
              {(facets?.market ?? []).map((m) => (
                <option key={m}>{m}</option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs text-slate-500">
            Data
            <select className="input" aria-label="Data origin" value={filters.synthetic} onChange={(e) => update({ synthetic: e.target.value as Filters["synthetic"] })}>
              <option value="">real + synthetic</option>
              <option value="false">real only</option>
              <option value="true">synthetic only</option>
            </select>
          </label>
        </div>
        <div className="flex flex-col gap-2">
          <label className="flex flex-col gap-1 text-xs text-slate-500">
            Min statistical score
            <input className="input w-28" type="number" step="0.05" min="0" max="1" value={filters.min_stat_score} onChange={(e) => update({ min_stat_score: e.target.value })} />
          </label>
          <label className="flex items-center gap-1 text-xs text-slate-500">
            <input type="checkbox" checked={filters.latest_scan_only} onChange={(e) => update({ latest_scan_only: e.target.checked })} />
            latest scan only
          </label>
        </div>
        <div className="flex flex-col gap-2">
          <select
            className="input"
            aria-label="Saved queries"
            value=""
            onChange={(e) => {
              const q = saved.find((s) => s.id === e.target.value);
              if (q) update({ ...EMPTY_FILTERS, ...(q.filters as Partial<Filters>) });
            }}
          >
            <option value="">Saved queries…</option>
            {saved.map((q) => (
              <option key={q.id} value={q.id}>
                {q.name}
              </option>
            ))}
          </select>
          <div className="flex gap-2">
            <button className="btn-ghost" onClick={saveQuery}>
              Save query
            </button>
            <button className="btn-ghost" onClick={() => update(EMPTY_FILTERS)}>
              Reset
            </button>
          </div>
        </div>
      </div>

      <div className="card overflow-x-auto p-0">
        <table className="min-w-full divide-y divide-slate-200 dark:divide-slate-800">
          <thead>
            <tr>
              <th className="th"><span className="sr-only">Explain</span></th>
              <th className="th cursor-pointer" onClick={() => sortBy("event_ts")}>Event (UTC){arrow("event_ts")}</th>
              <th className="th">Instrument</th>
              <th className="th">Class / market</th>
              <th className="th">Type</th>
              <th className="th">Bar</th>
              <th className="th">Dir</th>
              <th className="th">Method</th>
              <th className="th" title="Observed value of the tested quantity">Observed</th>
              <th className="th" title="Statistical reference from the trailing baseline (current bar excluded)">Reference</th>
              <th className="th" title={`Size of the deviation from the reference. ${DEFINITIONS.statistic}`}>Deviation</th>
              <th className="th cursor-pointer" title={DEFINITIONS.stat_score} onClick={() => sortBy("statistical_score")}>Stat. score{arrow("statistical_score")}</th>
              <th className="th cursor-pointer" title={DEFINITIONS.econ_score} onClick={() => sortBy("economic_score")}>Econ. score{arrow("economic_score")}</th>
              <th className="th cursor-pointer" onClick={() => sortBy("adjusted_pvalue")} title={DEFINITIONS.adj_p}>Adj. p{arrow("adjusted_pvalue")}</th>
              <th className="th" title={DEFINITIONS.status}>Status</th>
              <th className="th cursor-pointer" title={DEFINITIONS.priority} onClick={() => sortBy("investigation_priority")}>Investigation priority{arrow("investigation_priority")}</th>
              <th className="th" title={DEFINITIONS.stage}>Stage</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
            {data?.items.map((a) => (
              <Fragment key={a.id}>
              <tr className="cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-800/50" onClick={() => navigate(`/anomalies/${a.id}`)}>
                <td className="td">
                  <button
                    className="btn-ghost px-1"
                    aria-expanded={open === a.id}
                    aria-label={`Explain scores for ${a.symbol} ${fmtTs(a.event_ts)}`}
                    onClick={(e) => {
                      e.stopPropagation();
                      setOpen((o) => (o === a.id ? null : a.id));
                    }}
                  >
                    {open === a.id ? "▾" : "▸"}
                  </button>
                </td>
                <td className="td tabular-nums">{fmtTs(a.event_ts)}</td>
                <td className="td">
                  <Link to={`/anomalies/${a.id}`} className="font-medium text-indigo-700 dark:text-indigo-300" onClick={(e) => e.stopPropagation()}>
                    {a.symbol}
                    {a.related_symbol ? ` / ${a.related_symbol}` : ""}
                  </Link>{" "}
                  {a.is_synthetic && <SyntheticBadge />}
                </td>
                <td className="td text-slate-500">
                  {a.asset_class}
                  {a.market ? ` · ${a.market}` : ""}
                </td>
                <td className="td">{a.anomaly_type}</td>
                <td className="td">{a.frequency}</td>
                <td className="td">{a.direction > 0 ? "▲" : a.direction < 0 ? "▼" : "·"}</td>
                <td className="td text-slate-500">{a.detector}</td>
                <td className="td tabular-nums" title={a.summary?.observed_label ?? ""}>{fmt(a.summary?.observed)}</td>
                <td className="td tabular-nums" title={a.summary?.reference_label ?? ""}>{fmt(a.summary?.reference)}</td>
                <td className="td tabular-nums">
                  {fmt(a.statistic)} <span className="text-xs text-slate-500">{a.summary?.deviation_unit ?? ""}</span>
                </td>
                <td className="td"><ScoreBar value={a.statistical_score} label="statistical score" /></td>
                <td className="td"><ScoreBar value={a.economic_score} label="economic score" /></td>
                <td className="td tabular-nums whitespace-nowrap">{fmt(a.adjusted_pvalue)} <SignificanceBadge p={a.adjusted_pvalue} /></td>
                <td className="td"><Badge>{a.validation_status}</Badge></td>
                <td className="td whitespace-nowrap"><PriorityBadge priority={a.investigation_priority} /></td>
                <td className="td"><StageBadge stage={a.stage} /></td>
              </tr>
              {open === a.id && (
                <tr>
                  <td className="td bg-slate-50 dark:bg-slate-900/50" colSpan={17}>
                    <ScoreExplanations a={a} compact />
                  </td>
                </tr>
              )}
              </Fragment>
            ))}
          </tbody>
        </table>
        {data && data.items.length === 0 && (
          <p className="p-6 text-center text-sm text-slate-500">
            No anomalies match. Load data in <Link className="underline" to="/data">Data Health</Link> and run a scan.
          </p>
        )}
      </div>
      {data && data.total > PAGE && (
        <div className="flex items-center gap-2 text-sm">
          <button className="btn-ghost" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>Previous</button>
          <span>
            {page * PAGE + 1}–{Math.min((page + 1) * PAGE, data.total)} of {data.total}
          </span>
          <button className="btn-ghost" disabled={(page + 1) * PAGE >= data.total} onClick={() => setPage((p) => p + 1)}>Next</button>
        </div>
      )}
      {data && <p className="text-xs text-slate-500">{data.total} anomalies. Statistical score ranks the size of the statistic and is not a probability. Economic score compares the move with a placeholder cost assumption and is not a backtest. Investigation priority orders research work and is not an execution signal; open ▸ on a row to see how each score was computed.</p>}
    </div>
  );
}

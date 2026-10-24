import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { alpha, api, type AnomalyDetail, type EventStudy, type WorthItem } from "../api";
import { VerdictBadge } from "./AlphaResearch";
import { DEFINITIONS, EventStudyView, PriorityBadge, ScoreExplanations, SignificanceBadge, StageBadge } from "../components/scores";
import { Badge, ErrorBox, KV, QualityBadge, ScoreBar, Section, SyntheticBadge, fmt, fmtTs } from "../components/ui";
import { chartColors } from "../components/theme";

const FROM_DETECTOR: Record<string, (x: WorthItem) => boolean> = {
  return_shock: (x) => x.detector === "return_shock",
  volatility_regime: (x) => x.family === "volatility",
  pair_divergence: (x) => x.strategy === "pair_mean_reversion",
};

function WorthForAnomaly({ datasetId, detector, synthetic }: { datasetId: string; detector: string; synthetic: boolean }) {
  const [items, setItems] = useState<WorthItem[] | null>(null);
  useEffect(() => {
    alpha.worthTesting(synthetic, datasetId).then((w) => setItems(w.items.filter(FROM_DETECTOR[detector] ?? (() => false))), () => setItems([]));
  }, [datasetId, detector, synthetic]);
  return (
    <Section title="Is this anomaly type worth testing?">
      {items === null ? (
        <p className="text-sm text-slate-500">Reviewing…</p>
      ) : items.length ? (
        <ul className="space-y-2 text-sm">
          {items.map((x) => (
            <li key={x.key}>
              <VerdictBadge item={x} /> {x.title}
              <ul className="list-disc pl-5">{x.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-slate-500">No testable idea comes from this anomaly type on this dataset.</p>
      )}
      <Link className="text-xs underline" to="/alpha#worth">All recommendations →</Link>
    </Section>
  );
}

const day = (ts: string) => ts.slice(0, 16).replace("T", " ");

export default function AnomalyDetailPage() {
  const { id } = useParams();
  const [d, setD] = useState<AnomalyDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [study, setStudy] = useState<EventStudy | null>(null);
  const [studyBusy, setStudyBusy] = useState(false);
  const [studyError, setStudyError] = useState<string | null>(null);
  const c = chartColors();

  const load = () => {
    if (id) api.anomaly(id).then((x) => { setD(x); setStudy(x.event_study ?? null); }, (e) => setError(String(e.message ?? e)));
  };
  useEffect(load, [id]);

  const runStudy = async () => {
    if (!d) return;
    setStudyBusy(true);
    setStudyError(null);
    try {
      await api.eventStudy(d.anomaly.dataset_id, d.anomaly.detector, d.anomaly.related_dataset_id);
      load(); // refresh priority and stage with the new evidence
    } catch (e) {
      setStudyError(String((e as Error).message ?? e));
    } finally {
      setStudyBusy(false);
    }
  };

  const priceData = useMemo(() => {
    if (!d) return [];
    const rel = d.related_context_bars;
    // With a related instrument both legs are indexed to 100 at the first bar (one axis, common base).
    const base = d.context_bars[0]?.close ?? 1;
    const relMap = new Map((rel ?? []).map((b) => [b.event_ts, b.close]));
    const relBase = rel?.[0]?.close ?? 1;
    return d.context_bars.map((b) => {
      const r = relMap.get(b.event_ts);
      return {
        t: day(b.event_ts),
        primary: rel ? (b.close / base) * 100 : b.close,
        related: rel && r !== undefined ? (r / relBase) * 100 : undefined,
        volume: b.volume,
        post: b.post_event,
      };
    });
  }, [d]);

  if (error) return <ErrorBox error={error} />;
  if (!d) return <p className="text-sm text-slate-500">Loading…</p>;
  const a = d.anomaly;
  const evT = day(a.event_ts);
  const firstPost = priceData.find((p) => p.post)?.t;
  const lastT = priceData[priceData.length - 1]?.t;
  const thr = Number(d.experiment.detector_params.threshold ?? 0);
  const ref = d.reference_distribution;
  const hist =
    ref.bins && ref.counts
      ? ref.counts.map((n, i) => ({ x: +((ref.bins![i] + ref.bins![i + 1]) / 2).toFixed(3), n }))
      : [];
  const twoSeries = !!d.related_context_bars;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <Link to="/" className="text-sm text-indigo-700 dark:text-indigo-300">
          ← Scanner
        </Link>
        <h1 className="text-lg font-semibold">
          {a.symbol}
          {a.related_symbol ? ` / ${a.related_symbol}` : ""} · {a.anomaly_type}
        </h1>
        {a.is_synthetic && <SyntheticBadge />}
        <Badge>{a.validation_status}</Badge>
        <StageBadge stage={a.stage} />
        <PriorityBadge priority={a.investigation_priority} />
        <SignificanceBadge p={a.adjusted_pvalue} />
        <Link to={`/research?anomaly=${a.id}`} className="btn ml-auto">Investigate → hypothesis</Link>
        {d.reproduced ? (
          <Badge tone="green">✓ reproduced from stored data</Badge>
        ) : (
          <Badge tone="red">✗ not reproduced</Badge>
        )}
      </div>
      <p className="text-sm text-slate-500">
        Event {fmtTs(a.event_ts)} · available {fmtTs(a.available_ts)} · {a.frequency} bars · detector {a.detector}@{a.detector_version}
      </p>

      {a.summary && (
        <Section title="Detection summary">
          <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2 lg:grid-cols-4" aria-label="Detection summary">
            {[
              ["Instrument", `${a.symbol ?? a.instrument_id}${a.related_symbol ? ` vs ${a.related_symbol}` : ""}`],
              ["Timeframe", a.frequency],
              ["Date (UTC)", fmtTs(a.event_ts)],
              [`Observed: ${a.summary.observed_label ?? "value"}`, fmt(a.summary.observed)],
              [
                `Reference: ${a.summary.reference_label ?? ""}`,
                `${fmt(a.summary.reference)}${a.summary.reference_scale != null ? ` ± ${fmt(a.summary.reference_scale)}` : ""}`,
              ],
              ...(a.summary.relative != null ? [[a.summary.relative_label ?? "relative", fmt(a.summary.relative)]] : []),
              ["Deviation", `${fmt(a.summary.deviation)} ${a.summary.deviation_unit}`],
              ["Method", `${a.detector}@${a.detector_version}: ${a.summary.method}`],
            ].map(([k, v]) => (
              <div key={k}>
                <dt className="text-xs text-slate-500">{k}</dt>
                <dd className="tabular-nums">{v}</dd>
              </div>
            ))}
          </dl>
        </Section>
      )}

      <Section title="How to read the scores: rarity, significance, economic relevance, predictive capacity">
        <ScoreExplanations a={a} study={study} />
      </Section>

      <Section
        title="Predictive evaluation (event study of this anomaly type on this dataset)"
        right={
          <button className="btn" onClick={runStudy} disabled={studyBusy}>
            {studyBusy ? "Running…" : study ? "Re-run predictive evaluation" : "Run predictive evaluation"}
          </button>
        }
      >
        <ErrorBox error={studyError} />
        {study ? (
          <EventStudyView s={study} />
        ) : (
          <p className="text-sm text-slate-500">
            Not evaluated. The study re-runs {a.detector} on this dataset, enters at the first open after each event becomes available, and
            measures forward returns against the unconditional return of the same horizon, in and out of sample, under several cost
            scenarios. Pair divergences are measured on the spread (long Y, short β·X) with costs on both legs. Nothing is traded.
          </p>
        )}
      </Section>

      <div className="grid gap-4 lg:grid-cols-3">
        <Section title="Scores">
          <div className="flex flex-col gap-2 text-sm">
            <div className="flex items-center justify-between" title={DEFINITIONS.stat_score}>Statistical (ranking) <ScoreBar value={a.statistical_score} label="statistical" /></div>
            <div className="flex items-center justify-between" title={DEFINITIONS.econ_score}>Economic (preliminary) <ScoreBar value={a.economic_score} label="economic" /></div>
            <KV data={{ statistic: a.statistic, "empirical p": a.empirical_pvalue, "BH-adjusted p": a.adjusted_pvalue, "tests in scan": d.experiment.n_tests }} />
          </div>
        </Section>
        <Section title="Costs and tradability (preliminary)">
          <KV data={a.economic_context} />
        </Section>
        <Section title="Data used">
          <KV
            data={{
              provider: d.dataset.provider_id,
              source: d.dataset.source_ref,
              rows: d.dataset.rows,
              range: `${fmtTs(d.dataset.start_ts)} → ${fmtTs(d.dataset.end_ts)}`,
              adjustment: d.dataset.adjustment,
              "content hash": d.dataset.content_hash.slice(0, 16),
            }}
          />
          <div className="mt-2 flex gap-2 text-sm">
            quality <QualityBadge status={d.dataset.quality_status} />
          </div>
        </Section>
      </div>

      <Section title={twoSeries ? `Event chart: ${a.symbol} vs ${a.related_symbol} (indexed to 100)` : `Event chart: ${a.symbol} close`}>
        <div className="h-72">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={priceData} margin={{ top: 20, right: 16, bottom: 0, left: 0 }}>
              <CartesianGrid stroke={c.grid} vertical={false} />
              <XAxis dataKey="t" tick={{ fontSize: 11, fill: c.axis }} minTickGap={40} />
              <YAxis tick={{ fontSize: 11, fill: c.axis }} domain={["auto", "auto"]} width={56} />
              <Tooltip formatter={(v: number) => fmt(v)} />
              {twoSeries && <Legend formatter={(v: string) => <span style={{ color: c.axis }}>{v}</span>} />}
              {firstPost && lastT && <ReferenceArea x1={firstPost} x2={lastT} fill={c.muted} fillOpacity={0.12} label={{ value: "after event (context only)", fontSize: 11, fill: c.axis, position: "insideTop" }} />}
              <ReferenceLine x={evT} stroke={c.axis} strokeDasharray="4 3" label={{ value: "event", fontSize: 11, fill: c.axis, position: "top" }} />
              <Line type="monotone" dataKey="primary" name={a.symbol ?? "price"} stroke={c.primary} strokeWidth={2} dot={false} isAnimationActive={false} />
              {twoSeries && <Line type="monotone" dataKey="related" name={a.related_symbol ?? "related"} stroke={c.secondary} strokeWidth={2} dot={false} isAnimationActive={false} />}
            </LineChart>
          </ResponsiveContainer>
        </div>
      </Section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title={`Detector statistic around the event (threshold ±${thr})`}>
          <div className="h-56">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={d.statistic_series.map((p) => ({ t: day(p.event_ts), v: p.value }))} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
                <CartesianGrid stroke={c.grid} vertical={false} />
                <XAxis dataKey="t" tick={{ fontSize: 11, fill: c.axis }} minTickGap={40} />
                <YAxis tick={{ fontSize: 11, fill: c.axis }} width={48} />
                <Tooltip formatter={(v: number) => fmt(v)} />
                {thr > 0 && <ReferenceLine y={thr} stroke={c.muted} strokeDasharray="4 3" />}
                {thr > 0 && <ReferenceLine y={-thr} stroke={c.muted} strokeDasharray="4 3" />}
                <ReferenceLine x={evT} stroke={c.axis} strokeDasharray="4 3" />
                <Line type="monotone" dataKey="v" name="statistic" stroke={c.primary} strokeWidth={2} dot={false} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </Section>
        <Section title="Reference distribution (baseline before the event)">
          {hist.length ? (
            <>
              <div className="h-56">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={hist} margin={{ top: 8, right: 16, bottom: 0, left: 0 }} barCategoryGap={2}>
                    <CartesianGrid stroke={c.grid} vertical={false} />
                    <XAxis dataKey="x" type="number" domain={["dataMin", "dataMax"]} tick={{ fontSize: 11, fill: c.axis }} />
                    <YAxis tick={{ fontSize: 11, fill: c.axis }} width={40} allowDecimals={false} />
                    <Tooltip formatter={(v: number) => [v, "bars"]} labelFormatter={(x) => `statistic ≈ ${x}`} />
                    <Bar dataKey="n" fill={c.primary} radius={[4, 4, 0, 0]} isAnimationActive={false} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
              <p className="mt-1 text-xs text-slate-500">
                {ref.label}. Event value {fmt(ref.event_value)}; 1%/99% quantiles {fmt(ref.quantiles?.["0.01"])} / {fmt(ref.quantiles?.["0.99"])}.
              </p>
            </>
          ) : (
            <p className="text-sm text-slate-500">Not available.</p>
          )}
        </Section>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Statistics">
          <KV data={a.metrics} />
        </Section>
        <Section title="Alternative explanations to rule out">
          <ul className="list-disc space-y-1 pl-5 text-sm">
            {a.alternative_explanations.map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
        </Section>
        <Section title="Detector and reproducible experiment">
          <p className="mb-2 text-sm text-slate-500">{d.detector.description}</p>
          <KV
            data={{
              experiment: d.experiment.id,
              "algorithm version": d.experiment.algorithm_version,
              "code version": d.experiment.code_version,
              "config hash": d.experiment.config_hash.slice(0, 16),
              parameters: d.experiment.detector_params,
            }}
          />
        </Section>
        <Section title="Data quality at detection">
          <KV data={a.data_quality} />
        </Section>
        <WorthForAnomaly datasetId={a.dataset_id} detector={a.detector} synthetic={a.is_synthetic} />
        <Section title="Related hypotheses and research stage">
          <p className="mb-2 text-sm"><StageBadge stage={a.stage} /></p>
          {d.related_hypotheses.length ? (
            <ul className="list-disc space-y-1 pl-5 text-sm">
              {d.related_hypotheses.map((h) => (
                <li key={h.id}><Link className="underline" to={`/research/${h.id}`}>{h.title}</Link> · {h.status}</li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-slate-500">None yet. Create one with “Investigate → hypothesis”; a strategy is validated only after its walk-forward and Gates A–D.</p>
          )}
        </Section>
        <Section title="Notes">
          <ul className="list-disc space-y-1 pl-5 text-sm text-slate-600 dark:text-slate-400">
            {d.notes.map((n) => (
              <li key={n}>{n}</li>
            ))}
          </ul>
        </Section>
      </div>

      <details className="card">
        <summary className="cursor-pointer text-sm font-semibold">Bars shown in the chart (table)</summary>
        <div className="mt-2 max-h-80 overflow-auto">
          <table className="min-w-full text-sm">
            <thead>
              <tr>
                {["event_ts", "open", "high", "low", "close", "volume", "after event"].map((h) => (
                  <th key={h} className="th">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {d.context_bars.map((b) => (
                <tr key={b.event_ts} className={b.post_event ? "text-slate-400" : ""}>
                  <td className="td">{fmtTs(b.event_ts)}</td>
                  <td className="td">{fmt(b.open)}</td>
                  <td className="td">{fmt(b.high)}</td>
                  <td className="td">{fmt(b.low)}</td>
                  <td className="td">{fmt(b.close)}</td>
                  <td className="td">{fmt(b.volume)}</td>
                  <td className="td">{b.post_event ? "yes" : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}

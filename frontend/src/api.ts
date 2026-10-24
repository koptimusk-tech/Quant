// Typed client for the Quant Anomaly Lab API (/api/v1). Types mirror backend/qal/api/schemas.py.

export interface Anomaly {
  id: string;
  experiment_id: string;
  detector: string;
  detector_version: string;
  anomaly_type: string;
  instrument_id: string;
  related_instrument_id: string | null;
  dataset_id: string;
  related_dataset_id: string | null;
  frequency: string;
  event_ts: string;
  available_ts: string;
  direction: number;
  statistic: number;
  empirical_pvalue: number | null;
  adjusted_pvalue: number | null;
  statistical_score: number;
  economic_score: number | null;
  metrics: Record<string, unknown>;
  economic_context: Record<string, unknown>;
  data_quality: Record<string, unknown>;
  alternative_explanations: string[];
  is_synthetic: boolean;
  validation_status: string;
  symbol: string | null;
  related_symbol: string | null;
  asset_class: string | null;
  market: string | null;
  provider_id: string | null;
  summary?: AnomalySummary;
  explanations?: Explanations;
  investigation_priority?: Priority | null;
  stage?: Stage | null;
}

export interface ScoreComponent {
  name: string;
  value: number | string | null;
  role?: string;
  source: string;
  weight?: number;
  contribution?: number;
}

/** Four separate concepts: rarity, significance, economic relevance, predictive capacity. */
export interface Explanations {
  rarity: { statistic: number; statistical_score: number; threshold: number | null; formula: string; text: string };
  significance: {
    level: "strong" | "significant" | "weak" | "not_significant" | "unknown";
    label: string;
    adjusted_pvalue: number | null;
    empirical_pvalue: number | null;
    text: string;
    method?: string;
  };
  economic: {
    score: number | null;
    interpretable: boolean;
    formula: string;
    aggregation: string;
    components: ScoreComponent[];
    text: string;
    caveat: string | null;
    missing: string[];
    liquidity_flag?: string;
  };
  predictive: { text: string };
}

export interface Priority {
  value: number;
  tier: "high" | "medium" | "low";
  base: number;
  components: ScoreComponent[];
  multipliers: { name: string; value: number; source: string }[];
  capped: boolean;
  explanation: string;
  label: string;
  formula: string;
  event_study: { experiment_id: string; verdict: string; verdict_text: string; created_at: string } | null;
}

export interface Stage {
  stage: "detection" | "investigation" | "oos_failed" | "oos_passed" | "paper_trading_ready";
  label: string;
  hypotheses: { id: string; title: string; status: string }[];
}

export interface EventStudyStats {
  n: number;
  mean_gross_bps?: number;
  mean_cost_bps?: number;
  mean_net_bps?: number;
  hit_rate?: number;
  max_drawdown_bps?: number;
  worst_trade_bps?: number;
  pvalue?: number | null;
  adjusted_pvalue?: number;
  pvalue_one_sided?: number;
  ci95_net_bps?: [number, number] | null;
}

export interface EventStudy {
  detector: string;
  horizons: number[];
  n_events: number;
  verdict: "insufficient_sample" | "no_evidence" | "in_sample_only" | "oos_supported";
  verdict_text: string;
  entry_rule: string;
  exit_rule: string;
  split_ts?: string | null;
  table: { horizon: number; rule: string; all: EventStudyStats; in_sample: EventStudyStats }[];
  selected?: { horizon: number; rule: string; in_sample: EventStudyStats } | null;
  out_of_sample?: EventStudyStats;
  walk_forward?: { fold: number; n: number; start: string; end: string; mean_net_bps: number }[];
  baseline_mean_bps?: Record<string, number>;
  cost_model?: { fixed_per_side_bps: number; slippage_range_frac: number; note: string };
  multiple_testing?: string;
  events_dropped_by_horizon?: Record<string, number>;
  is_synthetic?: boolean;
}

/** Observed value, statistical reference, deviation and method (uniform across detectors). */
export interface AnomalySummary {
  method: string;
  observed_label: string | null;
  observed: number | null;
  reference_label: string | null;
  reference: number | null;
  reference_scale: number | null;
  deviation: number;
  deviation_unit: string;
  relative: number | null;
  relative_label: string | null;
}

export interface AnomalyPage {
  total: number;
  items: Anomaly[];
}

export interface Bar {
  ts: string;
  event_ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  adj_close: number | null;
  volume: number | null;
  post_event: boolean;
}

export interface AnomalyDetail {
  anomaly: Anomaly;
  experiment: {
    id: string;
    algorithm_version: string;
    code_version: string;
    config_hash: string;
    created_at: string;
    n_tests: number;
    detector_params: Record<string, unknown>;
  };
  dataset: {
    id: string;
    content_hash: string;
    provider_id: string;
    source_ref: string;
    rows: number;
    start_ts: string;
    end_ts: string;
    is_synthetic: boolean;
    quality_status: string;
    adjustment: string;
  };
  detector: { name: string; version: string; description: string };
  reproduced: boolean;
  context_bars: Bar[];
  related_context_bars: Bar[] | null;
  statistic_series: { event_ts: string; value: number | null }[];
  reference_distribution: {
    values_n?: number;
    bins?: number[];
    counts?: number[];
    event_value?: number;
    quantiles?: Record<string, number>;
    label?: string;
  };
  related_hypotheses: { id: string; title: string; status: string }[];
  event_study?: EventStudy | null;
  event_study_id?: string | null;
  notes: string[];
}

export interface Facets {
  asset_class: string[];
  market: string[];
  provider: string[];
  frequency: string[];
  anomaly_type: string[];
  detector: string[];
  status: string[];
  symbol: string[];
}

export interface SavedQuery {
  id: string;
  name: string;
  filters: Record<string, unknown>;
}

export interface DataHealth {
  generated_at: string;
  providers: {
    id: string;
    kind: string;
    description: string;
    is_synthetic: boolean;
    last_success_at: string | null;
    last_error_at: string | null;
    last_error: string | null;
    last_latency_ms: number | null;
    datasets: number;
  }[];
  datasets: {
    dataset_id: string;
    provider_id: string;
    symbol: string;
    asset_class: string;
    frequency: string;
    rows: number;
    start_ts: string;
    end_ts: string;
    is_synthetic: boolean;
    quality_status: string;
    coverage: number | null;
    missing_bars: number | null;
    n_gaps: number | null;
    largest_gaps: { start: string; end: string; bars: number }[];
    age_hours: number | null;
    stale: boolean | null;
    issues: { check: string; severity: string; message: string; count: number }[];
  }[];
}

export interface Job {
  id: string;
  kind: string;
  status: string;
  attempts: number;
  max_attempts: number;
  error: string | null;
  result: Record<string, unknown>;
  schedule_id: string | null;
  created_at: string;
  finished_at: string | null;
}

export interface Schedule {
  id: string;
  name: string;
  kind: string;
  payload: Record<string, unknown>;
  interval_seconds: number;
  enabled: boolean;
  next_run_at: string;
  last_run_at: string | null;
}

export interface CatalogEntry {
  kind: string;
  name: string;
  asset_classes: string[];
  frequencies: string[];
  requires_credentials: boolean;
  cost: string;
  license_notes: string;
  history_notes: string;
  delay_notes: string;
  limits_notes: string;
  install: string;
}

const BASE = "/api/v1";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, {
    ...init,
    headers:
      init?.body && !(init.body instanceof FormData)
        ? { "Content-Type": "application/json", ...(init?.headers ?? {}) }
        : init?.headers,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* not JSON */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

export function toQuery(params: Record<string, unknown>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    if (Array.isArray(v)) v.forEach((x) => q.append(k, String(x)));
    else q.append(k, String(v));
  }
  const s = q.toString();
  return s ? `?${s}` : "";
}

export const api = {
  anomalies: (params: Record<string, unknown>) => request<AnomalyPage>(`/anomalies${toQuery(params)}`),
  facets: () => request<Facets>("/anomalies/facets"),
  anomaly: (id: string) => request<AnomalyDetail>(`/anomalies/${id}`),
  /** Predictive evaluation of one anomaly type on one dataset (stored; identical requests reuse it). */
  eventStudy: (dataset_id: string, detector: string) =>
    request<{ id: string; results: EventStudy }>("/event-studies", { method: "POST", body: JSON.stringify({ dataset_id, detector }) }),
  savedQueries: () => request<SavedQuery[]>("/saved-queries"),
  saveQuery: (name: string, filters: Record<string, unknown>) =>
    request<SavedQuery>("/saved-queries", { method: "POST", body: JSON.stringify({ name, filters }) }),
  deleteQuery: (id: string) => request<void>(`/saved-queries/${id}`, { method: "DELETE" }),
  health: () => request<DataHealth>("/data/health"),
  catalog: () => request<CatalogEntry[]>("/providers/catalog"),
  ingest: (items: Record<string, unknown>[], runAsync = true) =>
    request<{ job?: Job; ingested?: unknown[] }>("/data/ingest", {
      method: "POST",
      body: JSON.stringify({ items, run_async: runAsync }),
    }),
  /** Upload a CSV/Parquet/MT4/MT5 export; it is validated, normalised and stored as a dataset. */
  upload: (form: FormData) =>
    request<{ ingested: { symbol: string; dataset_id: string; quality_status: string; rows: number }[] }>(
      "/data/upload",
      { method: "POST", body: form },
    ),
  scan: (body: Record<string, unknown>) =>
    request<{ job?: Job; experiment?: { id: string } }>("/scans", { method: "POST", body: JSON.stringify(body) }),
  jobs: () => request<Job[]>("/jobs?limit=20"),
  schedules: () => request<Schedule[]>("/schedules"),
  createSchedule: (body: Record<string, unknown>) =>
    request<Schedule>("/schedules", { method: "POST", body: JSON.stringify(body) }),
  patchSchedule: (id: string, body: Record<string, unknown>) =>
    request<Schedule>(`/schedules/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteSchedule: (id: string) => request<void>(`/schedules/${id}`, { method: "DELETE" }),
  datasets: () => request<{ id: string; symbol: string }[]>("/datasets"),
};

// ---- Phase 2: research ---------------------------------------------------------------------

export interface Hypothesis {
  id: string;
  family_id: string;
  version: number;
  parent_id: string | null;
  title: string;
  status: string;
  contract: Record<string, any>;
  contract_hash: string;
  final_test_accesses: number;
  created_at: string;
}

export interface GateDecision {
  id: string;
  experiment_id: string | null;
  gate: string;
  decision: string;
  reasons: string[];
  created_at: string;
}

export interface HypothesisDetail {
  hypothesis: Hypothesis;
  experiments: { id: string; kind: string; status: string; created_at: string; error: string | null; decision: string | null; oos_sharpe_net: number | null; contaminated: boolean | null }[];
  gates: GateDecision[];
  versions: Hypothesis[];
}

export interface StrategyInfo {
  name: string;
  description: string;
  n_assets: number;
  defaults: Record<string, unknown>;
}

export interface Experiment {
  id: string;
  kind: string;
  hypothesis_id: string | null;
  status: string;
  algorithm_version: string;
  code_version: string;
  config: Record<string, any>;
  data_refs: Record<string, any>[];
  results: Record<string, any>;
  error: string | null;
  created_at: string;
}

export interface DatasetRow {
  id: string;
  symbol: string;
  asset_class: string;
  provider_id: string;
  frequency: string;
  instrument_id: string;
  is_synthetic: boolean;
  start_ts: string;
  end_ts: string;
  rows: number;
}

export interface DetectorInfo {
  name: string;
  version: string;
  anomaly_type: string;
  pairwise: boolean;
  description: string;
  defaults: Record<string, unknown>;
}

export const research = {
  strategies: () => request<StrategyInfo[]>("/strategies"),
  detectors: () => request<DetectorInfo[]>("/detectors"),
  datasets: () => request<DatasetRow[]>("/datasets"),
  hypotheses: () => request<Hypothesis[]>("/hypotheses"),
  hypothesis: (id: string) => request<HypothesisDetail>(`/hypotheses/${id}`),
  create: (contract: Record<string, unknown>, parent_id?: string) =>
    request<Hypothesis>("/hypotheses", { method: "POST", body: JSON.stringify({ contract, parent_id }) }),
  validate: (id: string, runAsync = true) =>
    request<{ job?: Job; experiment?: Experiment }>(`/hypotheses/${id}/validate`, { method: "POST", body: JSON.stringify({ run_async: runAsync }) }),
  finalTest: (id: string) => request<{ experiment: Experiment }>(`/hypotheses/${id}/final-test?confirm=true`, { method: "POST" }),
  transition: (id: string, target: string, reason: string) =>
    request<Hypothesis>(`/hypotheses/${id}/transition`, { method: "POST", body: JSON.stringify({ target, reason }) }),
  experiment: (id: string) => request<Experiment>(`/experiments/${id}`),
  experiments: (kind?: string) => request<Experiment[]>(`/experiments${toQuery({ kind, limit: 30 })}`),
};

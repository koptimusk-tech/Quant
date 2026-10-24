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
  mean_excess_bps?: number;
  ci95_excess_bps?: [number, number] | null;
  hit_rate_gross?: number;
  by_cost?: Record<string, { mean_cost_bps: number; mean_net_bps: number; ci95_net_bps: [number, number] | null; worst_trade_bps: number; max_drawdown_bps: number }>;
}

export interface CostScenario {
  name: string;
  commission_bps: number;
  half_spread_bps: number;
  slippage_bps: number;
  slippage_range_frac: number;
  source: string;
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
  table: { horizon: number; rule: string; n_independent?: number; n_out_of_sample?: number; all: EventStudyStats; in_sample: EventStudyStats }[];
  selected?: { horizon: number; rule: string; in_sample: EventStudyStats } | null;
  out_of_sample?: EventStudyStats;
  walk_forward?: { fold: number; n: number; start: string; end: string; mean_net_bps?: number | null; mean_excess_bps?: number | null }[];
  baseline_mean_bps?: Record<string, number | null>;
  position?: string;
  cost_scenarios?: CostScenario[];
  base_cost_scenario?: string;
  survives_costs_oos?: Record<string, boolean>;
  cost_model?: { scenarios?: string[]; base?: string; note: string };
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
  eventStudy: (dataset_id: string, detector: string, related_dataset_id?: string | null) =>
    request<{ id: string; results: EventStudy }>("/event-studies", {
      method: "POST",
      body: JSON.stringify({ dataset_id, detector, related_dataset_id: related_dataset_id ?? null }),
    }),
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

export interface Proposal {
  id: string;
  kind: "momentum" | "event" | "pair" | string;
  title: string;
  why: string;
  priority: number;
  evidence: Record<string, unknown>;
  variants: number;
  is_synthetic: boolean;
  existing_hypotheses: { id: string; title: string; status: string; versions_in_family: number }[];
  contract: Record<string, any>;
}

export const research = {
  proposals: () => request<Proposal[]>("/research/proposals"),
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

// ---- Alpha Research: agent cycles ----------------------------------------------------------

export interface AlphaBudget {
  max_ideas: number;
  max_hypotheses: number;
  max_experiments: number;
  max_variants: number;
  max_seconds: number;
  llm_tokens: number;
  include_synthetic: boolean;
}

export interface AlphaRun {
  id: string;
  status: string;
  created_at: string;
  finished_at: string | null;
  error: string | null;
  budget: AlphaBudget | null;
  n_ideas?: number;
  n_data_gaps?: number;
  n_registered?: number;
  n_duplicates?: number;
  n_rejected_by_review?: number;
  n_deferred?: number;
  n_experiments?: number;
  n_variants_backtested?: number;
  n_validation_passed?: number;
  n_validation_failed?: number;
  relationship_scan_id?: string | null;
  duration_s?: number;
  note?: string;
}

export interface AgentEvidence {
  kind: string;
  ref: string;
  summary: string;
  values: Record<string, unknown>;
}

export interface AgentMessage {
  id: string;
  run_id: string;
  seq: number;
  sender: string;
  receiver: string;
  kind: string;
  subject_key: string;
  hypothesis_id: string | null;
  payload: Record<string, any>;
  evidence: AgentEvidence[];
  created_at: string;
}

export interface EvidenceLabels {
  predictive_evidence: { level: string; text: string };
  validated_net_profitability: { established: boolean; text: string };
}

export interface AlphaHypothesisRow extends EvidenceLabels {
  id: string;
  title: string;
  status: string;
  version: number;
  created_by: string;
  created_at: string;
  origin: string;
  origin_observation: string;
  strategy: string;
  family: string | null;
  n_source_anomalies: number;
  objections: { block: number; major: number; minor: number };
  research_priority: number | null;
}

export interface ValidationView {
  experiment_id: string;
  status: string;
  created_at: string;
  error: string | null;
  decision: string | null;
  variants: number | null;
  windows: number;
  metrics: Record<string, number | null>;
  benchmark_sharpe_net: number | null;
  excess_vs_benchmark_mean_net: number | null;
  sharpe_ci: (number | null)[];
  positive_window_frac: number | null;
  placebo_pvalue: number | null;
  dsr: number | null;
  pbo: number | null;
  gates: { gate: string; passed: boolean; reasons: string[] }[] | null;
  leakage_checks_passed: boolean | null;
}

export interface AlphaHypothesisDetail extends EvidenceLabels {
  hypothesis: { id: string; title: string; status: string; version: number; created_by: string; created_at: string; contract: Record<string, any>; contract_hash: string; final_test_accesses: number };
  idea_key: string;
  messages: AgentMessage[];
  objections: { code: string; severity: string; message: string }[];
  decisions: { key: string; action: string; reason: string; hypothesis_id: string | null; experiment_id: string | null }[];
  validations: ValidationView[];
  gates: { gate: string; decision: string; reasons: string[]; experiment_id: string | null; created_at: string }[];
  eligibility: { eligible: boolean; requirements: { requirement: string; met: boolean; detail: string }[] };
}

export interface FactorModule {
  name: string;
  title: string;
  data: string;
  universe: string;
  formula: string;
  availability: string;
  rebalance: string;
  benchmark: string;
  available: boolean;
  reason: string;
  universes: { symbols: string[]; common_bars: number; asset_class: string; frequency: string }[];
}

export interface WorthItem {
  key: string;
  title: string;
  route: string;
  family: string;
  dataset_ids: string[];
  origin_observation: string;
  detector: string | null;
  strategy: string;
  priority: number;
  score: number | null;
  verdict: "worth_testing" | "already_tested" | "not_worth" | "not_testable";
  verdict_label: string;
  reasons: string[];
  refuted_if: string[];
  objections: { code: string; severity: string; message: string }[];
  existing: { id: string; title: string; status: string }[];
}

export interface WorthTesting {
  counts: Record<string, number>;
  items: WorthItem[];
  data_gaps: { key: string; family: string; reason: string }[];
  notes: string[];
}

export const alpha = {
  worthTesting: (includeSynthetic = false, datasetId?: string) =>
    request<WorthTesting>(`/alpha/worth-testing${toQuery({ include_synthetic: includeSynthetic, dataset_id: datasetId })}`),
  factors: (includeSynthetic = false) => request<FactorModule[]>(`/alpha/factors${toQuery({ include_synthetic: includeSynthetic })}`),
  runs: () => request<AlphaRun[]>("/alpha/runs"),
  run: (id: string) => request<{ run: AlphaRun; messages: AgentMessage[] }>(`/alpha/runs/${id}`),
  startRun: (budget: Partial<AlphaBudget>) =>
    request<{ job: Job }>("/alpha/runs", { method: "POST", body: JSON.stringify({ budget }) }),
  hypotheses: () => request<AlphaHypothesisRow[]>("/alpha/hypotheses"),
  hypothesis: (id: string) => request<AlphaHypothesisDetail>(`/alpha/hypotheses/${id}`),
  experiment: (id: string) => request<{ job: Job }>(`/alpha/hypotheses/${id}/experiments`, { method: "POST" }),
};

// ---- Phase 5: Model Lab --------------------------------------------------------------------

export interface ModelDetector {
  name: string;
  version: string;
  anomaly_type: string;
  description: string;
  defaults: Record<string, unknown>;
  stochastic: boolean;
  multivariate: boolean;
}

export interface FeatureInfo {
  name: string;
  group: string;
  formula: string;
  source: string;
  window: string;
  availability: string;
  missing: string;
  assumptions: string;
  biases: string;
  needs: string[];
}

export interface Overlap {
  jaccard: number | null;
  share_a_in_b: number | null;
  share_b_in_a: number | null;
}

export interface ModelRow {
  model: string;
  detector: string;
  status: "ok" | "not_applicable" | "failed";
  reason?: string;
  error?: string;
  params: Record<string, unknown>;
  runtime_s?: number;
  n_scored?: number;
  n_flagged?: number;
  n_episodes?: number;
  flag_rate?: number | null;
  nominal_rate?: number | null;
  n_bh_significant?: number;
  per_year?: { year: number; bars: number; flags: number; rate: number }[];
  rate_cv_across_years?: number | null;
  per_regime?: {
    high_vol: { bars: number; flags: number; rate: number | null };
    low_vol: { bars: number; flags: number; rate: number | null };
    high_to_low_ratio: number | null;
  };
  stability?: {
    seeds: { jaccard_mean?: number | null; runs?: ({ seed: number } & Overlap)[]; note?: string };
    window_variants: ({ variant: string; n_flagged?: number; not_applicable?: string } & Partial<Overlap>)[];
  };
  top_feature_counts?: Record<string, number>;
  features_dropped?: Record<string, string>;
  flags?: { ts: string; close: number; p: number; stat: number }[];
  predictive?: {
    verdict?: string;
    verdict_text?: string;
    error?: string;
    n_events?: number;
    selected?: { horizon: number; rule: string } | null;
    out_of_sample?: { n?: number; mean_excess_bps?: number; ci95_excess_bps?: number[]; pvalue_one_sided?: number; mean_net_bps?: number };
    oos_run_wide_adjusted_pvalue?: number;
  };
}

export interface ModelDataset {
  dataset_id: string;
  symbol: string;
  frequency: string;
  quality_status: string;
  is_synthetic: boolean;
  status: "ok" | "skipped";
  reason?: string;
  n_bars?: number;
  truncated_to_last_bars?: number;
  rows?: ModelRow[];
  overlap?: { models: string[]; matrix: (Overlap | null)[][] };
  chart?: { ts: string[]; close: number[]; step: number };
}

export interface BenchmarkRow {
  model: string;
  detector: string;
  recall_scored: number | null;
  nominal_rate: number | null;
  control_false_alarms: { scored?: number; flags?: number; rate?: number; episodes?: number; not_applicable?: string };
  events: { symbol: string; kind: string; bar: number; description: string; scored: boolean; detected: boolean; delay_bars: number | null }[];
}

export interface ModelComparisonSummary {
  id: string;
  status: string;
  created_at: string;
  finished_at: string | null;
  label: string;
  models: string[];
  datasets: string[];
  is_synthetic: boolean | null;
  benchmark: boolean;
  duration_s: number | null;
  budget_exhausted: boolean;
  error: string | null;
}

export interface ModelComparison extends ModelComparisonSummary {
  config: Record<string, any>;
  results: {
    datasets: ModelDataset[];
    limitations: string[];
    synthetic_benchmark?: { rows?: BenchmarkRow[]; note?: string; skipped?: string; n_bars?: number };
    budget_exhausted?: boolean;
    duration_s?: number;
  };
}

export interface ComparisonRequest {
  dataset_ids: string[];
  models: { detector: string; params?: Record<string, unknown>; label?: string }[];
  seeds?: number[];
  horizons?: number[];
  predictive?: boolean;
  benchmark?: boolean;
  max_seconds?: number;
  label?: string;
}

export const models = {
  detectors: () => request<ModelDetector[]>("/models/detectors"),
  features: () => request<{ default: string[]; features: FeatureInfo[] }>("/models/features"),
  experiments: () => request<ModelComparisonSummary[]>("/models/experiments"),
  experiment: (id: string) => request<ModelComparison>(`/models/experiments/${id}`),
  run: (body: ComparisonRequest) =>
    request<{ job: Job }>("/models/experiments", { method: "POST", body: JSON.stringify(body) }),
  jobs: () => request<Job[]>("/jobs?limit=50"),
  cancel: (jobId: string) => request<Job>(`/jobs/${jobId}/cancel`, { method: "POST" }),
  csvUrl: (id: string) => `${BASE}/models/experiments/${id}/export.csv`,
  jsonUrl: (id: string) => `${BASE}/models/experiments/${id}`,
};

// -- Portfolio & Risk (Phase 6) ---------------------------------------------------------------------
export interface PortfolioStrategy {
  id: string;
  version: number;
  name: string;
  source: "hypothesis" | "synthetic";
  universe: string[];
  frequency: string;
  group: string;
  counterparty: string;
  is_synthetic: boolean;
  eligible: boolean;
  reasons: string[];
  approval_status: string;
  limitations: string[];
  validation: { status: string; oos_metrics: Record<string, number | null>; experiment_id: string | null };
  history: { start?: string | null; end?: string | null; oos_bars?: number | null };
}

export interface RiskPolicyOut {
  id: string;
  name: string;
  version: number;
  label: string;
  limits: { strategy: Record<string, number>; group: Record<string, number>; portfolio: Record<string, any> };
  created_by: string;
  active: boolean;
  note: string;
  created_at: string;
}

export interface PortfolioRunSummary {
  id: string;
  status: string;
  created_at: string;
  label: string;
  strategies: string[];
  methods: string[];
  policy_version: string | null;
  is_synthetic: boolean | null;
  sharpe_net: number | null;
  max_drawdown: number | null;
  duration_s: number | null;
  error: string | null;
}

export interface Scenario {
  name: string;
  kind: "historical" | "hypothetical" | "synthetic";
  pnl: number;
  description?: string;
  basis?: string;
  start?: string;
  end?: string;
  max_drawdown?: number | null;
}

export interface MonteCarlo {
  kind: string;
  name?: string;
  note?: string;
  paths?: number;
  horizon_days?: number;
  daily?: Record<string, number>;
  horizon?: Record<string, number>;
  max_drawdown?: Record<string, number>;
}

export interface MethodResult {
  description: string;
  metrics: Record<string, number | null>;
  stability: { rebalances: number; mean_abs_change?: number };
  segments: { first: Record<string, any>; last: Record<string, any>; note: string };
  blocked: Record<string, number>;
  curve: { ts: string; equity: number; drawdown: number }[];
}

export interface Correlations {
  strategies: string[];
  observations: number;
  static?: number[][];
  static_mean_pairwise?: number | null;
  rolling?: { min: number | null; max: number | null };
  regime?: { high_vol: { mean_pairwise: number | null }; low_vol: { mean_pairwise: number | null }; rule: string };
  stress?: { matrix: number[][] | null; mean_pairwise: number | null; days: number; rule?: string; note: string };
}

export interface PortfolioRun extends PortfolioRunSummary {
  config: Record<string, any>;
  results: {
    is_synthetic: boolean;
    synthetic_note: string | null;
    policy_version: string;
    primary_method: string;
    strategies: { id: string; name: string; is_synthetic: boolean }[];
    rejected: { id: string; name?: string; reasons: string[] }[];
    period: { start: string; end: string; days: number };
    methods: Record<string, MethodResult>;
    primary: { metrics: Record<string, number | null>; n_rebalances: number; weights_history: { ts: string; final: Record<string, number>; blocked_rules: string[] }[] };
    benchmarks: { naive_equal_weight: Record<string, number | null>; strategies: Record<string, Record<string, number | null>> };
    cost_sensitivity: { multiplier: number; sharpe_net: number | null; total_return_net: number | null }[];
    risk: {
      as_of: string | null;
      basis: string;
      weights: Record<string, number>;
      ex_ante_vol: number;
      risk_contributions: Record<string, number>;
      marginal_risk: Record<string, number>;
      diversification: Record<string, number | null>;
      exposures: { instrument: Record<string, number>; currency_net: Record<string, number>; market_gross: Record<string, number>; gross: number; net: number };
      liquidity: { instrument: string; exposure: number; capacity_capital: number | null; note: string | null }[];
      marginal_analysis: { strategy: string; corr_with_rest: number | null; vol_with?: number; vol_without?: number; enb_with?: number; enb_without?: number; redundant: boolean; adds_risk_without_diversifying?: boolean; max_pair_corr?: number | null; most_correlated_with?: string | null; note?: string }[];
    };
    historical_risk: { basis: string; correlations: Correlations; drawdown: Record<string, any>; realised_vol: number | null };
    stress: { disclaimer: string; historical: Scenario[]; hypothetical: Scenario[]; monte_carlo: MonteCarlo[] };
    leakage_checks: { check: string; passed: boolean; description: string }[];
    notes: string[];
    decisions_stored?: number;
  };
}

export interface RiskDecisionOut {
  as_of: string;
  level: string;
  scope: string;
  rule: string;
  observed: string | null;
  limit: string;
  decision: "approved" | "blocked";
  action: string;
  reason: string;
  policy_version: string;
}

export interface ProposalRun extends PortfolioRunSummary {
  results: {
    proposals: { key: string; experiment_id: string; status: string; verdict: string; blocked_rules: string[]; strategies: string[]; methods: string[]; rationale: string; metrics: Record<string, number | null> }[];
    candidates: number;
    eligible: number;
    is_synthetic: boolean;
    note: string;
  };
  messages?: { seq: number; sender: string; receiver: string; kind: string; subject: string; payload: Record<string, any> }[];
}

export interface PortfolioRunRequest {
  strategy_ids: string[];
  methods: string[];
  rebalance: string;
  lookback_days: number;
  cov_method: string;
  cost_multiplier: number;
  criteria: { allow_synthetic: boolean };
  label?: string;
}

export const portfolio = {
  strategies: (includeSynthetic: boolean) =>
    request<PortfolioStrategy[]>(`/portfolio/strategies?include_synthetic=${includeSynthetic}`, {
      method: "POST",
      body: JSON.stringify({ allow_synthetic: includeSynthetic }),
    }),
  activePolicy: () => request<RiskPolicyOut>("/portfolio/policies/active"),
  createPolicy: (body: { limits: RiskPolicyOut["limits"]; approved_by: string; confirm: boolean; note?: string }) =>
    request<RiskPolicyOut>("/portfolio/policies", { method: "POST", body: JSON.stringify({ ...body, actor_kind: "human" }) }),
  runs: () => request<PortfolioRunSummary[]>("/portfolio/runs"),
  run: (id: string) => request<PortfolioRun>(`/portfolio/runs/${id}`),
  start: (body: PortfolioRunRequest) => request<{ job: Job }>("/portfolio/runs", { method: "POST", body: JSON.stringify(body) }),
  decisions: (id: string, decision?: string) =>
    request<{ items: RiskDecisionOut[] }>(`/portfolio/runs/${id}/decisions?limit=500${decision ? `&decision=${decision}` : ""}`),
  propose: (includeSynthetic: boolean) =>
    request<{ job: Job }>("/portfolio/proposals", { method: "POST", body: JSON.stringify({ include_synthetic: includeSynthetic }) }),
  proposals: () => request<ProposalRun[]>("/portfolio/proposals"),
  proposal: (id: string) => request<ProposalRun>(`/portfolio/proposals/${id}`),
  jobs: () => request<Job[]>("/jobs?limit=50"),
};

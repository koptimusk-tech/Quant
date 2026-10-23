import type { ReactNode } from "react";

export function Badge({ children, tone = "slate" }: { children: ReactNode; tone?: "slate" | "amber" | "green" | "red" | "indigo" }) {
  const tones = {
    slate: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
    amber: "bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-300",
    green: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/50 dark:text-emerald-300",
    red: "bg-rose-100 text-rose-800 dark:bg-rose-900/50 dark:text-rose-300",
    indigo: "bg-indigo-100 text-indigo-800 dark:bg-indigo-900/50 dark:text-indigo-300",
  };
  return <span className={`inline-block rounded px-1.5 py-0.5 text-xs font-medium ${tones[tone]}`}>{children}</span>;
}

export function SyntheticBadge() {
  return (
    <Badge tone="amber">
      <span title="Generated data with injected test anomalies. Not market data.">SYNTHETIC</span>
    </Badge>
  );
}

export function QualityBadge({ status }: { status: string }) {
  const tone = status === "pass" ? "green" : status === "warn" ? "amber" : status === "fail" ? "red" : "slate";
  return <Badge tone={tone}>{status}</Badge>;
}

export function ScoreBar({ value, label }: { value: number | null; label: string }) {
  if (value === null || value === undefined) return <span className="text-xs text-slate-400">n/a</span>;
  return (
    <div className="flex items-center gap-2" aria-label={`${label} ${value.toFixed(2)}`}>
      <div className="h-1.5 w-16 rounded bg-slate-200 dark:bg-slate-700">
        <div className="h-1.5 rounded bg-indigo-500" style={{ width: `${Math.round(value * 100)}%` }} />
      </div>
      <span className="text-xs tabular-nums">{value.toFixed(2)}</span>
    </div>
  );
}

export function Section({ title, children, right }: { title: string; children: ReactNode; right?: ReactNode }) {
  return (
    <section className="card">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold">{title}</h2>
        {right}
      </div>
      {children}
    </section>
  );
}

export function KV({ data }: { data: Record<string, unknown> }) {
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
      {Object.entries(data).map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-slate-500">{k}</dt>
          <dd className="min-w-0 tabular-nums [overflow-wrap:anywhere]">{fmt(v)}</dd>
        </div>
      ))}
    </dl>
  );
}

export function fmt(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") return Math.abs(v) >= 1e5 || (Math.abs(v) < 1e-3 && v !== 0) ? v.toExponential(3) : String(+v.toFixed(4));
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

export function fmtTs(ts: string | null | undefined): string {
  if (!ts) return "—";
  return ts.replace("T", " ").replace(/\.\d+/, "").replace(/(\+00:00|Z)$/, "");
}

export function ErrorBox({ error }: { error: string | null }) {
  if (!error) return null;
  return <div className="rounded-md border border-rose-300 bg-rose-50 p-2 text-sm text-rose-800 dark:border-rose-800 dark:bg-rose-950 dark:text-rose-300">{error}</div>;
}

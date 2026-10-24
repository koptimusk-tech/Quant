import { useCallback, useEffect, useMemo, useState } from "react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import {
  paper as api,
  type DatasetRow,
  type PaperEquityOut,
  type PaperEvaluation,
  type PaperEventOut,
  type PaperOrderOut,
  type PaperPositionOut,
  type PaperRiskOut,
  type PaperSessionOut,
  type PaperStrategies,
} from "../api";
import { Badge, ErrorBox, Section, fmt, fmtTs } from "../components/ui";
import { chartColors } from "../components/theme";

const pct = (v: number | null | undefined, d = 2) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(d)}%`);
const money = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : v.toLocaleString(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 2 });
const STATUS_TONE: Record<string, "slate" | "amber" | "green" | "red" | "indigo"> = {
  RUNNING: "green", PAUSED: "amber", HALTED: "red", COMPLETED: "slate", CREATED: "indigo",
};
const OPEN = new Set(["PENDING", "PARTIALLY_FILLED"]);

function Labels({ s }: { s: PaperSessionOut }) {
  return (
    <div className="flex flex-wrap gap-1">
      {s.labels.map((l) => (
        <Badge key={l} tone={l.startsWith("UNVALIDATED") || l.startsWith("SYNTHETIC") ? "amber" : "slate"}>{l}</Badge>
      ))}
    </div>
  );
}

function Tile({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="rounded-md border border-slate-200 p-2 dark:border-slate-800">
      <div className="text-xs text-slate-500">{label}</div>
      <div className="text-lg font-semibold tabular-nums">{value}</div>
      {note && <div className="text-xs text-slate-500">{note}</div>}
    </div>
  );
}

// -- create ------------------------------------------------------------------------------------------
function CreateForm({ by, onCreated }: { by: string; onCreated: (s: PaperSessionOut) => void }) {
  const [strats, setStrats] = useState<PaperStrategies | null>(null);
  const [datasets, setDatasets] = useState<DatasetRow[]>([]);
  const [name, setName] = useState("Paper session");
  const [mode, setMode] = useState<"replay" | "new_data">("replay");
  const [capital, setCapital] = useState(100000);
  const [scenario, setScenario] = useState("base");
  const [orderType, setOrderType] = useState("market");
  const [stop, setStop] = useState("");
  const [start, setStart] = useState("");
  const [validated, setValidated] = useState<string[]>([]);
  const [demoType, setDemoType] = useState("");
  const [demoDs, setDemoDs] = useState<string[]>([]);
  const [allowDemo, setAllowDemo] = useState(false);
  const [allocation, setAllocation] = useState(0.3);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.strategies().then(setStrats).catch((e) => setErr(String(e)));
    api.datasets().then(setDatasets).catch(() => setDatasets([]));
  }, []);

  const demo = strats?.demo.find((d) => d.strategy_id === demoType);
  const strategies = [
    ...validated.map((id) => ({ strategy_id: id, allocation })),
    ...(demoType ? [{ strategy_id: demoType, dataset_ids: demoDs, allocation }] : []),
  ];
  const canCreate = by.trim().length >= 2 && strategies.length > 0 && (!demoType || allowDemo);

  const submit = async () => {
    setErr(null);
    try {
      const s = await api.create({
        name, created_by: by, mode, capital, cost_scenario: scenario, order_type: orderType,
        stop_loss_pct: stop ? Number(stop) / 100 : null, start: start ? `${start}T00:00:00Z` : null,
        allow_unvalidated_demo: allowDemo, strategies,
      });
      onCreated(s);
    } catch (e) {
      setErr(String(e));
    }
  };

  return (
    <Section title="New paper session">
      <div className="grid gap-3 md:grid-cols-2">
        <div className="space-y-2 text-sm">
          <label className="block">Name <input className="input w-full" value={name} onChange={(e) => setName(e.target.value)} /></label>
          <div className="flex flex-wrap gap-2">
            <label>Data
              <select aria-label="data mode" className="input ml-1" value={mode} onChange={(e) => setMode(e.target.value as "replay" | "new_data")}>
                <option value="replay">historical replay</option>
                <option value="new_data">new data only</option>
              </select>
            </label>
            <label>Virtual capital <input type="number" className="input ml-1 w-28" value={capital} onChange={(e) => setCapital(Number(e.target.value))} /></label>
            <label>Costs
              <select aria-label="cost scenario" className="input ml-1" value={scenario} onChange={(e) => setScenario(e.target.value)}>
                <option value="base">base</option><option value="adverse">adverse (2x)</option><option value="severe">severe (4x)</option>
              </select>
            </label>
          </div>
          <div className="flex flex-wrap gap-2">
            <label>Orders
              <select aria-label="order type" className="input ml-1" value={orderType} onChange={(e) => setOrderType(e.target.value)}>
                <option value="market">market</option><option value="limit">limit</option>
              </select>
            </label>
            <label>Stop-loss % <input aria-label="stop loss percent" className="input ml-1 w-16" value={stop} placeholder="none" onChange={(e) => setStop(e.target.value)} /></label>
            <label>Allocation <input type="number" step="0.05" className="input ml-1 w-20" value={allocation} onChange={(e) => setAllocation(Number(e.target.value))} /></label>
            {mode === "replay" && (
              <label>Replay from <input type="date" className="input ml-1" value={start} onChange={(e) => setStart(e.target.value)} /></label>
            )}
          </div>
          <p className="text-xs text-slate-500">
            {mode === "replay"
              ? "Historical replay reproduces past bars one at a time; it is not real-time trading."
              : "Only bars ingested after the start are traded; schedule ingestion to move the session forward."}
            {" "}A stop-loss does not guarantee a maximum loss: prices can gap through it.
          </p>
        </div>
        <div className="space-y-2 text-sm">
          <div className="font-medium">Validated strategies</div>
          {!strats?.validated.length && <p className="text-xs text-slate-500">No validated strategies yet.</p>}
          {strats?.validated.map((v) => (
            <label key={v.strategy_id} className="block text-xs">
              <input type="checkbox" aria-label={`select ${v.strategy_id}`} disabled={!v.eligible} checked={validated.includes(v.strategy_id)}
                onChange={() => setValidated((x) => (x.includes(v.strategy_id) ? x.filter((y) => y !== v.strategy_id) : [...x, v.strategy_id]))} />{" "}
              {v.name} <span className="text-slate-500">({v.status})</span>
              {!v.eligible && <span className="ml-1 text-red-600">{v.reasons[0]}</span>}
            </label>
          ))}
          <div className="font-medium">Unvalidated demo rule (simulator exercise only)</div>
          <select aria-label="demo strategy" className="input w-full" value={demoType} onChange={(e) => { setDemoType(e.target.value); setDemoDs([]); }}>
            <option value="">none</option>
            {strats?.demo.map((d) => <option key={d.strategy_id} value={d.strategy_id}>{d.name}</option>)}
          </select>
          {demo && (
            <>
              <p className="text-xs text-slate-500">{demo.description} Needs {demo.n_assets || "3 or more"} dataset(s).</p>
              <select aria-label="demo datasets" multiple className="input h-24 w-full" value={demoDs}
                onChange={(e) => setDemoDs(Array.from(e.target.selectedOptions).map((o) => o.value))}>
                {datasets.map((d) => <option key={d.id} value={d.id}>{d.symbol} · {d.frequency} · {d.provider_id}{d.is_synthetic ? " · synthetic" : ""}</option>)}
              </select>
              <label className="block text-xs text-amber-700 dark:text-amber-400">
                <input type="checkbox" aria-label="allow unvalidated demo" checked={allowDemo} onChange={(e) => setAllowDemo(e.target.checked)} />{" "}
                {demo.note}
              </label>
            </>
          )}
        </div>
      </div>
      <div className="mt-3 flex items-center gap-2">
        <button className="btn" disabled={!canCreate} onClick={submit}>Create session</button>
        {by.trim().length < 2 && <span className="text-xs text-slate-500">Enter your name above to act.</span>}
      </div>
      <ErrorBox error={err} />
    </Section>
  );
}

// -- detail ------------------------------------------------------------------------------------------
type Tab = "orders" | "positions" | "risk" | "events" | "evaluation" | "config";

function EquityChart({ rows, ev }: { rows: PaperEquityOut[]; ev: PaperEvaluation | null }) {
  const c = chartColors();
  const data = useMemo(() => {
    if (ev) return ev.curves.ts.map((t, i) => ({ ts: t, paper: ev.curves.paper[i], backtest: ev.curves.backtest[i] }));
    return rows.map((r) => ({ ts: r.bar_ts.slice(0, 10), paper: r.equity / (rows[0]?.equity || 1) }));
  }, [rows, ev]);
  if (!data.length) return <p className="text-sm text-slate-500">No bars processed yet.</p>;
  return (
    <div className="h-56">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={c.grid} vertical={false} />
          <XAxis dataKey="ts" tick={{ fontSize: 11, fill: c.axis }} minTickGap={60} />
          <YAxis domain={["auto", "auto"]} tick={{ fontSize: 11, fill: c.axis }} width={48} />
          <Tooltip formatter={(v: number) => fmt(v)} />
          <Legend />
          <Line dataKey="paper" name="paper (simulated)" stroke={c.series[0]} strokeWidth={2} dot={false} isAnimationActive={false} />
          {ev && <Line dataKey="backtest" name="backtest, same bars" stroke={c.series[1]} strokeWidth={2} dot={false} isAnimationActive={false} />}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function Detail({ id, by, onChanged, onDeleted }: { id: string; by: string; onChanged: () => void; onDeleted: () => void }) {
  const [s, setS] = useState<PaperSessionOut | null>(null);
  const [tab, setTab] = useState<Tab>("orders");
  const [ordersRows, setOrders] = useState<PaperOrderOut[]>([]);
  const [positions, setPositions] = useState<PaperPositionOut[]>([]);
  const [equity, setEquity] = useState<PaperEquityOut[]>([]);
  const [risk, setRisk] = useState<PaperRiskOut[]>([]);
  const [events, setEvents] = useState<PaperEventOut[]>([]);
  const [ev, setEv] = useState<PaperEvaluation | null>(null);
  const [of, setOf] = useState({ status: "", strategy: "", instrument: "" });
  const [efilter, setEfilter] = useState({ level: "", category: "", strategy: "", instrument: "", start: "", end: "" });
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const can = by.trim().length >= 2;

  const load = useCallback(async () => {
    try {
      const [ss, eq, pos] = await Promise.all([api.session(id), api.equity(id), api.positions(id)]);
      setS(ss); setEquity(eq); setPositions(pos);
      setOrders(await api.orders(id, of));
      setRisk(await api.risk(id, "blocked"));
      setEvents(await api.events(id, efilter));
      if (ss.verdict) setEv(await api.evaluation(id).catch(() => null));
      else setEv(null);
    } catch (e) {
      setErr(String(e));
    }
  }, [id, of, efilter]);

  useEffect(() => { load(); }, [load]);

  const act = async (fn: () => Promise<unknown>) => {
    setErr(null); setBusy(true);
    try { await fn(); await load(); onChanged(); } catch (e) { setErr(String(e)); } finally { setBusy(false); }
  };
  if (!s) return <ErrorBox error={err} />;
  const sm = s.summary;
  const strategies = s.strategies.concat(["MANUAL"]);
  const instruments = s.data_refs.map((d) => d.symbol);
  const pausedByData = s.status === "PAUSED" && (s.pause_reason ?? "").startsWith("data quality");

  return (
    <div className="space-y-3">
      <Section title={s.name} right={<Badge tone={STATUS_TONE[s.status] ?? "slate"}>{s.status}</Badge>}>
        <Labels s={s} />
        {s.pause_reason && <p className="mt-2 rounded bg-amber-50 p-2 text-sm text-amber-800 dark:bg-amber-950 dark:text-amber-300">{s.pause_reason}</p>}
        {s.notes.map((n) => <p key={n} className="mt-1 text-xs text-slate-500">{n}</p>)}
        <div className="mt-3 flex flex-wrap gap-2">
          <button className="btn" disabled={!can || busy || !["CREATED", "PAUSED"].includes(s.status) || s.kill_switch} onClick={() => act(() => api.start(id, by))}>
            {s.status === "CREATED" ? "Start" : "Resume"}
          </button>
          <button className="btn-ghost" disabled={!can || busy || s.status !== "RUNNING"} onClick={() => act(() => api.pause(id, by))}>Pause</button>
          <button className="btn-ghost" disabled={!can || busy || s.status !== "RUNNING"} onClick={() => act(() => api.step(id, by, 1))}>Step 1 bar</button>
          <button className="btn-ghost" disabled={!can || busy || s.status !== "RUNNING"} onClick={() => act(() => api.step(id, by, 20))}>Step 20 bars</button>
          <button className="btn-ghost" disabled={!can || busy || s.status !== "RUNNING"} onClick={() => act(() => api.run(id, by))}>Run in background</button>
          {pausedByData && (
            <button className="btn-ghost" disabled={!can || busy} onClick={() => window.confirm("Skip the bar that failed the data checks? This is logged.") && act(() => api.step(id, by, 1, true))}>
              Skip bad bar
            </button>
          )}
          <button className="btn-ghost" disabled={busy || s.bars_processed === 0} onClick={() => act(async () => setEv(await api.evaluate(id)))}>Evaluate vs backtest</button>
          {!s.kill_switch ? (
            <button className="rounded-md bg-red-600 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50" disabled={!can || busy || ["COMPLETED", "HALTED"].includes(s.status)}
              onClick={() => window.confirm("Activate the kill switch? All open orders are cancelled and no new orders are accepted.") && act(() => api.killSwitch(id, by, true, "manual"))}>
              Kill switch
            </button>
          ) : (
            <button className="btn-ghost" disabled={!can || busy} onClick={() => window.confirm("Release the kill switch? The session stays paused until started.") && act(() => api.killSwitch(id, by, false, "released"))}>
              Release kill switch
            </button>
          )}
          <button className="btn-ghost" disabled={!can || busy} onClick={() => window.confirm("Reset this session? Orders, fills and positions are cleared; the previous summary is kept in the experiment record.") && act(() => api.reset(id, by))}>Reset</button>
          <button className="btn-ghost" disabled={!can || busy} onClick={async () => {
            if (!window.confirm("Delete this session? Its experiment record is kept, marked deleted.")) return;
            try { await api.remove(id, by); onChanged(); onDeleted(); } catch (e) { setErr(String(e)); }
          }}>Delete</button>
          <span className="ml-auto text-xs text-slate-500">Export:</span>
          {["orders", "fills", "equity", "ledger", "events"].map((t) => (
            <a key={t} className="text-xs text-indigo-600 underline" href={api.exportUrl(id, t)}>{t}</a>
          ))}
        </div>
        <ErrorBox error={err} />
      </Section>

      <div className="grid grid-cols-2 gap-2 md:grid-cols-5">
        <Tile label={`Equity (${s.account_currency})`} value={money(sm.equity)} note={pct(sm.return_net)} />
        <Tile label="Realized P&L" value={money(sm.realized_pnl)} note="closed positions" />
        <Tile label="Unrealized P&L" value={money(sm.unrealized_pnl)} note="open positions, not realized" />
        <Tile label="Fees + holding costs" value={money((sm.fees ?? 0) + (sm.holding_costs ?? 0))} note={`fees ${money(sm.fees)}`} />
        <Tile label="Gross exposure" value={pct(sm.gross_exposure, 1)} note={`net ${pct(sm.net_exposure, 1)}`} />
        <Tile label="Drawdown" value={pct(sm.drawdown)} />
        <Tile label="Margin used" value={money(sm.margin_used)} />
        <Tile label="Bars processed" value={String(s.bars_processed)} note={`last bar ${fmtTs(s.cursor_ts)}`} />
        <Tile label="Risk policy" value={s.policy_version.split("#")[0]} />
        <Tile label="Verdict" value={s.verdict ?? "not evaluated"} note="not an approval to trade" />
      </div>

      <Section title="Equity">
        <EquityChart rows={equity} ev={ev} />
        {s.last_signals && Object.keys(s.last_signals).length > 0 && (
          <div className="mt-2 text-xs text-slate-600 dark:text-slate-400">
            Last signals:{" "}
            {Object.entries(s.last_signals).map(([k, v]) => (
              <span key={k} className="mr-3">{k} @ {fmtTs(v.bar_ts)}: {Object.entries(v.targets).map(([sym, w]) => `${sym} ${w >= 0 ? "+" : ""}${w.toFixed(2)}`).join(", ")}</span>
            ))}
          </div>
        )}
      </Section>

      <div className="flex gap-1">
        {(["orders", "positions", "risk", "events", "evaluation", "config"] as Tab[]).map((t) => (
          <button key={t} className={`rounded-md px-3 py-1 text-sm ${tab === t ? "bg-slate-200 font-medium dark:bg-slate-800" : ""}`} onClick={() => setTab(t)}>
            {t === "risk" ? "risk blocks" : t}
          </button>
        ))}
      </div>

      {tab === "orders" && (
        <Section title="Orders">
          <div className="mb-2 flex flex-wrap gap-2 text-xs">
            <select aria-label="order status" className="input" value={of.status} onChange={(e) => setOf({ ...of, status: e.target.value })}>
              <option value="">all statuses</option>
              {["PENDING,PARTIALLY_FILLED", "FILLED", "REJECTED", "CANCELLED", "EXPIRED"].map((x) => <option key={x} value={x}>{x}</option>)}
            </select>
            <select aria-label="order strategy" className="input" value={of.strategy} onChange={(e) => setOf({ ...of, strategy: e.target.value })}>
              <option value="">all strategies</option>{strategies.map((x) => <option key={x}>{x}</option>)}
            </select>
            <select aria-label="order instrument" className="input" value={of.instrument} onChange={(e) => setOf({ ...of, instrument: e.target.value })}>
              <option value="">all instruments</option>{instruments.map((x) => <option key={x}>{x}</option>)}
            </select>
          </div>
          <div className="max-h-96 overflow-auto">
            <table className="min-w-full text-sm">
              <thead><tr>{["Decided", "Strategy", "Instrument", "Side", "Qty", "Type", "Limit/stop", "Purpose", "Status", "Filled", "Avg price", "Ref. price", "Fees", "Reason", ""].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
              <tbody>
                {ordersRows.map((o) => (
                  <tr key={o.id}>
                    <td className="td text-xs">{fmtTs(o.created_ts)}</td>
                    <td className="td text-xs">{o.strategy_id}</td>
                    <td className="td">{o.instrument}</td>
                    <td className="td">{o.side}</td>
                    <td className="td tabular-nums">{fmt(o.quantity)}</td>
                    <td className="td">{o.order_type}</td>
                    <td className="td tabular-nums">{o.limit_price ? fmt(o.limit_price) : ""}{o.stop_price ? ` / ${fmt(o.stop_price)}` : ""}</td>
                    <td className="td text-xs">{o.purpose}</td>
                    <td className="td"><Badge tone={o.status === "FILLED" ? "green" : o.status === "REJECTED" ? "red" : OPEN.has(o.status) ? "indigo" : "slate"}>{o.status}</Badge></td>
                    <td className="td tabular-nums">{fmt(o.filled_quantity)}</td>
                    <td className="td tabular-nums">{o.avg_fill_price ? fmt(o.avg_fill_price) : "—"}</td>
                    <td className="td tabular-nums">{fmt(o.reference_price)}</td>
                    <td className="td tabular-nums">{money(o.commission)}</td>
                    <td className="td text-xs whitespace-normal">{o.reject_reason ?? o.cancel_reason ?? ""}</td>
                    <td className="td">{OPEN.has(o.status) && <button className="text-xs text-indigo-600 underline" disabled={!can} onClick={() => act(() => api.cancelOrder(id, o.id, by))}>cancel</button>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!ordersRows.length && <p className="text-sm text-slate-500">No orders.</p>}
          </div>
        </Section>
      )}

      {tab === "positions" && (
        <Section title="Positions">
          <table className="min-w-full text-sm">
            <thead><tr>{["Strategy", "Instrument", "Quantity", "Avg price", "Last", "Notional", "Realized", "Unrealized (not realized)", "Fees", "Holding costs"].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
            <tbody>
              {positions.map((p) => (
                <tr key={p.strategy_id + p.instrument}>
                  <td className="td text-xs">{p.strategy_id}</td><td className="td">{p.instrument}</td>
                  <td className="td tabular-nums">{fmt(p.quantity)}</td><td className="td tabular-nums">{fmt(p.avg_price)}</td>
                  <td className="td tabular-nums">{fmt(p.last_price)}</td><td className="td tabular-nums">{money(p.notional)}</td>
                  <td className="td tabular-nums">{money(p.realized_pnl)}</td><td className="td tabular-nums">{money(p.unrealized_pnl)}</td>
                  <td className="td tabular-nums">{money(p.fees)}</td><td className="td tabular-nums">{money(p.holding_costs)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!positions.length && <p className="text-sm text-slate-500">No positions.</p>}
        </Section>
      )}

      {tab === "risk" && (
        <Section title="Risk blocks (pre-trade checks that rejected or reduced an order)">
          <div className="max-h-96 overflow-auto">
            <table className="min-w-full text-sm">
              <thead><tr>{["As of", "Scope", "Rule", "Observed", "Limit", "Action", "Reason"].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
              <tbody>
                {risk.map((r, i) => (
                  <tr key={i}>
                    <td className="td text-xs">{fmtTs(r.as_of)}</td><td className="td text-xs">{r.scope}</td><td className="td">{r.rule}</td>
                    <td className="td">{r.observed}</td><td className="td">{r.limit}</td><td className="td text-xs">{r.action}</td>
                    <td className="td text-xs whitespace-normal">{r.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!risk.length && <p className="text-sm text-slate-500">No order was blocked.</p>}
          </div>
        </Section>
      )}

      {tab === "events" && (
        <Section title="Event history">
          <div className="mb-2 flex flex-wrap gap-2 text-xs">
            <select aria-label="event level" className="input" value={efilter.level} onChange={(e) => setEfilter({ ...efilter, level: e.target.value })}>
              <option value="">all levels</option>{["info", "warn", "error,critical"].map((x) => <option key={x}>{x}</option>)}
            </select>
            <select aria-label="event category" className="input" value={efilter.category} onChange={(e) => setEfilter({ ...efilter, category: e.target.value })}>
              <option value="">all categories</option>
              {["data", "signal", "order", "fill", "risk", "reconcile", "config", "system", "evaluation"].map((x) => <option key={x}>{x}</option>)}
            </select>
            <select aria-label="event strategy" className="input" value={efilter.strategy} onChange={(e) => setEfilter({ ...efilter, strategy: e.target.value })}>
              <option value="">all strategies</option>{strategies.map((x) => <option key={x}>{x}</option>)}
            </select>
            <select aria-label="event instrument" className="input" value={efilter.instrument} onChange={(e) => setEfilter({ ...efilter, instrument: e.target.value })}>
              <option value="">all instruments</option>{instruments.map((x) => <option key={x}>{x}</option>)}
            </select>
            <input aria-label="events from" type="date" className="input" value={efilter.start} onChange={(e) => setEfilter({ ...efilter, start: e.target.value })} />
            <input aria-label="events to" type="date" className="input" value={efilter.end} onChange={(e) => setEfilter({ ...efilter, end: e.target.value })} />
          </div>
          <div className="max-h-96 overflow-auto">
            <table className="min-w-full text-sm">
              <thead><tr>{["#", "Simulated time", "Level", "Category", "Code", "Message"].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
              <tbody>
                {events.map((e) => (
                  <tr key={e.seq}>
                    <td className="td text-xs">{e.seq}</td><td className="td text-xs">{fmtTs(e.sim_ts)}</td>
                    <td className="td"><Badge tone={e.level === "info" ? "slate" : e.level === "warn" ? "amber" : "red"}>{e.level}</Badge></td>
                    <td className="td text-xs">{e.category}</td><td className="td text-xs">{e.code}</td>
                    <td className="td text-xs whitespace-normal">{e.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}

      {tab === "evaluation" && (
        <Section title="Evaluation against the backtest on the same bars">
          {!ev ? <p className="text-sm text-slate-500">Not evaluated yet.</p> : (
            <div className="space-y-2 text-sm">
              <p><Badge tone={ev.verdict === "consistent_with_backtest" ? "green" : ev.verdict === "deviates_from_backtest" ? "red" : "amber"}>{ev.verdict}</Badge></p>
              <p className="text-xs text-slate-600 dark:text-slate-400">{ev.note}</p>
              {ev.reasons.length > 0 && <ul className="list-disc pl-5 text-xs">{ev.reasons.map((r) => <li key={r}>{r}</li>)}</ul>}
              <table className="text-sm">
                <tbody>
                  {Object.entries(ev.metrics).map(([k, v]) => (
                    <tr key={k}><td className="td text-xs text-slate-500">{k}</td><td className="td tabular-nums">{fmt(v)}</td></tr>
                  ))}
                </tbody>
              </table>
              {ev.regime.filter((r) => r.shift).map((r) => (
                <p key={r.instrument} className="text-xs text-amber-700">Regime change on {r.instrument}: volatility ×{r.vol_ratio_session_vs_prior_250} vs the prior 250 bars.</p>
              ))}
              <p className="text-xs text-slate-500">Criteria fixed at creation (hash {ev.criteria_hash.slice(0, 12)}): {Object.entries(ev.criteria).map(([k, v]) => `${k}=${v}`).join(", ")}</p>
            </div>
          )}
        </Section>
      )}

      {tab === "config" && (
        <Section title="Configuration and reproducibility">
          <p className="text-xs">Code {s.code_version} · config v{s.config_version} ({s.config_hash?.slice(0, 12)}) · policy {s.policy_version} · experiment {s.experiment_id}</p>
          <ul className="mt-1 list-disc pl-5 text-xs">
            {s.data_refs.map((d) => <li key={d.dataset_id}>{d.symbol}: dataset {d.dataset_id.slice(0, 8)} · {d.provider} · hash {d.content_hash.slice(0, 12)}{d.is_synthetic ? " · synthetic" : ""}</li>)}
          </ul>
          <ul className="mt-2 list-disc pl-5 text-xs">
            {s.plans.map((p) => <li key={p.id}>{p.id} ({p.kind}, {p.status}) · {p.type} {JSON.stringify(p.params)} · allocation {p.allocation}</li>)}
          </ul>
          <div className="mt-2 text-xs font-medium">Configuration history</div>
          <ul className="list-disc pl-5 text-xs">
            {s.config_history.map((h) => <li key={h.version}>v{h.version} {fmtTs(h.at)} by {h.by}: {h.change ?? JSON.stringify(h.diff)} {h.reason ? `(${h.reason})` : ""}</li>)}
          </ul>
        </Section>
      )}
    </div>
  );
}

// -- page --------------------------------------------------------------------------------------------
export default function PaperTradingPage() {
  const [sessions, setSessions] = useState<PaperSessionOut[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [compareIds, setCompareIds] = useState<string[]>([]);
  const [comparison, setComparison] = useState<Record<string, any>[] | null>(null);
  const [by, setBy] = useState("");
  const [creating, setCreating] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(() => api.sessions().then(setSessions).catch((e) => setErr(String(e))), []);
  useEffect(() => { load(); }, [load]);

  return (
    <div className="space-y-4">
      <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200">
        Paper trading is a simulation. No order is sent to any broker, exchange or account. A historical replay is a
        reproduction of the past, not real-time trading, and no result here makes a strategy ready to trade.
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <label className="text-sm">Your name <input aria-label="acting person" className="input ml-1" value={by} onChange={(e) => setBy(e.target.value)} /></label>
        <button className="btn-ghost" onClick={() => setCreating((x) => !x)}>{creating ? "Close" : "New session"}</button>
        <button className="btn-ghost" disabled={compareIds.length < 2} onClick={() => api.compare(compareIds).then(setComparison).catch((e) => setErr(String(e)))}>
          Compare selected
        </button>
      </div>
      <ErrorBox error={err} />
      {creating && <CreateForm by={by} onCreated={(s) => { setCreating(false); load(); setSelected(s.id); }} />}

      <Section title="Sessions">
        {!sessions.length && <p className="text-sm text-slate-500">No paper sessions yet.</p>}
        {sessions.length > 0 && (
          <table className="min-w-full text-sm">
            <thead><tr>{["", "Session", "Status", "Data", "Strategies", "Equity", "Return", "Verdict", "Created"].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
            <tbody>
              {sessions.map((s) => (
                <tr key={s.id} className={selected === s.id ? "bg-slate-100 dark:bg-slate-800" : ""}>
                  <td className="td"><input type="checkbox" aria-label={`compare ${s.name}`} checked={compareIds.includes(s.id)}
                    onChange={() => setCompareIds((x) => (x.includes(s.id) ? x.filter((y) => y !== s.id) : [...x, s.id]))} /></td>
                  <td className="td"><button className="text-indigo-600 underline" onClick={() => setSelected(s.id)}>{s.name}</button></td>
                  <td className="td"><Badge tone={STATUS_TONE[s.status] ?? "slate"}>{s.status}</Badge></td>
                  <td className="td text-xs">{s.mode === "replay" ? "replay" : "new data"}{s.is_synthetic && <> · <Badge tone="amber">synthetic</Badge></>}{s.is_demo && <> · <Badge tone="amber">unvalidated demo</Badge></>}</td>
                  <td className="td text-xs">{s.strategies.join(", ")}</td>
                  <td className="td tabular-nums">{money(s.summary.equity)}</td>
                  <td className="td tabular-nums">{pct(s.summary.return_net)}</td>
                  <td className="td text-xs">{s.verdict ?? "—"}</td>
                  <td className="td text-xs">{fmtTs(s.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>

      {comparison && (
        <Section title="Session comparison">
          <table className="min-w-full text-sm">
            <thead><tr>{["Session", "Bars", "Costs", "Orders", "Stop", "Return", "Drawdown", "Fill rate", "Gap vs backtest", "Verdict"].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
            <tbody>
              {comparison.map((c) => (
                <tr key={c.id}>
                  <td className="td">{c.name}{c.is_synthetic ? " · synthetic" : ""}</td><td className="td">{c.bars}</td>
                  <td className="td">{c.cost_scenario}</td><td className="td">{c.order_type}</td><td className="td">{c.stop_loss_pct ? pct(c.stop_loss_pct, 1) : "—"}</td>
                  <td className="td">{pct(c.summary.return_net)}</td><td className="td">{pct(c.summary.drawdown)}</td>
                  <td className="td">{pct(c.fill_rate, 0)}</td><td className="td">{pct(c.return_gap)}</td><td className="td text-xs">{c.verdict ?? "not evaluated"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      )}

      {selected && <Detail key={selected} id={selected} by={by} onChanged={load} onDeleted={() => setSelected(null)} />}
    </div>
  );
}

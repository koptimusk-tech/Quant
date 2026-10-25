"""Command line entry points.

    python -m qal.cli init-db
    python -m qal.cli demo                     # synthetic data + scan, no network needed
    python -m qal.cli ingest yahoo SPY etf --start 2015-01-01
    python -m qal.cli ingest ccxt BTC/USDT crypto --frequency 1h --exchange binance
    python -m qal.cli scan --all
    python -m qal.cli worker                   # run jobs and schedules without the API
    python -m qal.cli predictive-study --out runs   # event studies on the real EURUSD/USDCHF datasets
    python -m qal.cli alpha-cycle --out alpha_runs  # one bounded cycle of the research agents
    python -m qal.cli model-lab --frequency 1d --benchmark --out model_runs  # compare anomaly models
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from qal.anomaly.engine import ScanConfig, run_scan
from qal.config import get_settings
from qal.data.ingest import IngestRequest, ingest
from qal.data.store import ParquetStore
from qal.db import create_all, session_scope
from qal.models import Dataset, Experiment


def cmd_demo(args: argparse.Namespace) -> None:
    create_all()
    ids = {}
    with session_scope() as s:
        for sym in ("SYN-A", "SYN-B", "SYN-C", "SYN-D"):
            r = ingest(s, IngestRequest(adapter="synthetic", symbol=sym, asset_class="other",
                                        options={"seed": args.seed, "n_bars": args.n_bars}), actor="cli")
            ids[sym] = r["dataset_id"]
    with session_scope() as s:
        exp = run_scan(s, ScanConfig(dataset_ids=list(ids.values()),
                                     detectors={"return_shock": {}, "volume_anomaly": {}, "volatility_regime": {},
                                                "pair_divergence": {}},
                                     pairs=[(ids["SYN-B"], ids["SYN-A"]), (ids["SYN-D"], ids["SYN-C"])],
                                     label="synthetic demo"),
                       store=ParquetStore(get_settings().data_dir), actor="cli")
        print(json.dumps({"experiment_id": exp.id, "status": exp.status, **exp.results}, indent=2, default=str))
    print("SYNTHETIC DATA: these anomalies include deliberately injected test events.")


def cmd_ingest(args: argparse.Namespace) -> None:
    create_all()
    opts = json.loads(args.options) if args.options else {}
    cfg = {"exchange": args.exchange} if args.exchange else {}
    with session_scope() as s:
        r = ingest(s, IngestRequest(adapter=args.adapter, symbol=args.symbol, asset_class=args.asset_class,
                                    frequency=args.frequency, market=args.market or "",
                                    start=datetime.fromisoformat(args.start) if args.start else None,
                                    end=datetime.fromisoformat(args.end) if args.end else None,
                                    options=opts, adapter_config=cfg), actor="cli")
    print(json.dumps(r, indent=2, default=str))


def cmd_scan(args: argparse.Namespace) -> None:
    create_all()
    with session_scope() as s:
        ids = args.dataset or [d.id for d in s.scalars(select(Dataset).where(Dataset.is_latest.is_(True))).all()]
        pairs = [tuple(p.split(",")) for p in args.pair]
        exp = run_scan(s, ScanConfig(dataset_ids=ids, pairs=pairs, force=args.force),
                       store=ParquetStore(get_settings().data_dir), actor="cli")
        print(json.dumps({"experiment_id": exp.id, "status": exp.status, "error": exp.error, **exp.results},
                         indent=2, default=str))


def cmd_predictive(args: argparse.Namespace) -> None:
    from qal.research import predictive

    create_all()
    spreads = []
    for spec in args.spread_csv:
        # SYMBOL:FREQ:POINT:PATH (the path goes last so Windows drive letters survive the split)
        sym, freq, point, path = spec.split(":", 3)
        spreads.append(predictive.spread_from_mt5_csv(path, sym, freq, float(point), args.spread_csv_timezone))
    symbols = tuple(s.strip() for s in args.symbols.split(",") if s.strip())
    freqs = tuple(f.strip() for f in args.frequencies.split(",") if f.strip())
    if args.spread_mt5:
        for sym in symbols:
            for freq in freqs:
                try:
                    spreads.append(predictive.spread_from_mt5_terminal(sym, freq, args.mt5_offset_hours))
                except Exception as exc:  # reported, never replaced by an estimate presented as real
                    print(f"historical spread unavailable for {sym} {freq}: {exc}")
    pair = tuple(args.pair.split(",")) if args.pair else None
    cfg = predictive.RunConfig(symbols=symbols, frequencies=freqs, seed=args.seed, pair=pair, spreads=spreads,
                               horizons=tuple(int(h) for h in args.horizons.split(",")))
    out = Path(args.out) / f"run_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    with session_scope() as s:
        r = predictive.run(s, ParquetStore(get_settings().data_dir), cfg, out)
    print(json.dumps({"out_dir": str(out), "run_hash": r["run_hash"],
                      "missing": [f"{m['symbol']} {m['frequency']}" for m in r["missing_datasets"]],
                      "verdicts": [f"{c['dataset']} {c['detector']}: {c.get('verdict') or c['status']}"
                                   for c in r["comparison"]]}, indent=2))


def cmd_alpha_cycle(args: argparse.Namespace) -> None:
    from qal.alpha.agents import ResearchCoordinator
    from qal.alpha.messages import Budget
    from qal.alpha.report import hypothesis_detail, run_detail
    from qal.models import Hypothesis
    from qal.research.costs import CostModel

    create_all()
    costs = {k: CostModel(**v) for k, v in json.loads(Path(args.costs).read_text()).items()} if args.costs else {}
    budget = Budget(max_ideas=args.max_ideas, max_hypotheses=args.max_hypotheses,
                    max_experiments=args.max_experiments, max_variants=args.max_variants,
                    max_seconds=args.max_seconds)
    with session_scope() as s:
        run = ResearchCoordinator().run_cycle(s, budget, actor="cli", costs=costs,
                                              run_experiments=not args.no_experiments, checkpoint=s.commit)
        s.flush()
        detail = run_detail(s, run)
        hyp_ids = {m["hypothesis_id"] for m in detail["messages"] if m["hypothesis_id"]}
        detail["hypotheses"] = [hypothesis_detail(s, s.get(Hypothesis, h)) for h in sorted(hyp_ids)]
        rel = s.get(Experiment, run.results["relationship_scan_id"]) if run.results.get("relationship_scan_id") else None
        detail["relationship_scan"] = {"id": rel.id, **rel.results} if rel else None
        if args.out:
            out = Path(args.out) / f"alpha_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
            out.mkdir(parents=True, exist_ok=True)
            (out / "alpha_run.json").write_text(json.dumps(detail, indent=2, default=str))
            print(f"written {out / 'alpha_run.json'}")
        print(json.dumps(detail["run"], indent=2, default=str))


def cmd_model_lab(args: argparse.Namespace) -> None:
    from qal.research.model_lab import as_config, run_comparison

    create_all()
    with session_scope() as s:
        if args.datasets:
            ids = args.datasets
        else:  # the latest real dataset of every instrument at the requested frequencies
            q = select(Dataset).where(Dataset.is_latest.is_(True), Dataset.is_synthetic.is_(args.synthetic))
            ids = [d.id for d in s.scalars(q) if not args.frequency or d.frequency in args.frequency]
        body = {"dataset_ids": sorted(ids), "predictive": not args.no_predictive, "benchmark": args.benchmark,
                "max_seconds": args.max_seconds, "label": args.label, "seeds": args.seeds, "force": args.force}
        if args.models:
            body["models"] = [{"detector": m} for m in args.models]
        exp = run_comparison(s, as_config(body), store=ParquetStore(get_settings().data_dir), actor="cli",
                             checkpoint=s.commit)
        s.commit()
        record = {"id": exp.id, "status": exp.status, "error": exp.error, "config": exp.config,
                  "data_refs": exp.data_refs, "results": exp.results}
        if args.out:
            out = Path(args.out) / f"models_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
            out.mkdir(parents=True, exist_ok=True)
            (out / "model_comparison.json").write_text(json.dumps(record, indent=2, default=str))
            print(f"written {out / 'model_comparison.json'}")
        for d in exp.results.get("datasets", []):
            for r in d.get("rows", []) or [{"model": "-", "status": d.get("status"), "reason": d.get("reason")}]:
                pred = r.get("predictive") or {}
                print(f"{d['symbol']:>8} {d['frequency']:>3} {r['model']:<22} {r['status']:<14} "
                      f"flags={r.get('n_flagged')} episodes={r.get('n_episodes')} rate={r.get('flag_rate')} "
                      f"oos={pred.get('verdict')} {r.get('reason') or ''}")
        print(json.dumps({"id": exp.id, "status": exp.status, "duration_s": exp.results.get("duration_s"),
                          "budget_exhausted": exp.results.get("budget_exhausted", False)}))


def cmd_e2e_audit(args: argparse.Namespace) -> None:
    from qal import e2e

    create_all()
    cfg = e2e.E2EConfig()
    if args.adapter != "synthetic":
        cfg = e2e.E2EConfig(adapter=args.adapter, symbols=tuple(args.symbols.split(",")), asset_class=args.asset_class,
                            options=json.loads(args.options) if args.options else {})
    with session_scope() as s:
        report = e2e.run(s, cfg)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"e2e_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(report, indent=2, default=str))
    for st in report["stages"]:
        print(f"{st['stage']:<12} {st['status']:<16} {st['seconds']:>7}s  {st.get('error') or ''}")
    print(f"validated path stops at: {report['validated_path_stops_at']}")
    print(f"written {path}")


def cmd_worker(args: argparse.Namespace) -> None:
    from qal.jobs.runner import process_once

    create_all()
    print("worker running; Ctrl+C to stop")
    while True:
        process_once()
        time.sleep(get_settings().scheduler_poll_seconds)


def main() -> None:
    p = argparse.ArgumentParser(prog="qal")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db").set_defaults(fn=lambda a: create_all())
    d = sub.add_parser("demo")
    d.add_argument("--seed", type=int, default=7)
    d.add_argument("--n-bars", type=int, default=1500)
    d.set_defaults(fn=cmd_demo)
    i = sub.add_parser("ingest")
    i.add_argument("adapter")
    i.add_argument("symbol")
    i.add_argument("asset_class")
    i.add_argument("--frequency", default="1d")
    i.add_argument("--market")
    i.add_argument("--start")
    i.add_argument("--end")
    i.add_argument("--exchange")
    i.add_argument("--options", help="JSON adapter options, e.g. '{\"path\": \"file.csv\", \"format\": \"mt5\"}'")
    i.set_defaults(fn=cmd_ingest)
    sc = sub.add_parser("scan")
    sc.add_argument("--dataset", action="append", default=[])
    sc.add_argument("--pair", action="append", default=[], help="dataset_y,dataset_x")
    sc.add_argument("--force", action="store_true")
    sc.add_argument("--all", action="store_true")
    sc.set_defaults(fn=cmd_scan)
    sub.add_parser("worker").set_defaults(fn=cmd_worker)
    pv = sub.add_parser("predictive-study", help="out-of-sample event studies on real datasets")
    pv.add_argument("--out", default="predictive_runs")
    pv.add_argument("--symbols", default="EURUSD,USDCHF")
    pv.add_argument("--frequencies", default="1d,1h")
    pv.add_argument("--horizons", default="1,5,10,20")
    pv.add_argument("--pair", default="EURUSD,USDCHF", help="Y,X symbols; empty to skip")
    pv.add_argument("--seed", type=int, default=0)
    pv.add_argument("--spread-csv", action="append", default=[], help="SYMBOL:FREQ:POINT:PATH of an MT5 bar export")
    pv.add_argument("--spread-csv-timezone", default=None, help="timezone of the CSV times (default UTC)")
    pv.add_argument("--spread-mt5", action="store_true", help="read bar spreads from the running MT5 terminal")
    pv.add_argument("--mt5-offset-hours", type=float, default=0.0, help="broker server time minus UTC")
    pv.set_defaults(fn=cmd_predictive)
    ac = sub.add_parser("alpha-cycle", help="one bounded research cycle of the four research agents")
    ac.add_argument("--max-ideas", type=int, default=40)
    ac.add_argument("--max-hypotheses", type=int, default=12)
    ac.add_argument("--max-experiments", type=int, default=6)
    ac.add_argument("--max-variants", type=int, default=120)
    ac.add_argument("--max-seconds", type=float, default=900)
    ac.add_argument("--no-experiments", action="store_true", help="register hypotheses but do not validate them")
    ac.add_argument("--costs", help="JSON file {symbol: cost model} with the account's real costs")
    ac.add_argument("--out", default="alpha_runs", help="directory for the full JSON report; empty to skip")
    ac.set_defaults(fn=cmd_alpha_cycle)
    ml = sub.add_parser("model-lab", help="compare anomaly models (Isolation Forest, LOF, baselines) on datasets")
    ml.add_argument("--datasets", nargs="*", help="dataset ids (default: latest real dataset of each instrument)")
    ml.add_argument("--frequency", nargs="*", help="only these frequencies, e.g. 1d 1h")
    ml.add_argument("--synthetic", action="store_true", help="use synthetic datasets instead of real ones")
    ml.add_argument("--models", nargs="*", help="detector names (default: isolation_forest, local_outlier_factor, "
                                                "return_shock, volatility_regime)")
    ml.add_argument("--seeds", nargs="*", type=int, default=[0, 1, 2])
    ml.add_argument("--no-predictive", action="store_true")
    ml.add_argument("--benchmark", action="store_true", help="also run the synthetic known-anomaly benchmark")
    ml.add_argument("--max-seconds", type=float, default=3600)
    ml.add_argument("--label", default="cli")
    ml.add_argument("--force", action="store_true", help="recompute even if an identical comparison is stored")
    ml.add_argument("--out", default=None)
    ml.set_defaults(fn=cmd_model_lab)
    e2 = sub.add_parser("e2e-audit", help="end-to-end run of every stage, data to alert (paper only)")
    e2.add_argument("--adapter", default="synthetic", help="synthetic (default) or a real adapter, e.g. yahoo")
    e2.add_argument("--symbols", default="SPY,QQQ,GLD,TLT", help="real adapter only: comma-separated symbols")
    e2.add_argument("--asset-class", default="etf")
    e2.add_argument("--options", default=None, help="JSON adapter options")
    e2.add_argument("--out", default="e2e_runs")
    e2.set_defaults(fn=cmd_e2e_audit)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

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
    costs = {k: CostModel(**v) for k, v in json.loads(Path(args.costs).read_text(encoding="utf-8")).items()} if args.costs else {}
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


def cmd_lab_run(args: argparse.Namespace) -> None:
    """Experiment Lab from the command line: run spec files and/or the generator's proposals, then rank."""
    from qal.lab import generator, service
    from qal.lab.spec import ExperimentSpec

    create_all()
    store = ParquetStore(get_settings().data_dir)
    with session_scope() as s:
        todo: list[tuple[ExperimentSpec, str | None]] = [
            (ExperimentSpec.model_validate(json.loads(Path(f).read_text(encoding="utf-8"))), None) for f in args.spec]
        if args.generate:
            g = generator.generate(s, families=args.families.split(",") if args.families else None, limit=args.limit)
            print(f"generator proposed {g['n_specs']} specs ({g['n_variants']} variants), skipped {len(g['skipped'])}")
            todo += [(ExperimentSpec.model_validate(o["spec"]), o["generator_key"]) for o in g["proposed"]]
        for spec, key in todo:
            exp, created = service.create(s, spec, actor="cli", generator_key=key)
            service.run(s, exp, store=store, actor="cli", checkpoint=s.commit)
            s.commit()
            d = ((exp.results or {}).get("pipeline") or {}).get("decision_at_completion") or {}
            print(f"{exp.id}  {exp.status:<10} {d.get('decision') or '':<15} {'' if created else '(existing) '}"
                  f"{spec.contract.title}  {exp.error or ''}")
        r = service.ranking(s, include_synthetic=not args.real_only)
        print(f"\nranking ({r['multiple_testing']})")
        for row in r["rows"]:
            print(f"{row['rank']:>3} {row['decision']:<15} score {row['score']:.2f}  adj p {row['adjusted_p']:.3f}  "
                  f"{row['title']}")
        if args.out:
            out = Path(args.out)
            out.mkdir(parents=True, exist_ok=True)
            path = out / f"lab_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
            path.write_text(json.dumps(r, indent=2, default=str))
            print(f"written {path}")


def cmd_robustness_run(args: argparse.Namespace) -> None:
    """Robustness Lab: evaluate completed lab experiments (all candidates, or the ones given) and print the state
    of each dimension. Runs are stored; repeating a run on the same data and configuration returns the stored one."""
    from qal.robustness import service

    create_all()
    store = ParquetStore(get_settings().data_dir)
    with session_scope() as s:
        cands = service.candidates(s)
        ids = args.experiment or [c["experiment_id"] for c in cands
                                  if not (args.real_only and c["is_synthetic"])][: args.limit]
        out = []
        for i in ids:
            e = service.run(s, service.RobustnessConfig(experiment_id=i, max_targets=args.max_targets), store=store,
                            actor="cli", checkpoint=s.commit)
            s.commit()
            r = e.results or {}
            print(f"{e.id}  {e.status:<9} {r.get('state') or '':<32} {e.config.get('lab_title')}  {e.error or ''}")
            for k, d in (r.get("dimensions") or {}).items():
                print(f"      {k:<16} {d['status']:<18} {d['reason']}")
            out.append({"id": e.id, "status": e.status, **r})
        if args.out:
            path = Path(args.out)
            path.mkdir(parents=True, exist_ok=True)
            f = path / f"robustness_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
            f.write_text(json.dumps(out, indent=2, default=str))
            print(f"written {f}")


def cmd_program_audit(args: argparse.Namespace) -> None:
    """Paper Trading Validation: the admission checks of every completed lab experiment. It admits nothing: an
    admission needs a named person and a written justification (API or UI)."""
    from qal.api.routers.program import eligibility

    create_all()
    with session_scope() as s:
        rows = [r for r in eligibility(s) if not (args.real_only and r["is_synthetic"])]
        for r in rows:
            ok = "meets the evidence requirements" if r["meets_evidence_requirements"] else "NOT admissible as validated"
            print(f"{r['lab_experiment_id']}  {r['title']}  decision={r['decision']}  "
                  f"robustness={r['robustness_state']}  -> {ok}")
            for c in r["checks"]:
                print(f"      {'PASS' if c['passed'] else 'FAIL'}  {c['check']:<16} {c['detail']}")
        print(f"{sum(r['meets_evidence_requirements'] for r in rows)} of {len(rows)} candidate(s) meet the evidence "
              "requirements.")
        if args.out:
            path = Path(args.out)
            path.mkdir(parents=True, exist_ok=True)
            f = path / f"program_audit_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
            f.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
            print(f"written {f}")


def cmd_program_run(args: argparse.Namespace) -> None:
    """Paper Trading Validation: move a program forward (simulation only) and print its state."""
    from qal.program import service

    create_all()
    with session_scope() as s:
        out = service.run(s, args.program, args.max_bars)
        print(json.dumps(out, indent=2, default=str))


def cmd_program_report(args: argparse.Namespace) -> None:
    """Paper Trading Validation: write the final report of a program (with its configuration) as JSON."""
    from qal.program import report, service

    create_all()
    with session_scope() as s:
        rep = report.build(s, service.get(s, args.program))
        print(f"{rep['label']}  status={rep['status']}  rules={rep['rule']['state']}  "
              f"classification={rep['classification'].get('state') or 'pending review'}")
        for x in rep["limitations"]:
            print(f"  - {x}")
        path = Path(args.out or ".")
        path.mkdir(parents=True, exist_ok=True)
        f = path / f"paper_validation_{args.program}.json"
        f.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
        print(f"written {f}")



def cmd_review_audit(args: argparse.Namespace) -> None:
    """Strategy Review: the evidence that exists (sessions, evidence class, decisions) and what is still missing.
    It changes nothing."""
    from qal.review import service

    create_all()
    with session_scope() as s:
        out = service.phase_summary(s)
        print(f"status: {out['status']}  ({len(out['subjects'])} paper session(s), {out['prospective_subjects']} with "
              "prospective evidence)")
        for r in out["subjects"]:
            d = r["decision"]
            print(f"  {r['subject']}  " + (f"{d['strategy']} v{d['version']}  {d['evidence_class']}  bars={d['bars']}  "
                                            f"state={d['state']}  current={r['current'].get('state')}" if d else
                                            "not reviewed yet (run review-run)"))
        for x in out["to_collect"]:
            print(f"  to collect: {x}")
        _write(args.out, "review_audit", out)


def cmd_review_run(args: argparse.Namespace) -> None:
    """Strategy Review: run the deterministic decision engine on every paper session and store new decisions."""
    from qal.review import service

    create_all()
    with session_scope() as s:
        rows = service.run(s, args.by)
        for r in rows:
            print(f"{r['subject']}  {r['strategy']} v{r['version']}  {r['evidence_class']}  integrity={r['integrity']}  "
                  f"-> {r['state']}{'' if r['stored'] else ' (unchanged)'}")
            print(f"      {r['reason']}")
        if not rows:
            print("no paper session to review: INSUFFICIENT_EVIDENCE (nothing is fabricated)")
        _write(args.out, "review_run", rows)


def cmd_review_report(args: argparse.Namespace) -> None:
    """Strategy Review: write the report of a decision (JSON and Markdown)."""
    from qal.review import report, service

    create_all()
    with session_scope() as s:
        rep = report.build(s, service.get_decision(s, args.decision))
        path = Path(args.out or ".")
        path.mkdir(parents=True, exist_ok=True)
        (path / f"strategy_review_{args.decision}.json").write_text(json.dumps(rep, indent=2, default=str),
                                                                    encoding="utf-8")
        (path / f"strategy_review_{args.decision}.md").write_text(report.markdown(rep), encoding="utf-8")
        print(f"written {path / f'strategy_review_{args.decision}.md'} and .json")


def _write(out: str | None, name: str, obj: object) -> None:
    if not out:
        return
    path = Path(out)
    path.mkdir(parents=True, exist_ok=True)
    f = path / f"{name}_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    f.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    print(f"written {f}")

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
    lr = sub.add_parser("lab-run", help="Experiment Lab: run spec files or generated hypotheses and rank them")
    lr.add_argument("--spec", action="append", default=[], help="path of an experiment spec JSON (repeatable)")
    lr.add_argument("--generate", action="store_true", help="also run the generator's proposals")
    lr.add_argument("--families", default=None, help="comma-separated families for the generator")
    lr.add_argument("--limit", type=int, default=10)
    lr.add_argument("--real-only", action="store_true", help="rank only experiments on real data")
    lr.add_argument("--out", default=None, help="directory for the ranking JSON")
    lr.set_defaults(fn=cmd_lab_run)
    rr = sub.add_parser("robustness-run", help="Robustness Lab: evaluate completed lab experiments")
    rr.add_argument("--experiment", action="append", default=[], help="lab experiment id (repeatable); default: "
                                                                       "the ranked candidates")
    rr.add_argument("--limit", type=int, default=5, help="number of ranked candidates when no id is given")
    rr.add_argument("--max-targets", type=int, default=12, help="assets for the cross-asset matrix")
    rr.add_argument("--real-only", action="store_true", help="only candidates on real data")
    rr.add_argument("--out", default=None, help="directory for the JSON results")
    rr.set_defaults(fn=cmd_robustness_run)
    pa = sub.add_parser("program-audit", help="Paper Trading Validation: admission checks of the lab candidates")
    pa.add_argument("--real-only", action="store_true")
    pa.add_argument("--out", help="Directory for a JSON copy")
    pa.set_defaults(fn=cmd_program_audit)
    pr = sub.add_parser("program-run", help="Paper Trading Validation: process bars of a program (simulation)")
    pr.add_argument("--program", required=True)
    pr.add_argument("--max-bars", type=int, default=5000)
    pr.set_defaults(fn=cmd_program_run)
    pp = sub.add_parser("program-report", help="Paper Trading Validation: write a program's report")
    pp.add_argument("--program", required=True)
    pp.add_argument("--out", help="Directory (default: current)")
    pp.set_defaults(fn=cmd_program_report)
    ra = sub.add_parser("review-audit", help="Strategy Review: evidence available and what is missing")
    ra.add_argument("--out")
    ra.set_defaults(fn=cmd_review_audit)
    rr = sub.add_parser("review-run", help="Strategy Review: run the decision engine and store new decisions")
    rr.add_argument("--by", required=True, help="who triggers the review (recorded)")
    rr.add_argument("--out")
    rr.set_defaults(fn=cmd_review_run)
    rp = sub.add_parser("review-report", help="Strategy Review: write the report of a decision")
    rp.add_argument("--decision", required=True)
    rp.add_argument("--out")
    rp.set_defaults(fn=cmd_review_report)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

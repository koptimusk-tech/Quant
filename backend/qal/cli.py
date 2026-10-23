"""Command line entry points.

    python -m qal.cli init-db
    python -m qal.cli demo                     # synthetic data + scan, no network needed
    python -m qal.cli ingest yahoo SPY etf --start 2015-01-01
    python -m qal.cli ingest ccxt BTC/USDT crypto --frequency 1h --exchange binance
    python -m qal.cli scan --all
    python -m qal.cli worker                   # run jobs and schedules without the API
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime

from sqlalchemy import select

from qal.anomaly.engine import ScanConfig, run_scan
from qal.config import get_settings
from qal.data.ingest import IngestRequest, ingest
from qal.data.store import ParquetStore
from qal.db import create_all, session_scope
from qal.models import Dataset


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
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

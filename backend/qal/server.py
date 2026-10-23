"""Standalone API server entry point.

Used by the desktop app's bundled sidecar (PyInstaller) and by `python -m qal.server`. It runs the same
FastAPI app as `uvicorn qal.api.main:app`, passing the app object so a frozen build needs no import strings.
"""
from __future__ import annotations

import argparse
import multiprocessing


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="qal-server", description="Quant Anomaly Lab local API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    import uvicorn

    from qal.api.main import app

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()

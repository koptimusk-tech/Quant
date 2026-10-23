"""Freeze the Python backend into a self-contained folder (desktop/sidecar/qal-server/).

The desktop installer ships this folder so users do not need Python. Run it with the interpreter that has the
backend and its provider extras installed, on the platform you are building for (PyInstaller does not
cross-compile):

    pip install -e "backend[postgres,yahoo,crypto]" pyinstaller   # plus [metatrader] on Windows
    python desktop/scripts/build_sidecar.py
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
OUT = ROOT / "desktop" / "sidecar"

# Imported lazily by the data adapters; bundled when present so downloads work out of the box.
OPTIONAL_PROVIDERS = ["yfinance", "ccxt", "MetaTrader5", "psycopg"]


def main() -> None:
    work = OUT / "build"
    args = [
        str(BACKEND / "qal" / "server.py"),
        "--name", "qal-server",
        "--onedir",
        "--noconfirm",
        "--clean",
        "--distpath", str(OUT),
        "--workpath", str(work),
        "--specpath", str(work),
        "--paths", str(BACKEND),
        "--collect-submodules", "qal",
        "--collect-submodules", "uvicorn",
        "--exclude-module", "tkinter",
        "--exclude-module", "pytest",
        "--exclude-module", "IPython",
        "--exclude-module", "matplotlib",
        "--exclude-module", "sklearn",
        "--exclude-module", "PIL",
    ]
    for mod in OPTIONAL_PROVIDERS:
        if importlib.util.find_spec(mod) is None:
            print(f"[sidecar] {mod} not installed: that provider will report an error in the app", file=sys.stderr)
            continue
        args += ["--hidden-import", mod]
        if mod == "ccxt":
            args += ["--collect-submodules", "ccxt"]
    PyInstaller.__main__.run(args)
    shutil.rmtree(work, ignore_errors=True)
    print(f"[sidecar] built {OUT / 'qal-server'}")


if __name__ == "__main__":
    main()

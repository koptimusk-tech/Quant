from __future__ import annotations

import os
import subprocess
import sys

from qal.api.main import _parent_alive


def test_parent_alive_tracks_the_launcher_process():
    assert _parent_alive(os.getppid())
    # A finished process (on POSIX: any process that is no longer our parent) makes the watchdog exit.
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert not _parent_alive(child.pid)


def test_server_entry_point_parses_arguments(monkeypatch):
    import uvicorn

    from qal import server

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw, app=app))
    server.main(["--port", "9123"])
    assert seen["host"] == "127.0.0.1" and seen["port"] == 9123
    assert seen["app"].title == "Quant Anomaly Lab API"

"""FastAPI application. Versioned under /api/v1; OpenAPI docs at /docs and /api/v1/openapi.json."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal import __version__
from qal.api.routers import (
    alpha,
    anomalies,
    data,
    hypotheses,
    jobs,
    lab,
    model_lab,
    paper,
    portfolio,
    research,
    program,
    review,
    robustness,
)
from qal.api.schemas import AuditOut
from qal.config import get_settings
from qal.db import create_all, get_engine, get_session
from qal.models import AuditEvent
from qal.versioning import code_version

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    get_engine()
    if settings.database_url.startswith("sqlite"):
        create_all()  # dev convenience; PostgreSQL deployments run `alembic upgrade head`
    _watch_parent()
    worker = None
    if settings.enable_scheduler:
        from qal.jobs.runner import Worker

        worker = Worker(settings.scheduler_poll_seconds)
        worker.start()
    yield
    if worker:
        worker.stop()


def _parent_alive(pid: int) -> bool:
    import os

    if os.name == "nt":
        # getppid() keeps returning the original id on Windows, so ask the OS whether that process still runs.
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x00000102  # WAIT_TIMEOUT: still running
        finally:
            kernel32.CloseHandle(handle)
    return os.getppid() == pid


def _watch_parent() -> None:
    """When launched by the desktop app, exit if that process disappears (no orphaned API)."""
    import os
    import threading
    import time

    if not os.environ.get("QAL_EXIT_WITH_PARENT"):
        return
    parent = os.getppid()

    def watch() -> None:
        while True:
            time.sleep(2)
            if not _parent_alive(parent):
                os._exit(0)

    threading.Thread(target=watch, name="qal-parent-watch", daemon=True).start()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Quant Anomaly Lab API", version=__version__, lifespan=lifespan,
                  openapi_url="/api/v1/openapi.json", docs_url="/docs",
                  description="Point-in-time anomaly discovery. Detected anomalies are observations for research, "
                              "not trading signals. Real order execution is not implemented.")
    app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
                       allow_methods=["*"], allow_headers=["*"])
    v1 = APIRouter(prefix="/api/v1")

    @v1.get("/health", tags=["system"])
    def health() -> dict:
        return {"status": "ok", "version": __version__, "code_version": code_version(),
                "live_trading_enabled": False, "scheduler": settings.enable_scheduler}

    @v1.get("/audit", response_model=list[AuditOut], tags=["system"])
    def audit_log(limit: int = Query(100, le=1000), s: Session = Depends(get_session)):
        return s.scalars(select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(limit)).all()

    for r in (data.router, anomalies.router, research.router, hypotheses.router, jobs.router, alpha.router,
              model_lab.router, portfolio.router, paper.router, lab.router, robustness.router,
              program.router, review.router):
        v1.include_router(r)
    app.include_router(v1)
    if settings.frontend_dist and Path(settings.frontend_dist, "index.html").is_file():
        _mount_frontend(app, Path(settings.frontend_dist))
    return app


def _mount_frontend(app: FastAPI, dist: Path) -> None:
    """Serve the built web UI (used by the desktop app and single-process deployments)."""
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        f = (dist / path).resolve()
        if path and f.is_file() and dist.resolve() in f.parents:
            return FileResponse(f)
        return FileResponse(dist / "index.html")


app = create_app()

"""FastAPI application. Versioned under /api/v1; OpenAPI docs at /docs and /api/v1/openapi.json."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal import __version__
from qal.api.routers import anomalies, data, jobs, research
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
    worker = None
    if settings.enable_scheduler:
        from qal.jobs.runner import Worker

        worker = Worker(settings.scheduler_poll_seconds)
        worker.start()
    yield
    if worker:
        worker.stop()


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

    for r in (data.router, anomalies.router, research.router, jobs.router):
        v1.include_router(r)
    app.include_router(v1)
    return app


app = create_app()

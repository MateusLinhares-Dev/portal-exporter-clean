from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from structlog.contextvars import bind_contextvars, clear_contextvars

from softexpert_exporter.bootstrap import build_container
from softexpert_exporter.config import get_settings
from softexpert_exporter.infrastructure.logging import configure_logging
from softexpert_exporter.interfaces.api import router

settings = get_settings()
configure_logging(settings.LOG_LEVEL)
logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.container = build_container()
    logger.info("application_started", env=settings.APP_ENV)
    try:
        yield
    finally:
        await app.state.container.close()
        logger.info("application_stopped")


app = FastAPI(
    title="SoftExpert Document Exporter",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.APP_ENV != "production" else None,
    redoc_url=None,
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    import uuid

    clear_contextvars()
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    bind_contextvars(request_id=request_id, method=request.method, path=request.url.path)
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        clear_contextvars()


app.include_router(router)

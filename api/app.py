"""FastAPI application factory."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from .rate_limit import check_rate_limit, limit_detail_message, limit_for_bucket
from .routes import router

PACKAGE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = PACKAGE_DIR / "static"
ICON_DIR = PACKAGE_DIR / "Icon"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from ..services.catalog import initialize_catalogs
    from ..services.compute import start_job_reaper, stop_job_reaper
    from ..services.stats import init_stats

    initialize_catalogs()
    init_stats()
    start_job_reaper()
    yield
    stop_job_reaper()


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        allowed, bucket, retry_after, reason = check_rate_limit(request)
        if not allowed:
            limit = limit_for_bucket(bucket or "")
            return JSONResponse(
                status_code=429,
                content={
                    "detail": limit_detail_message(
                        bucket or "api",
                        reason=reason or "burst",
                        retry_after=retry_after or 60,
                        limit=limit,
                    ),
                },
                headers={"Retry-After": str(retry_after or 60)},
            )
        return await call_next(request)


app = FastAPI(title="Phase Diagram Service", version="1.0.0", lifespan=lifespan)
app.add_middleware(RateLimitMiddleware)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/icons", StaticFiles(directory=str(ICON_DIR)), name="icons")
app.include_router(router)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")

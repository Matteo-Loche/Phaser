"""FastAPI application factory."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .routes import router

PACKAGE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = PACKAGE_DIR / "static"
ICON_DIR = PACKAGE_DIR / "Icon"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from ..services.catalog import initialize_catalogs
    from ..services.compute import start_job_reaper, stop_job_reaper

    initialize_catalogs()
    start_job_reaper()
    yield
    stop_job_reaper()


app = FastAPI(title="Phase Diagram Service", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/icons", StaticFiles(directory=str(ICON_DIR)), name="icons")
app.include_router(router)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")

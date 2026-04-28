"""Application entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import settings
from .database import init_db
from .routers import plants, photos, ui

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Augie's Plant Tracker", lifespan=lifespan)

# Static assets (CSS, etc.) bundled with the app.
app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")
ui.templates = templates  # share the instance

app.include_router(ui.router)
app.include_router(plants.router, prefix="/api/plants", tags=["plants"])
app.include_router(photos.router, prefix="/api/photos", tags=["photos"])


@app.get("/media/{rel_path:path}")
def serve_media(rel_path: str) -> Response:
    """Serve files from the data dir (photos + thumbs).

    We deliberately do *not* mount /data as static, so we can validate the
    path stays inside data_dir and isn't an arbitrary file read.
    """
    target = (settings.data_dir / rel_path).resolve()
    try:
        target.relative_to(settings.data_dir.resolve())
    except ValueError:
        return Response(status_code=404)
    if not target.is_file():
        return Response(status_code=404)
    return FileResponse(target)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}

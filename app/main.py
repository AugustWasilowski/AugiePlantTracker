"""Application entry point."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import settings
from .database import init_db
from .routers import plants, photos, sync, ui

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


async def _immich_sync_loop() -> None:
    """Background coroutine: poll Immich every SYNC_INTERVAL_MINUTES.

    Imported lazily so app startup doesn't fail if optional sync deps regress.
    """
    from .sync import run_sync  # local import keeps startup decoupled

    interval = settings.sync_interval_minutes * 60
    await asyncio.sleep(10)  # let the app finish starting before first poll
    while True:
        try:
            stats = await run_sync()
            log.info("Immich sync done: %s", stats)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Immich sync failed (will retry)")
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    sync_task: asyncio.Task | None = None
    retry_task: asyncio.Task | None = None
    if settings.immich_enabled and settings.sync_interval_minutes > 0:
        log.info(
            "Immich auto-import enabled: %s, every %d min",
            settings.immich_url,
            settings.sync_interval_minutes,
        )
        sync_task = asyncio.create_task(_immich_sync_loop())
    else:
        log.info("Immich auto-import disabled (set IMMICH_URL + IMMICH_API_KEY to enable)")

    # Daily retry worker for photos whose identify call hit a transient
    # error (Claude rate limit, n8n 5xx, low-confidence batch result, etc.).
    from .identify_retry import worker_loop
    retry_task = asyncio.create_task(worker_loop())
    log.info(
        "Identify retry worker started: max %d attempts, %dh between tries",
        settings.identify_retry_max_attempts,
        settings.identify_retry_interval_hours,
    )

    # Anthropic Batches workers (auto-sync only): one submits batches, the
    # other polls in-flight ones. Both no-op if ANTHROPIC_API_KEY is unset.
    batch_submit_task: asyncio.Task | None = None
    batch_poll_task: asyncio.Task | None = None
    from .identify_batch import enabled as batch_enabled, submit_loop, poll_loop
    if batch_enabled():
        batch_submit_task = asyncio.create_task(submit_loop())
        batch_poll_task = asyncio.create_task(poll_loop())
        log.info(
            "Anthropic Batches enabled: submit every %dm, poll every %dm, %s model",
            settings.batch_submit_interval_minutes,
            settings.batch_poll_interval_minutes,
            settings.anthropic_batch_model,
        )
    else:
        log.info("Anthropic Batches disabled (ANTHROPIC_API_KEY blank) — auto-sync uses live n8n webhook")

    try:
        yield
    finally:
        for t in (sync_task, retry_task, batch_submit_task, batch_poll_task):
            if t is not None:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass


app = FastAPI(title="Augie's Plant Tracker", lifespan=lifespan)

# Static assets (CSS, etc.) bundled with the app.
app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")
# Cache-bust static assets on every restart so browsers don't serve stale
# styles.css / justified-grid.js after a deploy.
import time as _time
templates.env.globals["asset_version"] = str(int(_time.time()))
ui.templates = templates  # share the instance

app.include_router(ui.router)
app.include_router(plants.router, prefix="/api/plants", tags=["plants"])
app.include_router(photos.router, prefix="/api/photos", tags=["photos"])
app.include_router(sync.router, prefix="/api/sync", tags=["sync"])


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

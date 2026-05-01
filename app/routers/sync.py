"""Manual sync trigger — fires the existing sync routine on demand."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter

from .. import sync as sync_module

log = logging.getLogger(__name__)
router = APIRouter()

_running_task: asyncio.Task | None = None


@router.post("")
@router.post("/")
async def trigger_sync() -> dict:
    """Kick off a forced sync in the background. Returns immediately."""
    global _running_task
    if _running_task is not None and not _running_task.done():
        return {"ok": True, "status": "already_running"}
    _running_task = asyncio.create_task(_run_logged())
    return {"ok": True, "status": "started"}


async def _run_logged() -> None:
    try:
        stats = await sync_module.run_sync(force=True)
        log.info("manual sync complete: %s", stats)
    except Exception:
        log.exception("manual sync failed")

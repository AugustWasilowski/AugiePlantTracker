"""Background worker that retries plant-identification calls that earlier
failed transiently (Claude rate-limited, overloaded, n8n 5xx, etc.).

Pipeline view:
  sync.py (or any other call site) runs ``n8n.identify()`` once. If it
  comes back with ``status == "retry"``, the caller stamps the Photo row
  as ``identify_state="pending"`` with ``identify_next_attempt_at`` set
  to ~24h out. This module's worker wakes up periodically, scans for
  rows whose retry time has arrived, and reruns ``n8n.identify()`` on the
  image already saved to disk. Successful retries write the identification
  back to the row and clear the queue flags. If a row exceeds
  ``IDENTIFY_RETRY_MAX_ATTEMPTS``, it's marked ``exhausted`` and we ping
  Telegram so the user knows.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx
from sqlalchemy import select

from . import n8n, storage
from .config import settings
from .database import session_scope
from .models import Photo

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


def mark_pending(
    photo_id: int,
    error: str,
    *,
    delay_hours: Optional[int] = None,
) -> None:
    """Record a transient identify failure on the Photo row.

    Called from ``sync.py`` (and anywhere else that calls ``n8n.identify``)
    when the outcome is ``status == "retry"``. Bumps the attempt counter
    and schedules the next try. If we're already at the max attempt count,
    flips the row to ``exhausted`` and lets the worker send the alert on
    its next pass.
    """
    delay = timedelta(
        hours=delay_hours if delay_hours is not None else settings.identify_retry_interval_hours
    )
    with session_scope() as sess:
        photo = sess.get(Photo, photo_id)
        if photo is None:
            return
        attempts = (photo.identify_attempts or 0) + 1
        photo.identify_attempts = attempts
        photo.identify_last_attempt_at = _now()
        photo.identify_last_error = error[:64] if error else None
        if attempts >= settings.identify_retry_max_attempts:
            photo.identify_state = "exhausted"
            photo.identify_next_attempt_at = None
            log.warning(
                "Photo %s: identify exhausted after %d attempts (last error: %s)",
                photo_id, attempts, error,
            )
        else:
            photo.identify_state = "pending"
            photo.identify_next_attempt_at = _now() + delay
            log.info(
                "Photo %s: identify queued for retry (#%d) at %s (error: %s)",
                photo_id, attempts, photo.identify_next_attempt_at.isoformat(), error,
            )


def _apply_success(photo: Photo, result: n8n.IdentifyResult) -> None:
    """Copy a successful identify result onto the Photo row."""
    growth = result.growth
    photo.identified_species = result.species
    photo.identified_common_name = result.common_name
    photo.identified_confidence = result.confidence
    photo.identified_raw = json.dumps(dict(result))
    if growth.get("height_cm") is not None:
        photo.measured_height_cm = growth.get("height_cm")
    if growth.get("leaf_count") is not None:
        photo.measured_leaf_count = growth.get("leaf_count")
    photo.identify_state = None
    photo.identify_next_attempt_at = None
    photo.identify_last_error = None


async def _retry_one(photo_id: int) -> None:
    """Look up the photo, retry identify, and update the queue state."""
    # Fetch path + current attempt count without holding the session open
    # across the network call.
    with session_scope() as sess:
        photo = sess.get(Photo, photo_id)
        if photo is None:
            return
        filename = photo.filename

    image_path = storage.absolute(filename)
    if not image_path.exists():
        log.warning("Photo %s: file %s missing — cannot retry", photo_id, image_path)
        # Mark as exhausted so we don't keep banging on a missing file.
        with session_scope() as sess:
            photo = sess.get(Photo, photo_id)
            if photo:
                photo.identify_state = "exhausted"
                photo.identify_last_error = "file_missing"
                photo.identify_next_attempt_at = None
        return

    outcome = await n8n.identify(image_path)
    if outcome.status == "ok" and outcome.result is not None:
        with session_scope() as sess:
            photo = sess.get(Photo, photo_id)
            if photo is not None:
                _apply_success(photo, outcome.result)
        log.info("Photo %s: identify succeeded on retry", photo_id)
    elif outcome.status == "retry":
        mark_pending(photo_id, outcome.error or "unknown")
    elif outcome.status == "permanent":
        # Permanent errors shouldn't loop forever either.
        with session_scope() as sess:
            photo = sess.get(Photo, photo_id)
            if photo is not None:
                photo.identify_state = "exhausted"
                photo.identify_last_error = (outcome.error or "permanent")[:64]
                photo.identify_next_attempt_at = None
                photo.identify_last_attempt_at = _now()
        log.warning("Photo %s: identify permanent error (%s)", photo_id, outcome.error)
    else:  # "skipped" — identify disabled. Leave the row pending for when it's re-enabled.
        log.info("Photo %s: identify skipped (disabled); leaving queued", photo_id)


async def _send_telegram(text: str) -> bool:
    """Notify the user via the n8n Claude→Telegram webhook. Best-effort."""
    if not settings.n8n_telegram_webhook_url or not settings.n8n_telegram_webhook_secret:
        log.warning("Telegram webhook not configured; would have sent: %s", text)
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                settings.n8n_telegram_webhook_url,
                json={"text": text},
                headers={
                    "X-Webhook-Secret": settings.n8n_telegram_webhook_secret,
                    # Cloudflare WAF rule 1010 has historically 403'd default
                    # python User-Agents on this hostname — set a friendly UA.
                    "User-Agent": "plant-tracker/1.0",
                },
            )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("Telegram notify failed: %s", e)
        return False
    return True


async def _send_announce(text: str) -> bool:
    """Optional Piper-TTS / StackChan announcement. Best-effort, fire-and-forget.

    The Announce Home workflow responds immediately and runs ~12s of TTS +
    choreography asynchronously, so we don't wait around for the whole thing.
    """
    if not settings.n8n_announce_webhook_url:
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                settings.n8n_announce_webhook_url,
                json={"text": text},
                headers={"User-Agent": "plant-tracker/1.0"},
            )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("Announce-home notify failed: %s", e)
        return False
    return True


async def _notify_exhausted(photo_ids: list[int]) -> None:
    """Send one summarised Telegram message for a batch of exhausted photos.

    After sending we stamp ``identify_last_error`` with a 'notified' marker
    so the same row doesn't get re-announced on every worker tick.
    """
    if not photo_ids:
        return
    with session_scope() as sess:
        rows = sess.scalars(
            select(Photo).where(Photo.id.in_(photo_ids))
        ).all()
        details = [(p.id, p.identify_attempts, p.identify_last_error) for p in rows]

    n = len(details)
    bullet_lines = "\n".join(
        f"• Photo #{pid} — {attempts} tries, last error: `{err or 'unknown'}`"
        for pid, attempts, err in details[:5]
    )
    if n > 5:
        bullet_lines += f"\n• …and {n - 5} more"
    text = (
        f"🌱 *Plant Tracker:* identification gave up on *{n}* photo{'s' if n != 1 else ''} "
        f"after {settings.identify_retry_max_attempts} retries.\n\n"
        f"{bullet_lines}\n\n"
        f"They're sitting in the inbox without identification. "
        f"Likely cause: Claude API rate-limited or overloaded for a sustained window."
    )
    # Short, spoken-friendly version for Piper TTS. Keep it under one breath.
    spoken = (
        f"Plant tracker update. I gave up on {n} "
        f"{'photo' if n == 1 else 'photos'} after {settings.identify_retry_max_attempts} tries. "
        f"Claude is having a rough day."
    )
    # Fire both notifications concurrently — Piper is best-effort.
    sent, _announced = await asyncio.gather(
        _send_telegram(text),
        _send_announce(spoken),
        return_exceptions=False,
    )
    if sent:
        with session_scope() as sess:
            for pid in photo_ids:
                photo = sess.get(Photo, pid)
                if photo is not None and photo.identify_state == "exhausted":
                    # Suffix so the row remains 'exhausted' but we won't
                    # re-announce it.
                    err = (photo.identify_last_error or "exhausted")[:50]
                    photo.identify_last_error = f"{err}|notified"[:64]


async def run_once() -> dict:
    """Single pass: retry due photos, then notify on newly-exhausted ones.

    Returns a stats dict useful for tests / manual triggers.
    """
    now = _now()

    # 1. Retry every pending photo whose next-attempt time has arrived.
    with session_scope() as sess:
        due_ids = sess.scalars(
            select(Photo.id).where(
                Photo.identify_state == "pending",
                Photo.identify_next_attempt_at.is_not(None),
                Photo.identify_next_attempt_at <= now,
            )
        ).all()

    retried = 0
    for pid in due_ids:
        try:
            await _retry_one(pid)
            retried += 1
        except Exception:
            log.exception("Photo %s: retry crashed", pid)

    # 2. Notify on exhausted photos we haven't notified about yet.
    with session_scope() as sess:
        to_notify = sess.scalars(
            select(Photo.id).where(
                Photo.identify_state == "exhausted",
                # The "|notified" suffix is our 'already announced' sentinel;
                # SQLAlchemy 2.x doesn't expose LIKE NOT cleanly across all
                # SQLite versions through Mapped[], so do the filter in Python.
            )
        ).all()
    fresh: list[int] = []
    with session_scope() as sess:
        for pid in to_notify:
            p = sess.get(Photo, pid)
            if p and "|notified" not in (p.identify_last_error or ""):
                fresh.append(pid)
    if fresh:
        await _notify_exhausted(fresh)

    return {"due": len(due_ids), "retried": retried, "exhausted_notified": len(fresh)}


async def worker_loop() -> None:
    """Long-running background coroutine started from main.py lifespan."""
    interval = max(60, settings.identify_retry_worker_minutes * 60)
    # Stagger the first tick so we don't fight the immich sync loop on boot.
    await asyncio.sleep(30)
    while True:
        try:
            stats = await run_once()
            if stats["due"] or stats["exhausted_notified"]:
                log.info("identify retry pass: %s", stats)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("identify retry pass crashed (will retry)")
        await asyncio.sleep(interval)

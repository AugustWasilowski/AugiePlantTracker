"""Anthropic Message Batches for Immich auto-sync plant identification.

Why this exists:
  The Immich auto-sync downloads photos in bursts (one trip to mom's garden =
  20 photos uploaded back-to-back). Identifying them via the live n8n webhook
  works but burns 100% Sonnet+Haiku rates. Anthropic's Batches API gives a
  flat 50% discount on input + output tokens in exchange for up-to-24h
  delivery (usually << 1h). The user doesn't care if a photo in the inbox is
  labeled "Monstera deliciosa" now or 40 minutes from now — they're going to
  open the inbox tomorrow morning anyway.

State machine on Photo.identify_state:
    NULL       — happy path (identified_raw populated) or never tried
    'batched'  — auto-sync inserted it, awaiting batch submission/result
    'pending'  — live-retry queue (handled by app.identify_retry)
    'exhausted'— gave up after IDENTIFY_RETRY_MAX_ATTEMPTS

Within 'batched':
    identify_batch_id = NULL              — waiting for next submission tick
    identify_batch_id = 'msgbatch_…'      — submitted, waiting for results

Two ticks:
    submit_tick: every BATCH_SUBMIT_INTERVAL_MINUTES
        - find 'batched' rows with no batch_id, up to BATCH_MAX_PHOTOS_PER_SUBMISSION
        - encode each image as base64, POST to /v1/messages/batches
        - stamp the returned batch_id on each row
    poll_tick: every BATCH_POLL_INTERVAL_MINUTES
        - SELECT DISTINCT batch_id WHERE state='batched' AND batch_id IS NOT NULL
        - GET each batch's status; when 'ended', stream results
        - apply: confidence >= 0.75 → identified, else 'pending' for live escalation
        - submitted_at > BATCH_MAX_WAIT_HOURS ago → bail to 'pending'
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import datetime, timedelta
from typing import Iterable, Optional

import httpx
from sqlalchemy import select

from . import storage
from .config import settings
from .database import session_scope
from .identify_retry import _apply_success  # success-path schema is identical
from .models import Photo
from .n8n import IdentifyResult

log = logging.getLogger(__name__)

_ANTHROPIC_BASE = "https://api.anthropic.com/v1"
_ANTHROPIC_VERSION = "2023-06-01"

# Confidence floor for accepting a Haiku-via-batch result outright.
# Below this, the photo gets bumped to the live 'pending' queue so the n8n
# webhook (now two-tier) can escalate to Sonnet.
_BATCH_ACCEPT_THRESHOLD = 0.75

# Same prompt as the n8n Haiku node so behaviour matches the live path.
_HAIKU_PROMPT = """You are identifying a houseplant from a photo. Respond with ONLY a JSON object (no prose, no markdown fences) with this exact shape:
{
  "species": "<scientific name or null>",
  "common_name": "<common name or null>",
  "confidence": <0..1>,
  "care_notes": "<2-3 sentence care summary: light, water, humidity>",
  "growth": { "height_cm": <number or null>, "leaf_count": <integer or null> }
}
If you cannot tell, set fields to null and confidence to 0."""


def _now() -> datetime:
    return datetime.utcnow()


def _api_headers() -> dict:
    return {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
    }


def _strip_md_fences(raw: str) -> str:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        # drop opening fence (```json or ```)
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return cleaned.strip()


def _parse_claude_reply(content: list) -> tuple[dict, Optional[str]]:
    """Pull JSON identification from a Claude `content` array. Mirrors the
    n8n Parse Haiku JSON code node. Returns (parsed_dict, error_or_none)."""
    raw_text = ""
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            raw_text = block.get("text", "")
            break
    cleaned = _strip_md_fences(str(raw_text))
    try:
        return json.loads(cleaned), None
    except json.JSONDecodeError as e:
        return ({
            "species": None,
            "common_name": None,
            "confidence": 0,
            "care_notes": None,
            "growth": {"height_cm": None, "leaf_count": None},
            "_raw": raw_text,
        }, f"parse:{e.__class__.__name__}")


def _image_to_b64(path) -> Optional[str]:
    try:
        with open(path, "rb") as fh:
            return base64.b64encode(fh.read()).decode("ascii")
    except OSError as e:
        log.warning("Could not read %s for batch encode: %s", path, e)
        return None


def _build_request_for_photo(photo_id: int, b64: str) -> dict:
    """One entry in the batch `requests` array. custom_id is how we map the
    batch result back to the Photo row — Anthropic preserves it 1:1."""
    return {
        "custom_id": f"photo-{photo_id}",
        "params": {
            "model": settings.anthropic_batch_model,
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": _HAIKU_PROMPT},
                    ],
                }
            ],
        },
    }


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------


def _pending_for_submission(limit: int) -> list[tuple[int, str]]:
    """Photos waiting to be batched: 'batched' state with no batch_id yet."""
    with session_scope() as sess:
        rows = sess.scalars(
            select(Photo)
            .where(Photo.identify_state == "batched")
            .where(Photo.identify_batch_id.is_(None))
            .order_by(Photo.id)
            .limit(limit)
        ).all()
        return [(p.id, p.filename) for p in rows]


async def submit_pending() -> dict:
    """Build and submit one batch, if there's anything to send."""
    if not settings.anthropic_api_key:
        return {"submitted": 0, "skipped": "no_api_key"}

    candidates = _pending_for_submission(settings.batch_max_photos_per_submission)
    if not candidates:
        return {"submitted": 0}

    requests: list[dict] = []
    request_photo_ids: list[int] = []
    for photo_id, filename in candidates:
        b64 = _image_to_b64(storage.absolute(filename))
        if b64 is None:
            # Image gone or unreadable — drop straight into live retry path.
            with session_scope() as sess:
                photo = sess.get(Photo, photo_id)
                if photo is not None:
                    photo.identify_state = "pending"
                    photo.identify_next_attempt_at = _now()
                    photo.identify_last_error = "batch_encode_fail"
            continue
        requests.append(_build_request_for_photo(photo_id, b64))
        request_photo_ids.append(photo_id)

    if not requests:
        return {"submitted": 0, "skipped": "no_readable_images"}

    body = {"requests": requests}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{_ANTHROPIC_BASE}/messages/batches",
                json=body,
                headers=_api_headers(),
            )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        # If submission fails (rate-limit, auth, network), leave the photos
        # in 'batched' state with no batch_id so the next tick retries.
        log.warning("Batch submission failed (%d photos): %s", len(requests), e)
        return {"submitted": 0, "error": str(e)[:120]}

    data = resp.json()
    batch_id = data.get("id")
    if not batch_id:
        log.warning("Batch submission returned no id: %r", data)
        return {"submitted": 0, "error": "no_batch_id"}

    submitted_at = _now()
    with session_scope() as sess:
        for pid in request_photo_ids:
            photo = sess.get(Photo, pid)
            if photo is not None:
                photo.identify_batch_id = batch_id
                photo.identify_batch_submitted_at = submitted_at
                photo.identify_last_error = None
    log.info("Submitted batch %s with %d photos", batch_id, len(request_photo_ids))
    return {"submitted": len(request_photo_ids), "batch_id": batch_id}


# ---------------------------------------------------------------------------
# Polling + result application
# ---------------------------------------------------------------------------


def _active_batch_ids() -> list[str]:
    with session_scope() as sess:
        rows = sess.scalars(
            select(Photo.identify_batch_id)
            .where(Photo.identify_state == "batched")
            .where(Photo.identify_batch_id.is_not(None))
            .distinct()
        ).all()
    return [b for b in rows if b]


def _photo_ids_for_batch(batch_id: str) -> list[int]:
    with session_scope() as sess:
        return list(
            sess.scalars(
                select(Photo.id).where(Photo.identify_batch_id == batch_id)
            ).all()
        )


async def _fetch_batch_status(client: httpx.AsyncClient, batch_id: str) -> Optional[dict]:
    try:
        resp = await client.get(
            f"{_ANTHROPIC_BASE}/messages/batches/{batch_id}",
            headers=_api_headers(),
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        log.warning("Batch %s status fetch failed: %s", batch_id, e)
        return None


async def _fetch_batch_results(client: httpx.AsyncClient, results_url: str) -> list[dict]:
    """Anthropic returns a JSONL stream. Parse it line by line."""
    try:
        resp = await client.get(results_url, headers=_api_headers())
        resp.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("Batch results fetch failed (%s): %s", results_url, e)
        return []
    out: list[dict] = []
    for line in resp.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("Skipping bad JSONL line in batch result")
    return out


def _apply_batch_result(entry: dict) -> str:
    """Apply one JSONL result entry to its Photo. Returns a short tag for stats.
    Entry shape: {"custom_id": "photo-123", "result": {"type": "succeeded", "message": {...}}}.
    """
    cid = entry.get("custom_id", "")
    if not cid.startswith("photo-"):
        return "bad_custom_id"
    try:
        photo_id = int(cid.removeprefix("photo-"))
    except ValueError:
        return "bad_custom_id"

    result = entry.get("result") or {}
    rtype = result.get("type")

    if rtype != "succeeded":
        # 'errored', 'canceled', 'expired' — kick to live retry path.
        err_kind = result.get("error", {}).get("type") if isinstance(result.get("error"), dict) else rtype
        with session_scope() as sess:
            photo = sess.get(Photo, photo_id)
            if photo is not None and photo.identify_state == "batched":
                photo.identify_state = "pending"
                photo.identify_next_attempt_at = _now()
                photo.identify_batch_id = None
                photo.identify_last_error = f"batch_{err_kind}"[:64]
        return f"failed:{err_kind}"

    message = result.get("message") or {}
    content = message.get("content") or []
    parsed, parse_err = _parse_claude_reply(content)

    if parse_err:
        with session_scope() as sess:
            photo = sess.get(Photo, photo_id)
            if photo is not None and photo.identify_state == "batched":
                photo.identify_state = "pending"
                photo.identify_next_attempt_at = _now()
                photo.identify_batch_id = None
                photo.identify_last_error = "batch_parse_error"
        return "parse_error"

    confidence = parsed.get("confidence") or 0
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.0

    if confidence < _BATCH_ACCEPT_THRESHOLD:
        # Bump to live retry so the n8n two-tier workflow can run Sonnet.
        with session_scope() as sess:
            photo = sess.get(Photo, photo_id)
            if photo is not None and photo.identify_state == "batched":
                photo.identify_state = "pending"
                photo.identify_next_attempt_at = _now()
                photo.identify_batch_id = None
                photo.identify_last_error = f"batch_lowconf_{confidence:.2f}"[:64]
        return "low_confidence"

    # Accepted — apply identification + clear all queue flags.
    with session_scope() as sess:
        photo = sess.get(Photo, photo_id)
        if photo is None:
            return "photo_gone"
        _apply_success(photo, IdentifyResult({**parsed, "_tier": "batch_haiku"}))
        photo.identify_batch_id = None
        photo.identify_batch_submitted_at = None
    return "ok"


async def poll_active() -> dict:
    """Check each in-flight batch; apply results when ready; expire stale ones."""
    if not settings.anthropic_api_key:
        return {"checked": 0, "skipped": "no_api_key"}

    batch_ids = _active_batch_ids()
    if not batch_ids:
        return {"checked": 0}

    stats = {"checked": len(batch_ids), "completed": 0, "still_in_progress": 0, "expired": 0, "results_applied": 0}
    cutoff = _now() - timedelta(hours=settings.batch_max_wait_hours)

    async with httpx.AsyncClient(timeout=60.0) as client:
        for batch_id in batch_ids:
            status = await _fetch_batch_status(client, batch_id)
            if status is None:
                # Network blip — try again next tick.
                continue

            processing_status = status.get("processing_status")
            results_url = status.get("results_url")

            if processing_status == "ended" and results_url:
                results = await _fetch_batch_results(client, results_url)
                applied = 0
                tag_counts: dict[str, int] = {}
                for entry in results:
                    tag = _apply_batch_result(entry)
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
                    if tag == "ok":
                        applied += 1
                stats["completed"] += 1
                stats["results_applied"] += applied
                log.info("Batch %s ended: %s", batch_id, tag_counts)
                continue

            # Still in progress — but is it stuck past our patience limit?
            stats["still_in_progress"] += 1
            with session_scope() as sess:
                stuck_rows = sess.scalars(
                    select(Photo)
                    .where(Photo.identify_batch_id == batch_id)
                    .where(Photo.identify_batch_submitted_at.is_not(None))
                    .where(Photo.identify_batch_submitted_at < cutoff)
                ).all()
                if stuck_rows:
                    stats["expired"] += len(stuck_rows)
                    for photo in stuck_rows:
                        photo.identify_state = "pending"
                        photo.identify_next_attempt_at = _now()
                        photo.identify_batch_id = None
                        photo.identify_last_error = "batch_timeout"
                    log.warning(
                        "Batch %s still in_progress after %dh — bumping %d photos to live retry",
                        batch_id, settings.batch_max_wait_hours, len(stuck_rows),
                    )

    return stats


# ---------------------------------------------------------------------------
# Loops
# ---------------------------------------------------------------------------


async def submit_loop() -> None:
    interval = max(60, settings.batch_submit_interval_minutes * 60)
    await asyncio.sleep(45)  # let app + retry workers warm up first
    while True:
        try:
            stats = await submit_pending()
            if stats.get("submitted"):
                log.info("Batch submit tick: %s", stats)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Batch submit tick crashed (will retry)")
        await asyncio.sleep(interval)


async def poll_loop() -> None:
    interval = max(30, settings.batch_poll_interval_minutes * 60)
    await asyncio.sleep(90)
    while True:
        try:
            stats = await poll_active()
            if stats.get("checked"):
                log.info("Batch poll tick: %s", stats)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Batch poll tick crashed (will retry)")
        await asyncio.sleep(interval)


def enabled() -> bool:
    return bool(settings.anthropic_api_key)

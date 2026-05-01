"""Auto-import worker — pulls plant candidates from Immich into the inbox.

Pipeline:
  1. Fan out CLIP queries against Immich smart search, dedupe by asset id.
  2. Skip assets we've already imported (Photo.immich_asset_id is the key).
  3. Enrich each remaining asset with GET /api/assets/{id} to get GPS + timestamps.
  4. Drop anything outside HOME_RADIUS_KM of HOME_LAT/HOME_LON, or with no GPS.
  5. Download the original, save through app.storage, thumbnail through app.imaging.
  6. Identify via the existing app.n8n.identify (which handles compression).
  7. Insert as Photo(plant_id=None, immich_asset_id=...) — appears in the inbox.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import select

from . import imaging, n8n, storage
from .config import settings
from .database import session_scope
from .immich import ImmichClient
from .models import Photo

log = logging.getLogger(__name__)

_last_scan_at: Optional[datetime] = None
_sync_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _parse_iso(raw) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _to_naive_utc(dt: datetime) -> datetime:
    """Existing Photo.captured_at is timezone-naive — normalise before storing."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _save_immich_image(image_bytes: bytes, original_filename: str) -> Path:
    """Adapter so we can reuse storage.save_upload (which expects a stream)."""
    return storage.save_upload(io.BytesIO(image_bytes), original_filename)


def _make_thumb_path(photo_path: Path) -> Path:
    rel = photo_path.relative_to(settings.photos_dir)
    return settings.thumbs_dir / rel.with_suffix(".jpg")


def _known_immich_ids() -> set[str]:
    """Asset ids already imported (any Photo with immich_asset_id set)."""
    with session_scope() as sess:
        rows = sess.scalars(
            select(Photo.immich_asset_id).where(Photo.immich_asset_id.is_not(None))
        ).all()
    return set(rows)


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------


async def run_sync(force: bool = False) -> dict:
    """One sync pass against Immich. Returns a stats dict.

    Cheap-path: if `force` is False and we've scanned at least once, ask
    Immich whether *any* asset has been uploaded since our last scan. If
    none, skip the expensive CLIP fan-out entirely.
    """
    global _last_scan_at

    if not settings.immich_url:
        raise RuntimeError("IMMICH_URL not configured.")
    if not settings.immich_api_key:
        raise RuntimeError("IMMICH_API_KEY not configured.")

    async with _sync_lock:
        scan_started_at = datetime.now(timezone.utc)

        if not force and _last_scan_at is not None:
            try:
                async with ImmichClient(settings.immich_url, settings.immich_api_key) as probe:
                    if not await probe.has_assets_after(_last_scan_at):
                        log.info(
                            "immich delta: nothing new since %s — skipping full sync",
                            _last_scan_at.isoformat(),
                        )
                        _last_scan_at = scan_started_at
                        return {
                            "queries": 0,
                            "raw_results": 0,
                            "unique_candidates": 0,
                            "already_known": 0,
                            "no_gps": 0,
                            "out_of_radius": 0,
                            "errors": 0,
                            "inserted": 0,
                            "skipped": "no_new_assets",
                        }
            except Exception as e:
                log.warning("immich delta check failed (%s) — running full sync", e)

        stats = await _run_full_sync()
        _last_scan_at = scan_started_at
        return stats


async def _run_full_sync() -> dict:
    """The original CLIP fan-out + geofence + import pass."""
    seen: dict[str, str] = {}  # asset_id -> first matching CLIP query
    raw_count = 0
    inserted = 0
    skipped_known = 0
    skipped_no_gps = 0
    skipped_geofence = 0
    errors = 0

    async with ImmichClient(settings.immich_url, settings.immich_api_key) as client:
        # 1. Fan out CLIP queries.
        for q in settings.clip_queries:
            try:
                results = await client.smart_search(q, settings.clip_results_per_query)
            except Exception as e:
                log.warning("immich smart_search(%r) failed: %s", q, e)
                errors += 1
                continue
            raw_count += len(results)
            for asset in results:
                aid = asset.get("id")
                if aid and aid not in seen:
                    seen[aid] = q
            log.info("immich query %r -> %d results", q, len(results))

        log.info(
            "immich: %d unique candidates across %d queries",
            len(seen),
            len(settings.clip_queries),
        )

        # 2. Dedup against already-imported photos.
        already_known = _known_immich_ids()
        new_ids = [aid for aid in seen if aid not in already_known]
        skipped_known = len(seen) - len(new_ids)

        if not new_ids:
            log.info("immich: nothing new to import")
            return {
                "queries": len(settings.clip_queries),
                "raw_results": raw_count,
                "unique_candidates": len(seen),
                "already_known": skipped_known,
                "no_gps": 0,
                "out_of_radius": 0,
                "errors": errors,
                "inserted": 0,
            }

        # 3. Enrich, geofence, download, save, identify, insert.
        for aid in new_ids:
            try:
                full = await client.get_asset(aid)
            except Exception as e:
                log.warning("immich get_asset(%s) failed: %s", aid, e)
                errors += 1
                continue

            exif = full.get("exifInfo") or {}
            lat = exif.get("latitude")
            lon = exif.get("longitude")
            if lat is None or lon is None:
                skipped_no_gps += 1
                continue
            matched_location: Optional[str] = None
            for fence in settings.all_geofences:
                if haversine_km(fence["lat"], fence["lon"], lat, lon) <= fence["radius_km"]:
                    matched_location = fence["name"]
                    break
            if matched_location is None:
                skipped_geofence += 1
                continue

            try:
                image_bytes = await client.download_original(aid)
            except Exception as e:
                log.warning("immich download_original(%s) failed: %s", aid, e)
                errors += 1
                continue

            original_filename = full.get("originalFileName") or f"{aid}.jpg"
            try:
                saved_path = _save_immich_image(image_bytes, original_filename)
            except Exception as e:
                log.warning("save failed for %s (%s): %s", aid, original_filename, e)
                errors += 1
                continue

            # Thumbnail.
            thumb_path = _make_thumb_path(saved_path)
            try:
                width, height = imaging.make_thumbnail(saved_path, thumb_path)
            except Exception as e:
                log.warning("thumbnail failed for %s: %s", saved_path, e)
                width = height = None

            # Captured timestamp: prefer EXIF on the actual file, fall back to
            # Immich's own metadata, then fileCreatedAt, then now.
            captured_at = (
                imaging.extract_captured_at(saved_path)
                or _parse_iso(exif.get("dateTimeOriginal"))
                or _parse_iso(full.get("fileCreatedAt"))
                or datetime.utcnow()
            )
            captured_at = _to_naive_utc(captured_at)

            # Identify via the existing async n8n module (it handles compression
            # and parses the response shape into IdentifyResult).
            try:
                ident = await n8n.identify(saved_path)
            except Exception as e:
                log.warning("identify failed for %s: %s", aid, e)
                ident = None

            growth = ident.growth if ident else {}

            photo = Photo(
                plant_id=None,
                filename=storage.relative(saved_path),
                thumb_filename=storage.relative(thumb_path) if thumb_path.exists() else None,
                captured_at=captured_at,
                width=width,
                height=height,
                identified_species=(ident.species if ident else None),
                identified_common_name=(ident.common_name if ident else None),
                identified_confidence=(ident.confidence if ident else None),
                identified_raw=json.dumps(dict(ident)) if ident else None,
                measured_height_cm=growth.get("height_cm"),
                measured_leaf_count=growth.get("leaf_count"),
                immich_asset_id=aid,
                imported_location=matched_location,
            )
            with session_scope() as sess:
                sess.add(photo)
            inserted += 1
            log.info("immich import: asset %s -> %s", aid, original_filename)

    stats = {
        "queries": len(settings.clip_queries),
        "raw_results": raw_count,
        "unique_candidates": len(seen),
        "already_known": skipped_known,
        "no_gps": skipped_no_gps,
        "out_of_radius": skipped_geofence,
        "errors": errors,
        "inserted": inserted,
    }
    log.info("immich sync complete: %s", stats)
    return stats

"""Photo upload + identification."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from .. import imaging, n8n, storage
from ..config import settings
from ..database import get_session
from ..models import Photo, Plant

log = logging.getLogger(__name__)
router = APIRouter()


def _make_thumb_path(photo_path: Path) -> Path:
    rel = photo_path.relative_to(settings.photos_dir)
    return settings.thumbs_dir / rel.with_suffix(".jpg")


@router.post("/upload")
async def upload_photo(
    file: UploadFile = File(...),
    plant_id: Optional[int] = Form(None),
    db: Session = Depends(get_session),
):
    """Accept an image upload, persist it, optionally call n8n for ID, return record."""
    if not file.filename:
        raise HTTPException(400, "Missing filename")

    saved = storage.save_upload(file.file, file.filename)

    thumb_path = _make_thumb_path(saved)
    try:
        width, height = imaging.make_thumbnail(saved, thumb_path)
    except Exception as e:
        log.warning("Thumbnail failed for %s: %s", saved, e)
        width = height = None

    captured_at = imaging.extract_captured_at(saved) or datetime.utcnow()

    ident = await n8n.identify(saved)

    plant: Optional[Plant] = None
    if plant_id is not None:
        plant = db.get(Plant, plant_id)
        if plant is None:
            raise HTTPException(400, f"Unknown plant_id {plant_id}")

    growth = ident.growth if ident else {}
    photo = Photo(
        plant_id=plant_id,
        filename=storage.relative(saved),
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
    )
    db.add(photo)
    db.commit()
    db.refresh(photo)

    if plant is not None and ident is not None:
        if not plant.species and ident.species:
            plant.species = ident.species
        if not plant.common_name and ident.common_name:
            plant.common_name = ident.common_name
        if not plant.care_notes and ident.care_notes:
            plant.care_notes = ident.care_notes
        db.commit()

    return {
        "id": photo.id,
        "plant_id": photo.plant_id,
        "filename": photo.filename,
        "thumb_filename": photo.thumb_filename,
        "captured_at": photo.captured_at.isoformat(),
        "identified": {
            "species": photo.identified_species,
            "common_name": photo.identified_common_name,
            "confidence": photo.identified_confidence,
            "care_notes": ident.care_notes if ident else None,
        },
    }


@router.post("/{photo_id}/assign")
def assign_photo(photo_id: int, plant_id: int = Form(...), db: Session = Depends(get_session)):
    photo = db.get(Photo, photo_id)
    if photo is None:
        raise HTTPException(404, "Photo not found")
    plant = db.get(Plant, plant_id)
    if plant is None:
        raise HTTPException(400, "Unknown plant")
    photo.plant_id = plant_id
    db.commit()
    return {"ok": True}


@router.post("/{photo_id}/measurements")
def update_measurements(
    photo_id: int,
    height_cm: Optional[float] = Form(None),
    leaf_count: Optional[int] = Form(None),
    caption: Optional[str] = Form(None),
    db: Session = Depends(get_session),
):
    photo = db.get(Photo, photo_id)
    if photo is None:
        raise HTTPException(404, "Photo not found")
    if height_cm is not None:
        photo.measured_height_cm = height_cm
    if leaf_count is not None:
        photo.measured_leaf_count = leaf_count
    if caption is not None:
        photo.caption = caption
    db.commit()
    return {"ok": True}


@router.delete("/{photo_id}")
def delete_photo(photo_id: int, db: Session = Depends(get_session)) -> dict:
    photo = db.get(Photo, photo_id)
    if photo is None:
        raise HTTPException(404, "Photo not found")
    for rel in (photo.filename, photo.thumb_filename):
        if not rel:
            continue
        try:
            storage.absolute(rel).unlink(missing_ok=True)
        except OSError:
            pass
    db.delete(photo)
    db.commit()
    return {"ok": True}

"""HTML pages — the actual web UI."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_session
from ..models import Photo, Plant

router = APIRouter()
templates: Jinja2Templates  # injected by main.py


def _unassigned_count(db: Session) -> int:
    return db.query(Photo).filter(Photo.plant_id.is_(None)).count()


def _ctx(request: Request, db: Session, **extra) -> dict:
    """Common template context — adds the inbox badge count to every page."""
    return {
        "request": request,
        "unassigned_count": _unassigned_count(db),
        "today_iso": datetime.now().strftime("%Y-%m-%d"),
        **extra,
    }


def _form_to_plant_kwargs(
    nickname: str,
    species: Optional[str],
    common_name: Optional[str],
    location: Optional[str],
    pot_size: Optional[str],
    soil_type: Optional[str],
    acquired_on: Optional[str],
    care_notes: Optional[str],
    notes: Optional[str],
) -> dict:
    acquired_dt: Optional[datetime] = None
    if acquired_on:
        try:
            acquired_dt = datetime.fromisoformat(acquired_on)
        except ValueError:
            acquired_dt = None
    return dict(
        nickname=nickname.strip(),
        species=(species or "").strip() or None,
        common_name=(common_name or "").strip() or None,
        location=(location or "").strip() or None,
        pot_size=(pot_size or "").strip() or None,
        soil_type=(soil_type or "").strip() or None,
        acquired_on=acquired_dt,
        care_notes=(care_notes or "").strip() or None,
        notes=(notes or "").strip() or None,
    )


@router.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_session)) -> HTMLResponse:
    plants = db.query(Plant).order_by(Plant.nickname).all()
    unassigned = (
        db.query(Photo)
        .filter(Photo.plant_id.is_(None))
        .order_by(Photo.uploaded_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        "index.html",
        _ctx(request, db, plants=plants, unassigned=unassigned),
    )


@router.get("/plants/new", response_class=HTMLResponse)
def new_plant_form(
    request: Request,
    photo_id: Optional[int] = None,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    photo = db.get(Photo, photo_id) if photo_id else None

    # Pull care notes from the photo's cached identification result so we can
    # prefill the textarea on the new-plant form.
    prefill_care_notes = ""
    if photo and photo.identified_raw:
        try:
            raw = json.loads(photo.identified_raw)
            if isinstance(raw, dict):
                prefill_care_notes = (raw.get("care_notes") or raw.get("careNotes") or "").strip()
        except (ValueError, TypeError):
            pass

    # Prefer the photo's capture date (EXIF) for "Acquired on" — that's when
    # the user first photographed the plant, the closest proxy we have to
    # when it entered their life. Falls back to today on the template side.
    prefill_acquired_on = photo.captured_at.strftime("%Y-%m-%d") if photo else ""

    return templates.TemplateResponse(
        "edit_plant.html",
        _ctx(
            request, db,
            plant=None,
            action="/plants/new",
            photo=photo,
            prefill_care_notes=prefill_care_notes,
            prefill_acquired_on=prefill_acquired_on,
        ),
    )


@router.post("/plants/new")
def create_plant(
    nickname: str = Form(...),
    species: Optional[str] = Form(None),
    common_name: Optional[str] = Form(None),
    location: Optional[str] = Form(None),
    pot_size: Optional[str] = Form(None),
    soil_type: Optional[str] = Form(None),
    acquired_on: Optional[str] = Form(None),
    care_notes: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    photo_id: Optional[int] = Form(None),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    kwargs = _form_to_plant_kwargs(
        nickname, species, common_name, location, pot_size,
        soil_type, acquired_on, care_notes, notes,
    )
    if not kwargs["nickname"]:
        raise HTTPException(400, "Nickname required")
    plant = Plant(**kwargs)
    db.add(plant)
    db.commit()
    db.refresh(plant)

    if photo_id is not None:
        photo = db.get(Photo, photo_id)
        if photo is not None and photo.plant_id is None:
            photo.plant_id = plant.id
            db.commit()

    return RedirectResponse(f"/plants/{plant.id}", status_code=303)


@router.get("/plants/{plant_id}", response_class=HTMLResponse)
def plant_detail(plant_id: int, request: Request, db: Session = Depends(get_session)) -> HTMLResponse:
    plant = db.get(Plant, plant_id)
    if plant is None:
        raise HTTPException(404, "Plant not found")
    return templates.TemplateResponse(
        "plant.html",
        _ctx(request, db, plant=plant),
    )


@router.get("/plants/{plant_id}/edit", response_class=HTMLResponse)
def edit_plant_form(plant_id: int, request: Request, db: Session = Depends(get_session)) -> HTMLResponse:
    plant = db.get(Plant, plant_id)
    if plant is None:
        raise HTTPException(404, "Plant not found")

    others = db.query(Plant).filter(Plant.id != plant_id).all()
    # Match on species when present, otherwise common_name. Case-insensitive,
    # whitespace-trimmed. Plants that share an identity with this one bubble
    # to the top of the modal so the merge case (same plant, two entries) is
    # one tap away.
    def _id_key(p: Plant) -> str:
        return ((p.species or p.common_name or "").strip().lower())

    self_key = _id_key(plant)
    others.sort(key=lambda p: (
        0 if self_key and _id_key(p) == self_key else 1,
        (p.nickname or "").lower(),
    ))

    return templates.TemplateResponse(
        "edit_plant.html",
        _ctx(
            request, db,
            plant=plant,
            action=f"/plants/{plant_id}/edit",
            other_plants=others,
        ),
    )


@router.post("/photos/{photo_id}/move")
def move_photo(
    photo_id: int,
    target_plant_id: int = Form(...),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    photo = db.get(Photo, photo_id)
    if photo is None:
        raise HTTPException(404, "Photo not found")
    target = db.get(Plant, target_plant_id)
    if target is None:
        raise HTTPException(400, "Unknown target plant")
    source_plant_id = photo.plant_id
    photo.plant_id = target_plant_id
    db.commit()
    # Land the user back on the source edit page so they can keep moving photos
    # (e.g. when merging two split entries). If the photo had no source plant,
    # send them to the destination instead.
    if source_plant_id is not None:
        return RedirectResponse(f"/plants/{source_plant_id}/edit", status_code=303)
    return RedirectResponse(f"/plants/{target_plant_id}", status_code=303)


@router.post("/plants/{plant_id}/edit")
def edit_plant(
    plant_id: int,
    nickname: str = Form(...),
    species: Optional[str] = Form(None),
    common_name: Optional[str] = Form(None),
    location: Optional[str] = Form(None),
    pot_size: Optional[str] = Form(None),
    soil_type: Optional[str] = Form(None),
    acquired_on: Optional[str] = Form(None),
    care_notes: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    plant = db.get(Plant, plant_id)
    if plant is None:
        raise HTTPException(404, "Plant not found")
    kwargs = _form_to_plant_kwargs(
        nickname, species, common_name, location, pot_size,
        soil_type, acquired_on, care_notes, notes,
    )
    for k, v in kwargs.items():
        setattr(plant, k, v)
    db.commit()
    return RedirectResponse(f"/plants/{plant_id}", status_code=303)


@router.post("/plants/{plant_id}/delete")
def delete_plant(plant_id: int, db: Session = Depends(get_session)) -> RedirectResponse:
    plant = db.get(Plant, plant_id)
    if plant is None:
        raise HTTPException(404, "Plant not found")
    db.delete(plant)
    db.commit()
    return RedirectResponse("/", status_code=303)


@router.get("/gallery", response_class=HTMLResponse)
def gallery(
    request: Request,
    sort: str = "newest",
    db: Session = Depends(get_session),
) -> HTMLResponse:
    """Photo grid grouped by location. Click a tile to open the plant."""
    plants = db.query(Plant).all()
    # Only plants with at least one photo — the gallery is photos.
    plants = [p for p in plants if p.latest_photo]

    groups: dict[str, list[Plant]] = {}
    for p in plants:
        loc = (p.location or "").strip() or "Other"
        groups.setdefault(loc, []).append(p)

    for loc in groups:
        if sort == "name":
            groups[loc].sort(key=lambda p: (p.nickname or "").lower())
        else:
            groups[loc].sort(key=lambda p: p.latest_photo.captured_at, reverse=True)

    total = sum(len(v) for v in groups.values())

    return templates.TemplateResponse(
        "gallery.html",
        _ctx(request, db, groups=groups, sort=sort, total=total),
    )


@router.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request, photo_id: Optional[int] = None, db: Session = Depends(get_session)) -> HTMLResponse:
    plants = db.query(Plant).order_by(Plant.nickname).all()
    photo = db.get(Photo, photo_id) if photo_id else None

    # When the photo has an identification, bubble plants of the same species
    # (or same genus) to the top of the match list so the user sees likely
    # candidates before scrolling. The template only renders plants[:4].
    if photo and (photo.identified_species or photo.identified_common_name):
        target_species = (photo.identified_species or "").strip().lower()
        target_common = (photo.identified_common_name or "").strip().lower()
        target_genus = target_species.split(" ", 1)[0] if target_species else ""

        def _rank(p: Plant) -> tuple[int, str]:
            sp = (p.species or "").strip().lower()
            cn = (p.common_name or "").strip().lower()
            genus = sp.split(" ", 1)[0] if sp else ""
            if (target_species and sp == target_species) or (target_common and cn == target_common):
                tier = 0  # exact species or common-name match
            elif target_genus and genus == target_genus:
                tier = 1  # same genus, different species
            else:
                tier = 2
            return (tier, (p.nickname or "").lower())

        plants.sort(key=_rank)

    return templates.TemplateResponse(
        "upload.html",
        _ctx(request, db, plants=plants, photo=photo),
    )

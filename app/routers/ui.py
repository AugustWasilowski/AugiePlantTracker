"""HTML pages — the actual web UI."""
from __future__ import annotations

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
        .limit(12)
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
    return templates.TemplateResponse(
        "edit_plant.html",
        _ctx(request, db, plant=None, action="/plants/new", photo=photo),
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
    return templates.TemplateResponse(
        "edit_plant.html",
        _ctx(request, db, plant=plant, action=f"/plants/{plant_id}/edit"),
    )


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


@router.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request, photo_id: Optional[int] = None, db: Session = Depends(get_session)) -> HTMLResponse:
    plants = db.query(Plant).order_by(Plant.nickname).all()
    photo = db.get(Photo, photo_id) if photo_id else None
    return templates.TemplateResponse(
        "upload.html",
        _ctx(request, db, plants=plants, photo=photo),
    )

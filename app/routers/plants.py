"""JSON API for plant CRUD."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_session
from ..models import Plant

router = APIRouter()


class PlantIn(BaseModel):
    nickname: str
    species: Optional[str] = None
    common_name: Optional[str] = None
    location: Optional[str] = None
    pot_size: Optional[str] = None
    soil_type: Optional[str] = None
    acquired_on: Optional[datetime] = None
    care_notes: Optional[str] = None
    notes: Optional[str] = None


class PlantOut(PlantIn):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


@router.get("", response_model=list[PlantOut])
def list_plants(db: Session = Depends(get_session)) -> list[Plant]:
    return db.query(Plant).order_by(Plant.nickname).all()


@router.post("", response_model=PlantOut)
def create_plant(payload: PlantIn, db: Session = Depends(get_session)) -> Plant:
    plant = Plant(**payload.model_dump())
    db.add(plant)
    db.commit()
    db.refresh(plant)
    return plant


@router.get("/{plant_id}", response_model=PlantOut)
def get_plant(plant_id: int, db: Session = Depends(get_session)) -> Plant:
    plant = db.get(Plant, plant_id)
    if plant is None:
        raise HTTPException(404, "Plant not found")
    return plant


@router.put("/{plant_id}", response_model=PlantOut)
def update_plant(plant_id: int, payload: PlantIn, db: Session = Depends(get_session)) -> Plant:
    plant = db.get(Plant, plant_id)
    if plant is None:
        raise HTTPException(404, "Plant not found")
    for k, v in payload.model_dump().items():
        setattr(plant, k, v)
    db.commit()
    db.refresh(plant)
    return plant


@router.delete("/{plant_id}")
def delete_plant(plant_id: int, db: Session = Depends(get_session)) -> dict:
    plant = db.get(Plant, plant_id)
    if plant is None:
        raise HTTPException(404, "Plant not found")
    db.delete(plant)
    db.commit()
    return {"ok": True}

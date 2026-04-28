"""ORM models."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _now() -> datetime:
    return datetime.utcnow()


class Plant(Base):
    __tablename__ = "plants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nickname: Mapped[str] = mapped_column(String(120), nullable=False)
    species: Mapped[Optional[str]] = mapped_column(String(200))
    common_name: Mapped[Optional[str]] = mapped_column(String(200))
    location: Mapped[Optional[str]] = mapped_column(String(120))
    pot_size: Mapped[Optional[str]] = mapped_column(String(60))
    soil_type: Mapped[Optional[str]] = mapped_column(String(120))
    acquired_on: Mapped[Optional[datetime]] = mapped_column(DateTime)
    care_notes: Mapped[Optional[str]] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now, nullable=False)

    photos: Mapped[list["Photo"]] = relationship(
        back_populates="plant",
        cascade="all, delete-orphan",
        order_by="Photo.captured_at.desc()",
    )

    @property
    def display_name(self) -> str:
        return self.nickname or self.common_name or self.species or f"Plant #{self.id}"

    @property
    def latest_photo(self) -> Optional["Photo"]:
        return self.photos[0] if self.photos else None


class Photo(Base):
    __tablename__ = "photos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plant_id: Mapped[Optional[int]] = mapped_column(ForeignKey("plants.id", ondelete="CASCADE"))
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    thumb_filename: Mapped[Optional[str]] = mapped_column(String(255))
    captured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    width: Mapped[Optional[int]] = mapped_column(Integer)
    height: Mapped[Optional[int]] = mapped_column(Integer)

    # Cached identification result (so we don't re-call n8n)
    identified_species: Mapped[Optional[str]] = mapped_column(String(200))
    identified_common_name: Mapped[Optional[str]] = mapped_column(String(200))
    identified_confidence: Mapped[Optional[float]] = mapped_column(Float)
    identified_raw: Mapped[Optional[str]] = mapped_column(Text)  # raw JSON

    # Optional measurements pulled from the LLM or entered manually.
    measured_height_cm: Mapped[Optional[float]] = mapped_column(Float)
    measured_leaf_count: Mapped[Optional[int]] = mapped_column(Integer)

    caption: Mapped[Optional[str]] = mapped_column(Text)

    plant: Mapped[Optional[Plant]] = relationship(back_populates="photos")

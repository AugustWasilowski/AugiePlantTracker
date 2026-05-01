"""ORM models."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
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
        # Ascending by capture date so the timeline reads left-to-right and
        # the scrubber index 0 is the *first* photo, len-1 is the latest.
        order_by="Photo.captured_at.asc()",
    )

    @property
    def display_name(self) -> str:
        return self.nickname or self.common_name or self.species or f"Plant #{self.id}"

    @property
    def latest_photo(self) -> Optional["Photo"]:
        return self.photos[-1] if self.photos else None

    @property
    def milestones(self) -> list["Photo"]:
        """Photos flagged is_milestone (or with a non-empty caption), newest first.

        Drives the journal feed under the scrubber.
        """
        out = [p for p in self.photos if p.is_milestone or (p.caption or "").strip()]
        out.sort(key=lambda p: p.captured_at, reverse=True)
        return out


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

    # NEW: marks a photo as a journal-worthy moment (repotted, first flower,
    # moved rooms, etc.). Surfaces in the "Journal" feed on the plant detail
    # page even if no caption is set.
    is_milestone: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Set for photos auto-imported from Immich (via app.sync). The original
    # asset id is the dedup key — the sync worker skips any asset whose id is
    # already present here. NULL for manually-uploaded photos.
    immich_asset_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)

    # Geofence the photo's GPS landed inside at import time, e.g. "Home" or
    # "Mom and Dad's House". Used to prefill the Location field when spawning
    # a new plant from an inbox photo. NULL for manual uploads.
    imported_location: Mapped[Optional[str]] = mapped_column(String(120))

    plant: Mapped[Optional[Plant]] = relationship(back_populates="photos")

    @property
    def confidence_word(self) -> str:
        """Plain-English confidence label for the UI."""
        c = self.identified_confidence
        if c is None:
            return ""
        if c >= 0.85:
            return "Likely"
        if c >= 0.55:
            return "Maybe"
        if c > 0:
            return "Unsure"
        return ""

    @property
    def confidence_lead(self) -> str:
        c = self.identified_confidence
        if c is None or c <= 0:
            return ""
        if c >= 0.85:
            return "Looks like"
        if c >= 0.55:
            return "Could be"
        return "Possibly"

"""JSON API for plant CRUD."""
from __future__ import annotations

import base64
import logging
from datetime import datetime
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import imaging, storage
from ..config import settings
from ..database import get_session
from ..models import ChatMessage, Photo, Plant

log = logging.getLogger(__name__)
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


class LookupIn(BaseModel):
    common_name: str


@router.post("/lookup")
async def lookup_by_common_name(payload: LookupIn) -> dict:
    """Ask n8n+Claude Haiku for the species and care notes given a common name.

    Used by the new-plant / edit-plant form's "Lookup" button so the user can
    type a corrected common name and pull fresh species + care info instead of
    keeping a wrong vision-ID guess.
    """
    name = payload.common_name.strip()
    if not name:
        raise HTTPException(400, "common_name is required")
    if not settings.n8n_lookup_webhook_url:
        raise HTTPException(503, "Lookup webhook not configured")

    headers = {"Content-Type": "application/json"}
    if settings.n8n_webhook_token:
        headers["X-Plant-Tracker-Token"] = settings.n8n_webhook_token

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                settings.n8n_lookup_webhook_url,
                json={"common_name": name},
                headers=headers,
            )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as e:
        log.warning("Plant lookup webhook failed for %r: %s", name, e)
        raise HTTPException(502, f"Lookup failed: {e}")

    # n8n sometimes wraps single results in a list — unwrap.
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        raise HTTPException(502, "Lookup returned unexpected shape")

    return {
        "species": data.get("species") or "",
        "common_name": data.get("common_name") or "",
        "care_notes": data.get("care_notes") or "",
    }


# ─── Chat (Ask Question) ────────────────────────────────────────────

CHAT_SYSTEM_PROMPT_TEMPLATE = (
    "You are a plant care expert helping the user with one specific houseplant. "
    "Be short and concise — keep replies to 1-3 plain sentences. "
    "Reply in plain text only: NO markdown, NO bullet lists, NO numbered lists, "
    "NO code blocks, NO headings, NO bold, NO italics, NO emoji. "
    "Just regular sentences. The chat UI cannot render anything else.\n\n"
    "Plant context:\n"
    "- Nickname: {nickname}\n"
    "- Species: {species}\n"
    "- Common name: {common_name}\n"
    "- Location: {location}\n"
    "- Care notes: {care_notes}\n\n"
    "If the user attaches a photo, examine it and answer their question about it."
)


def _system_prompt_for(plant: Plant) -> str:
    return CHAT_SYSTEM_PROMPT_TEMPLATE.format(
        nickname=plant.nickname or "",
        species=plant.species or "(unknown)",
        common_name=plant.common_name or "(unknown)",
        location=plant.location or "(unknown)",
        care_notes=plant.care_notes or "(none on file)",
    )


def _public_base(request: Request) -> str:
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    # Fall back to whatever the request came in on (good for local-LAN testing).
    return f"{request.url.scheme}://{request.url.netloc}"


class ChatSendIn(BaseModel):
    content: str
    photo_id: Optional[int] = None  # if set, attach this photo to the new user message


@router.get("/{plant_id}/chat")
def chat_history(plant_id: int, db: Session = Depends(get_session)) -> dict:
    plant = db.get(Plant, plant_id)
    if plant is None:
        raise HTTPException(404, "Plant not found")
    rows = (
        db.query(ChatMessage)
        .filter(ChatMessage.plant_id == plant_id)
        .order_by(ChatMessage.created_at.asc())
        .all()
    )
    return {
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "photo_id": m.photo_id,
                "created_at": m.created_at.isoformat(),
            }
            for m in rows
        ]
    }


@router.delete("/{plant_id}/chat")
def chat_clear(plant_id: int, db: Session = Depends(get_session)) -> dict:
    plant = db.get(Plant, plant_id)
    if plant is None:
        raise HTTPException(404, "Plant not found")
    deleted = db.query(ChatMessage).filter(ChatMessage.plant_id == plant_id).delete()
    db.commit()
    return {"ok": True, "deleted": deleted}


@router.post("/{plant_id}/chat")
async def chat_send(
    plant_id: int,
    payload: ChatSendIn,
    request: Request,
    db: Session = Depends(get_session),
) -> dict:
    plant = db.get(Plant, plant_id)
    if plant is None:
        raise HTTPException(404, "Plant not found")
    if not settings.n8n_chat_webhook_url:
        raise HTTPException(503, "Chat webhook not configured")

    text = (payload.content or "").strip()
    if not text:
        raise HTTPException(400, "Message content is required")

    # Resolve attached photo (if any) to inline base64. Sending inline avoids
    # auth/edge issues with the image URL (Cloudflare Access in front of
    # plants.mayorawesome.com would otherwise 302 Anthropic's fetcher to a
    # login page). Always re-encode to a small JPEG (768px max side, q=75)
    # to keep token cost predictable regardless of source resolution.
    image_block: Optional[dict] = None
    photo_id_to_record: Optional[int] = None
    if payload.photo_id is not None:
        photo = db.get(Photo, payload.photo_id)
        if photo is None or photo.plant_id != plant_id:
            raise HTTPException(400, "photo_id does not belong to this plant")
        rel = photo.filename  # full-res original gets downsized below
        try:
            abs_path = storage.absolute(rel)
            jpeg_bytes = imaging.make_identify_payload(abs_path, max_side=768, quality=75)
            b64 = base64.b64encode(jpeg_bytes).decode("ascii")
            image_block = {"type": "base64", "media_type": "image/jpeg", "data": b64}
            log.info(
                "Chat photo %s: %d bytes encoded to %d bytes base64",
                rel, len(jpeg_bytes), len(b64),
            )
        except (OSError, ValueError) as e:
            log.warning("Could not encode photo %s: %s", rel, e)
            raise HTTPException(500, "Could not read attached photo")
        photo_id_to_record = photo.id

    # Persist the user turn before calling out, so a webhook failure leaves
    # the conversation in a coherent state on next page load.
    user_msg = ChatMessage(
        plant_id=plant_id,
        role="user",
        content=text,
        photo_id=photo_id_to_record,
    )
    db.add(user_msg)
    db.commit()

    # Build full message history (excluding any future entries — we just added
    # the user turn so it's the last).
    history = (
        db.query(ChatMessage)
        .filter(ChatMessage.plant_id == plant_id)
        .order_by(ChatMessage.created_at.asc())
        .all()
    )
    api_messages = [{"role": m.role, "content": m.content} for m in history]

    headers = {"Content-Type": "application/json"}
    if settings.n8n_webhook_token:
        headers["X-Plant-Tracker-Token"] = settings.n8n_webhook_token

    body = {
        "messages": api_messages,
        "system": _system_prompt_for(plant),
        # `image` is a full Anthropic source object (or None). The n8n
        # workflow splices it into the last user message's content.
        "image": image_block,
    }

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(settings.n8n_chat_webhook_url, json=body, headers=headers)
        resp.raise_for_status()
        # Webhook can return 200 with empty body if the workflow errors before
        # the Respond node — surface that clearly instead of a JSON-decode 500.
        if not resp.content:
            raise HTTPException(502, "Chat webhook returned an empty response (workflow likely errored). Check n8n executions.")
        data = resp.json()
    except httpx.HTTPError as e:
        log.warning("Chat webhook failed for plant %s: %s", plant_id, e)
        raise HTTPException(502, f"Chat failed: {e}")

    if isinstance(data, list) and data:
        data = data[0]
    reply = (data.get("reply") if isinstance(data, dict) else "") or ""
    reply = reply.strip()
    if not reply:
        raise HTTPException(502, "Empty reply from chat webhook")

    asst_msg = ChatMessage(plant_id=plant_id, role="assistant", content=reply)
    db.add(asst_msg)
    db.commit()
    db.refresh(asst_msg)

    return {
        "user": {
            "id": user_msg.id,
            "role": "user",
            "content": user_msg.content,
            "photo_id": user_msg.photo_id,
            "created_at": user_msg.created_at.isoformat(),
        },
        "assistant": {
            "id": asst_msg.id,
            "role": "assistant",
            "content": asst_msg.content,
            "photo_id": None,
            "created_at": asst_msg.created_at.isoformat(),
        },
    }

"""Opt-in raw audio of a live session (owner-only) — `README.md` §9.

Additive routes under `/api/live/sessions/{id}/recording`. Audio exists only
for owners listed in `LIVE_AUDIO_RETENTION_EMAILS` and only until the
session's `audio_retain_until` (7 days by default); see `app.live.audio_store`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.db import SessionLocal, get_db
from app.deps import get_current_user
from app.live import audio_store, service
from app.models.live import LiveSession
from app.models.user import User

router = APIRouter(prefix="/live", tags=["live"])


def _owned(db: Session, session_id: UUID, user: User, *, lock: bool = False) -> LiveSession:
    s = service.get_readable(db, session_id, user, lock=lock)
    service.require_owner(s, user)
    return s


@router.get("/sessions/{session_id}/recording/info")
def recording_info(
    session_id: UUID,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> dict[str, Any]:
    """How much raw audio is stored for this session and until when (owner only)."""
    return audio_store.recording_info(db, _owned(db, session_id, current))


@router.get("/sessions/{session_id}/recording")
def download_recording(
    session_id: UUID,
    start_s: float = Query(default=0.0, ge=0),
    end_s: float | None = Query(default=None, ge=0),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> StreamingResponse:
    """The stored audio as a 16 kHz mono PCM16 WAV (optionally a time range)."""
    s = _owned(db, session_id, current)
    if end_s is not None and end_s <= start_s:
        raise HTTPException(422, "end_s must be after start_s.")
    samples, rate, a, b = audio_store.plan_wav(db, s, start_s, end_s)
    if samples <= 0:
        raise HTTPException(404, "No audio is stored for this session (or that range).")
    name = f"rgs-{session_id}.wav"
    return StreamingResponse(
        audio_store.iter_wav(SessionLocal, s.id, samples, rate, a, b),
        media_type="audio/wav",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Content-Length": str(44 + samples * audio_store.SAMPLE_WIDTH),
            "Cache-Control": "no-store",
        },
    )


@router.delete("/sessions/{session_id}/recording")
def delete_recording(
    session_id: UUID,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Delete the stored audio now and keep no more of it (owner only)."""
    return audio_store.delete_recording(db, _owned(db, session_id, current, lock=True))

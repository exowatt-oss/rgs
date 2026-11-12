"""Optional on-device words (Copper browser only); see README.md Configuration / Transcription.

Additive routes under `/api/live/sessions/{id}/ondevice/…`; nothing else in
the live API changes, and a session that never gets on-device words behaves
exactly as before.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.live import ondevice, service
from app.live.runtime import db_call, manager
from app.models.live import LiveSegment, LiveSpeaker
from app.models.user import User

router = APIRouter(prefix="/live", tags=["live"])

# Nemotron segments this far either side of the posted phrases are the live evidence.
LIVE_CONTEXT_S = 60.0


class OnDeviceWordIn(BaseModel):
    text: str = Field(max_length=200)
    start_s: float
    end_s: float


class OnDeviceSegmentIn(BaseModel):
    seq: int = Field(ge=0)
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)
    text: str = Field(default="", max_length=4000)
    words: list[OnDeviceWordIn] = Field(default_factory=list, max_length=ondevice.MAX_WORDS)


class OnDeviceIn(BaseModel):
    engine: str = Field(default=ondevice.ENGINE_DEFAULT, max_length=32)
    segments: list[OnDeviceSegmentIn] = Field(max_length=ondevice.MAX_SEGMENTS)


def _resolver(db: Session, session_id: UUID):
    rows = {
        sp.label: sp
        for sp in db.scalars(select(LiveSpeaker).where(LiveSpeaker.session_id == session_id))
    }
    return lambda label: service._resolve_label(rows, label)  # noqa: SLF001


def _accept(
    db: Session, session_id: UUID, user: User, body: OnDeviceIn, labelled: list[Any]
) -> tuple[list[dict], dict[str, Any]]:
    s = service.get_readable(db, session_id, user, lock=True)
    service.require_owner(s, user)
    phrases = ondevice.parse_phrases([seg.model_dump() for seg in body.segments])
    fresh = ondevice.store(db, s.id, phrases, body.engine)
    events: list[dict] = []
    if fresh:
        lo = min(p.start for p in fresh) - LIVE_CONTEXT_S
        hi = max(p.end for p in fresh) + LIVE_CONTEXT_S
        rows = db.scalars(
            select(LiveSegment).where(
                LiveSegment.session_id == s.id,
                LiveSegment.end_seconds >= lo,
                LiveSegment.start_seconds <= hi,
            )
        ).all()
        resolve = _resolver(db, s.id)
        index = ondevice._Index(ondevice.live_turns(rows, labelled, resolve))  # noqa: SLF001
        for phrase in sorted(fresh, key=lambda p: p.start):
            parts = ondevice.attribute(phrase, index)
            data = ondevice.event_data(phrase, parts, body.engine)
            events.append(service.append_event(db, s.id, "ondevice.segment", data))
        if s.final_transcript is not None:
            # Words that arrive after the end still make the stored transcript.
            from app.live.finalize import render_transcript

            s.final_transcript = render_transcript(db, s.id) or s.final_transcript
    db.commit()
    return events, {"accepted": len(fresh), "duplicates": len(phrases) - len(fresh)}


@router.post("/sessions/{session_id}/ondevice/segments")
async def post_ondevice_segments(
    session_id: UUID,
    body: OnDeviceIn,
    current: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Finished on-device phrases with word times (owner). Idempotent by `seq`."""
    rt = manager.get(session_id)
    labelled: list[Any] = []
    if rt is not None:
        labelled = [lab.word for lab in list(rt.labelled)]
    events, out = await db_call(_accept, session_id, current, body, labelled)
    for event in events:
        manager.broker.publish(session_id, event)
    return out


def _hybrid(db: Session, session_id: UUID, user: User) -> dict[str, Any]:
    s = service.get_readable(db, session_id, user)
    resolve = _resolver(db, s.id)
    rows = db.scalars(
        select(LiveSegment).where(LiveSegment.session_id == s.id).order_by(LiveSegment.seq)
    ).all()
    phrases = ondevice.load(db, s.id)
    merged = ondevice.merge(phrases, ondevice.turns_from_rows(rows, resolve))
    return {
        "engine": ondevice.engine_of(db, s.id),
        "ondevice_segments": len(phrases),
        "segments": [m.out() for m in merged],
        "status": s.status,
    }


@router.get("/sessions/{session_id}/ondevice/transcript")
def get_ondevice_transcript(
    session_id: UUID,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> dict[str, Any]:
    """The hybrid transcript: on-device words with Nemotron's speakers, by time."""
    return _hybrid(db, session_id, current)


def _phrases(db: Session, session_id: UUID, user: User) -> dict[str, Any]:
    s = service.get_readable(db, session_id, user)
    engine = ondevice.engine_of(db, s.id) or ondevice.ENGINE_DEFAULT
    return {
        "segments": [
            {
                "client_seq": p.seq,
                "engine": engine,
                "start_s": round(p.start, 3),
                "end_s": round(p.end, 3),
                "text": p.text,
                "words": [
                    {"text": w.text, "start_s": round(w.start, 3), "end_s": round(w.end, 3)}
                    for w in p.words
                ],
            }
            for p in ondevice.load(db, s.id)
        ]
    }


@router.get("/sessions/{session_id}/ondevice/segments")
def get_ondevice_segments(
    session_id: UUID,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> dict[str, Any]:
    """The on-device phrases as posted (a finished session's view re-applies them)."""
    return _phrases(db, session_id, current)

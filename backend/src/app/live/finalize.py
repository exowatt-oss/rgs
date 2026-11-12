"""Stop-time finalize / discard for RGS live sessions.

Much simpler than rgs's (which wrote a recording + canonical transcript
and enqueued its meeting pipeline): RGS renders the live transcript into
`live_sessions.final_transcript` ("[mm:ss] Name: text" lines, consecutive
same-speaker segments merged, merged speaker labels resolved) and marks the
session finalized. Discard deletes everything the session produced.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.live import service
from app.live.recap import get_recap_settings
from app.live.utterances import coalesce
from app.models.live import (
    LiveEvent,
    LiveFinding,
    LiveSegment,
    LiveSession,
    LiveSpeaker,
    LiveTrigger,
)
from app.models.live_audio import LiveAudioChunk

log = structlog.get_logger(__name__)

MERGE_GAP_S = 1.5


def _clock(seconds: float) -> str:
    total = int(max(0.0, seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def merge_segments(rows: list[LiveSegment], resolve) -> list[tuple[str, float, float, str]]:
    """Each speaker's fragments stitched into utterances (`app.live.utterances`)."""
    utterances = coalesce(
        (
            (seg.seq, seg.speaker_label, float(seg.start_seconds), float(seg.end_seconds), seg.text)
            for seg in rows
        ),
        resolve=resolve,
        gap_s=MERGE_GAP_S,
    )
    return [(u.speaker, u.start, u.end, u.text) for u in utterances]


def render_transcript(db: Session, session_id: UUID) -> str:
    """The session's transcript as plain text with display names."""
    speakers = {
        sp.label: sp
        for sp in db.scalars(select(LiveSpeaker).where(LiveSpeaker.session_id == session_id))
    }

    def resolve(label: str) -> str:
        seen: set[str] = set()
        while label in speakers and speakers[label].merged_into and label not in seen:
            seen.add(label)
            label = speakers[label].merged_into or label
        return label

    rows = db.scalars(
        select(LiveSegment).where(LiveSegment.session_id == session_id).order_by(LiveSegment.seq)
    ).all()
    # On-device words (Copper), when the browser sent any: the authenticated OpenAPI schema.
    from app.live.ondevice import hybrid_rows

    rows = hybrid_rows(db, session_id, list(rows), resolve)
    lines = []
    for label, start, _end, text in merge_segments(list(rows), resolve):
        sp = speakers.get(label)
        name = sp.display_name if sp else f"Speaker {label.lstrip('S')}"
        lines.append(f"[{_clock(start)}] {name}: {text}")
    return "\n".join(lines)


def finalize_session(db: Session, session_id: UUID, *, incomplete: bool = False) -> dict | None:
    """Store the final transcript and mark the session finalized.

    Idempotent: a terminal session returns None. Returns the final
    `session.status` event for the caller to publish.
    """
    s = db.scalar(select(LiveSession).where(LiveSession.id == session_id).with_for_update())
    if s is None or s.status in service.TERMINAL_STATUSES:
        db.rollback()
        return None
    text = render_transcript(db, session_id)
    s.final_transcript = text or None
    s.incomplete = bool(incomplete)
    if s.recap_enabled and get_recap_settings().recap_enabled:
        # The recap (notes → newspaper → Slack/email) runs after this commit.
        s.recap_status = "pending" if text.strip() else "skipped"
        s.recap_error = None if text.strip() else "Nothing was transcribed, so nothing to print."
    db.commit()
    log.info(
        "live.finalized",
        session_id=str(session_id),
        chars=len(text),
        incomplete=bool(incomplete),
    )
    return service.set_status(db, session_id, "finalized")


def discard_session(db: Session, session_id: UUID) -> dict | None:
    """Delete the live transcript, speakers, findings, triggers and events."""
    s = db.scalar(select(LiveSession).where(LiveSession.id == session_id).with_for_update())
    if s is None:
        db.rollback()
        return None
    for model in (LiveFinding, LiveTrigger, LiveSegment, LiveSpeaker, LiveEvent, LiveAudioChunk):
        db.execute(delete(model).where(model.session_id == session_id))
    s.audio_retain_until = None
    s.final_transcript = None
    s.audio_samples = 0
    s.audio_chunk_count = 0
    db.commit()
    return service.set_status(db, session_id, "cancelled")

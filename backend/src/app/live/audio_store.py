"""Opt-in short-retention raw audio for live sessions (owner-only, deletable).

RGS keeps no call audio by default — only the transcript. When the session
owner's email is listed in `LIVE_AUDIO_RETENTION_EMAILS` (a server setting;
prod: the app owner only), the live PCM is also stored in
`live_audio_chunks` until `live_sessions.audio_retain_until`
(`LIVE_AUDIO_RETENTION_DAYS`, default 7). That makes a bad transcript or
speaker split reproducible: the owner downloads the WAV
(`GET /api/live/sessions/{id}/recording`), replays it through the pipeline,
and deletes it (`DELETE …/recording`) whenever they like. The sweeper purges
expired audio.

The chunks are written in the same transaction as the audio ledger
(`service.persist_audio_chunk`), so stored audio is exactly the ledger.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.live.settings import LiveSettings, get_live_settings
from app.models.live import LiveSession
from app.models.live_audio import LiveAudioChunk

log = structlog.get_logger(__name__)

SAMPLE_WIDTH = 2  # PCM16
WAV_BATCH = 30  # chunks fetched per query while streaming


def retention_emails(settings: LiveSettings | None = None) -> set[str]:
    settings = settings or get_live_settings()
    raw = settings.live_audio_retention_emails or ""
    return {e.strip().lower() for e in raw.replace(";", ",").split(",") if e.strip()}


def retain_until(
    email: str | None, *, now: datetime | None = None, settings: LiveSettings | None = None
) -> datetime | None:
    """When a new session's audio expires, or None when this owner keeps none."""
    settings = settings or get_live_settings()
    if not email or email.strip().lower() not in retention_emails(settings):
        return None
    return (now or datetime.now(UTC)) + timedelta(days=settings.live_audio_retention_days)


def keep_chunk(db: Session, session: LiveSession, seq: int, start_sample: int, pcm: bytes) -> bool:
    """Store one ledger chunk if this session retains audio (caller commits)."""
    until = session.audio_retain_until
    if until is None or until <= datetime.now(UTC) or not pcm:
        return False
    db.execute(
        insert(LiveAudioChunk)
        .values(
            session_id=session.id,
            seq=seq,
            start_sample=start_sample,
            sample_count=len(pcm) // SAMPLE_WIDTH,
            pcm=pcm,
        )
        .on_conflict_do_nothing(index_elements=["session_id", "seq"])
    )
    return True


def recording_info(db: Session, session: LiveSession) -> dict[str, Any]:
    row = db.execute(
        select(
            func.count(LiveAudioChunk.seq),
            func.coalesce(func.sum(LiveAudioChunk.sample_count), 0),
            func.coalesce(func.sum(func.octet_length(LiveAudioChunk.pcm)), 0),
            func.min(LiveAudioChunk.start_sample),
        ).where(LiveAudioChunk.session_id == session.id)
    ).one()
    chunks, samples, size, first = int(row[0]), int(row[1]), int(row[2]), row[3]
    rate = session.sample_rate or 16000
    until = session.audio_retain_until
    return {
        "retained": chunks > 0,
        "retaining": until is not None and until > datetime.now(UTC),
        "retain_until": until.astimezone(UTC).isoformat().replace("+00:00", "Z") if until else None,
        "chunks": chunks,
        "seconds": round(samples / rate, 3),
        "start_s": round(int(first) / rate, 3) if first is not None else None,
        "bytes": size,
        "sample_rate": rate,
        "format": "audio/wav; PCM16 mono",
    }


def wav_header(samples: int, rate: int) -> bytes:
    data = samples * SAMPLE_WIDTH
    return (
        b"RIFF"
        + struct.pack("<I", 36 + data)
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * SAMPLE_WIDTH, SAMPLE_WIDTH, 16)
        + b"data"
        + struct.pack("<I", data)
    )


def _overlap(start_sample: int, count: int, a: int, b: int | None) -> tuple[int, int] | None:
    """Byte range of a chunk inside [a, b) samples, or None."""
    lo = max(start_sample, a)
    hi = start_sample + count if b is None else min(start_sample + count, b)
    if hi <= lo:
        return None
    return (lo - start_sample) * SAMPLE_WIDTH, (hi - start_sample) * SAMPLE_WIDTH


def plan_wav(
    db: Session, session: LiveSession, start_s: float = 0.0, end_s: float | None = None
) -> tuple[int, int, int, int | None]:
    """(samples, rate, first_sample, last_sample) of the WAV a range would produce."""
    rate = session.sample_rate or 16000
    a = max(0, int(round(start_s * rate)))
    b = None if end_s is None else max(a, int(round(end_s * rate)))
    rows = db.execute(
        select(LiveAudioChunk.start_sample, LiveAudioChunk.sample_count).where(
            LiveAudioChunk.session_id == session.id
        )
    ).all()
    total = 0
    for start, count in rows:
        r = _overlap(int(start), int(count), a, b)
        if r is not None:
            total += (r[1] - r[0]) // SAMPLE_WIDTH
    return total, rate, a, b


def iter_wav(
    session_factory: Any,
    session_id: UUID,
    samples: int,
    rate: int,
    a: int,
    b: int | None,
) -> Iterator[bytes]:
    """Stream header + PCM chunk by chunk (own DB session; bounded memory)."""
    yield wav_header(samples, rate)
    after = -1
    while True:
        with session_factory() as db:
            rows = db.execute(
                select(LiveAudioChunk.seq, LiveAudioChunk.start_sample, LiveAudioChunk.pcm)
                .where(LiveAudioChunk.session_id == session_id, LiveAudioChunk.seq > after)
                .order_by(LiveAudioChunk.seq)
                .limit(WAV_BATCH)
            ).all()
        if not rows:
            return
        for seq, start, pcm in rows:
            after = seq
            r = _overlap(int(start), len(pcm) // SAMPLE_WIDTH, a, b)
            if r is not None:
                yield bytes(pcm[r[0] : r[1]])


def delete_recording(db: Session, session: LiveSession) -> dict[str, Any]:
    """Delete a session's stored audio and stop retaining any more of it."""
    info = recording_info(db, session)
    db.execute(delete(LiveAudioChunk).where(LiveAudioChunk.session_id == session.id))
    session.audio_retain_until = None
    db.commit()
    log.info("live.audio_deleted", session_id=str(session.id), chunks=info["chunks"])
    return {"deleted_chunks": info["chunks"], "deleted_seconds": info["seconds"]}


def purge_expired(db: Session, *, now: datetime | None = None) -> int:
    """Drop audio past its retention date. Returns the number of sessions purged."""
    now = now or datetime.now(UTC)
    ids = list(
        db.scalars(
            select(LiveSession.id).where(
                LiveSession.audio_retain_until.is_not(None),
                LiveSession.audio_retain_until <= now,
            )
        )
    )
    if not ids:
        return 0
    db.execute(delete(LiveAudioChunk).where(LiveAudioChunk.session_id.in_(ids)))
    db.execute(update(LiveSession).where(LiveSession.id.in_(ids)).values(audio_retain_until=None))
    db.commit()
    log.info("live.audio_purged", sessions=len(ids))
    return len(ids)

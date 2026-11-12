"""Short-retention raw call audio (opt-in, owner-only) — `app.live.audio_store`.

RGS normally keeps no audio. For owners listed in
`LIVE_AUDIO_RETENTION_EMAILS` the live PCM is also stored here, chunk by
chunk, until `live_sessions.audio_retain_until` (default 7 days), so a bad
transcript or speaker split can be reproduced. The owner can download or
delete it at any time; the sweeper purges expired audio.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, LargeBinary
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db import Base


class LiveAudioChunk(Base):
    __tablename__ = "live_audio_chunks"

    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("live_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # Same numbering as the session's audio ledger (`audio_chunk_count`).
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    start_sample: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # Raw PCM16 little-endian mono at the session's sample rate (16 kHz).
    pcm: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

"""On-device words a browser sent for a live session (Copper's Phonon-2).

One row per finished phrase the browser's own speech-to-text heard, with its
words and their times on the session timeline (seconds from session audio
sample 0). Nemotron still decides who spoke; `app.live.ondevice` merges the
two by time. Contract: `README.md` §8.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db import Base


class LiveOnDeviceSegment(Base):
    __tablename__ = "live_ondevice_segments"
    __table_args__ = (
        UniqueConstraint("session_id", "client_seq", name="uq_live_ondevice_segments_client_seq"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=sa_text("gen_random_uuid()")
    )
    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("live_sessions.id", ondelete="CASCADE"), nullable=False
    )
    # The browser's own phrase number: a retried POST never stores a phrase twice.
    client_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    engine: Mapped[str] = mapped_column(String(32), nullable=False)
    start_seconds: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    end_seconds: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    text: Mapped[str] = mapped_column(String, nullable=False)
    # [{"text", "start_s", "end_s"}], session timeline seconds.
    words: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=sa_text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

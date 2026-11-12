"""RGS live sessions, live transcript, speakers, findings and gate triggers.

Ported from rgs Live Record (design notes), simplified for a standalone app:
one `live_sessions` row per press of the record button. A session may carry
an optional call link (Google Meet or any URL) and an optional attendee list
(used to resolve self-introductions to full names). At stop the final
transcript is rendered into `live_sessions.final_transcript` — there is no
downstream meeting pipeline.

`live_events` is the durable, replayable event log behind the WebSocket /
long-poll fan-out: `(session_id, seq)` with `seq` allocated from
`live_sessions.last_seq` by a single `UPDATE ... RETURNING` (no gaps from
concurrent writers, no reuse). Interim text is never persisted.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db import Base

LIVE_STATUSES = ("live", "stopping", "finalized", "failed", "cancelled")
ACTIVE_STATUSES = ("live", "stopping")
TRIGGER_STATUSES = ("pending", "claimed", "answered", "expired")


class LiveSession(Base):
    __tablename__ = "live_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('live', 'stopping', 'finalized', 'failed', 'cancelled')",
            name="ck_live_sessions_status",
        ),
        Index(
            "ix_live_sessions_active_code",
            "meet_code",
            postgresql_where=text("status IN ('live', 'stopping')"),
        ),
        Index("ix_live_sessions_started_by", "started_by_user_id", "started_at"),
        Index(
            "ix_live_sessions_recap_inflight",
            "recap_status",
            postgresql_where=text(
                "recap_status IN ('pending', 'writing', 'printing', 'delivering', 'sharing')"
            ),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    started_by_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    meet_url: Mapped[str | None] = mapped_column(String)
    meet_code: Mapped[str | None] = mapped_column(String(64))
    # Optional roster [{"name": ..., "email": ...}] for self-intro name matching.
    attendees: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'live'"), default="live"
    )
    asr_state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'ok'"), default="ok"
    )
    sample_rate: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("16000"), default=16000
    )
    # Samples durably persisted to object storage (contiguous from 0).
    audio_samples: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), default=0
    )
    audio_chunk_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    # Opt-in raw audio retention (app/live/audio_store.py): chunks are kept in
    # `live_audio_chunks` until this time; NULL = no audio kept.
    audio_retain_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Transcription commit point (seconds); everything before it is final.
    committed_until_s: Mapped[Decimal] = mapped_column(
        Numeric(10, 3), nullable=False, server_default=text("0"), default=0
    )
    last_seq: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    # Rendered "Name: text" transcript written at finalize (plain text).
    final_transcript: Mapped[str | None] = mapped_column(Text)
    # True when part of the audio could not be transcribed live.
    incomplete: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false"), default=False
    )
    error: Mapped[str | None] = mapped_column(String)
    # How many people are expected to talk (start hint; NULL = unknown).
    expected_speakers: Mapped[int | None] = mapped_column(Integer)
    # Live gate provider for this session (`jev` | `openai`); NULL = deployment default.
    gate_provider: Mapped[str | None] = mapped_column(String(16))
    # `call` (has a call link) | `adhoc` (an in-person / ad-hoc conversation, no link).
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'call'"), default="call"
    )
    # Recap (app/live/recap.py): at stop the LLM writes structured notes, RGS
    # publishes them as a external publisher edition and sends the owner the link.
    recap_enabled: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("true"), default=True
    )
    # Where the edition link goes: any of "slack", "email" ([] = only in RGS).
    deliver_via: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[\"slack\"]'::jsonb"),
        default=lambda: ["slack"],
    )
    # Opt-in (off by default): the recap also sends the edition and the RGS link to
    # the people RGS recognised in the call (their local directory identity), by `deliver_via`.
    share_with_participants: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false"), default=False
    )
    # Emails the recap was actually shared with: they may read (never change) the session.
    shared_with: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )
    # NULL (no recap) | pending | writing | printing | delivering | sharing | done | failed
    # | skipped
    recap_status: Mapped[str | None] = mapped_column(String(16))
    recap_notes: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    recap_edition: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    recap_delivery: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    recap_error: Mapped[str | None] = mapped_column(String)
    recap_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_audio_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LiveSpeaker(Base):
    __tablename__ = "live_speakers"
    __table_args__ = (UniqueConstraint("session_id", "label", name="uq_live_speakers_label"),)

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("live_sessions.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(16), nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    color_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # Bound only by an explicit owner rename (never automatically).
    user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    suggested_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    suggestion_score: Mapped[float | None] = mapped_column(Float)
    merged_into: Mapped[str | None] = mapped_column(String(16))
    # How display_name was set: NULL = default label, `manual` = owner rename
    # (never overridden), `self_intro` = the speaker introduced themselves,
    # `inferred` = the naming pass (address + reply, introductions by others,
    # the roster, elimination).
    name_source: Mapped[str | None] = mapped_column(String(16))
    # Confidence (0-1) of an automatic name and the words that gave it away.
    name_confidence: Mapped[float | None] = mapped_column(Float)
    name_evidence: Mapped[str | None] = mapped_column(String)
    # local directory identity behind the name (directory email), when resolved.
    person_email: Mapped[str | None] = mapped_column(String(320))
    # Low-confidence guess shown as "maybe X?" (directory person or as heard).
    suggested_name: Mapped[str | None] = mapped_column(String)
    suggested_email: Mapped[str | None] = mapped_column(String(320))
    speech_seconds: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0"), default=0.0
    )
    # Session-scoped endpoint embedding centroid (crash recovery only).
    centroid: Mapped[list[float] | None] = mapped_column(ARRAY(Float))
    centroid_weight: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0"), default=0.0
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LiveSegment(Base):
    __tablename__ = "live_segments"
    __table_args__ = (UniqueConstraint("session_id", "seq", name="uq_live_segments_seq"),)

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("live_sessions.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    speaker_label: Mapped[str] = mapped_column(String(16), nullable=False)
    start_seconds: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    end_seconds: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    text: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LiveFinding(Base):
    __tablename__ = "live_findings"
    __table_args__ = (
        Index("ix_live_findings_session_created", "session_id", "created_at"),
        Index(
            "uq_live_findings_trigger",
            "trigger_id",
            unique=True,
            postgresql_where=text("trigger_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("live_sessions.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    title_key: Mapped[str] = mapped_column(String(200), nullable=False)
    body_md: Mapped[str] = mapped_column(String, nullable=False)
    links: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )
    related_seq: Mapped[int | None] = mapped_column(Integer)
    trigger_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("live_triggers.id", ondelete="SET NULL")
    )
    source_agent: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LiveTrigger(Base):
    __tablename__ = "live_triggers"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'claimed', 'answered', 'expired')",
            name="ck_live_triggers_status",
        ),
        Index("ix_live_triggers_session_status", "session_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("live_sessions.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    query: Mapped[str] = mapped_column(String(1000), nullable=False)
    seq_from: Mapped[int] = mapped_column(Integer, nullable=False)
    seq_to: Mapped[int] = mapped_column(Integer, nullable=False)
    probability: Mapped[float] = mapped_column(Float, nullable=False)
    jev_latency_ms: Mapped[int | None] = mapped_column(Integer)
    provider: Mapped[str | None] = mapped_column(String(16))  # gate provider that decided
    anchor_seq: Mapped[int | None] = mapped_column(Integer)
    topic: Mapped[str | None] = mapped_column(String(500))
    context_text: Mapped[str | None] = mapped_column(String(4000))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'"), default="pending"
    )
    claimed_by: Mapped[str | None] = mapped_column(String(64))
    claimed_by_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finding_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LiveEvent(Base):
    __tablename__ = "live_events"

    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("live_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

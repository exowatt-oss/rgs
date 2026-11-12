"""RGS live sessions, live transcript, speakers, findings, gate triggers.

Ported from rgs 0067_live_record + 0068_live_gate_provider, without the
links to rgs's own tables (RGS stores the final transcript on the session).

Revision ID: 0002_live
Revises: 0001_initial_users
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002_live"
down_revision = "0001_initial_users"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def _uuid_pk() -> sa.Column:
    return sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _now(name: str = "created_at") -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _session_fk() -> sa.Column:
    return sa.Column(
        "session_id",
        UUID,
        sa.ForeignKey("live_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )


def upgrade() -> None:
    op.execute(
        "CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$ "
        "BEGIN NEW.updated_at = now(); RETURN NEW; END; $$ LANGUAGE plpgsql"
    )

    op.create_table(
        "live_sessions",
        _uuid_pk(),
        sa.Column(
            "started_by_user_id",
            UUID,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("meet_url", sa.String(), nullable=True),
        sa.Column("meet_code", sa.String(64), nullable=True),
        sa.Column(
            "attendees", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="live"),
        sa.Column("asr_state", sa.String(16), nullable=False, server_default="ok"),
        sa.Column("sample_rate", sa.Integer(), nullable=False, server_default="16000"),
        sa.Column("audio_samples", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("audio_chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("committed_until_s", sa.Numeric(10, 3), nullable=False, server_default="0"),
        sa.Column("last_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("final_transcript", sa.Text(), nullable=True),
        sa.Column("incomplete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("gate_provider", sa.String(16), nullable=True),
        _now("started_at"),
        sa.Column("last_audio_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        _now("updated_at"),
        sa.CheckConstraint(
            "status IN ('live', 'stopping', 'finalized', 'failed', 'cancelled')",
            name="ck_live_sessions_status",
        ),
    )
    op.create_index(
        "ix_live_sessions_started_by", "live_sessions", ["started_by_user_id", "started_at"]
    )
    op.create_index(
        "ix_live_sessions_active_code",
        "live_sessions",
        ["meet_code"],
        postgresql_where=sa.text("status IN ('live', 'stopping')"),
    )
    op.execute(
        "CREATE TRIGGER trg_live_sessions_updated_at BEFORE UPDATE ON live_sessions "
        "FOR EACH ROW EXECUTE FUNCTION touch_updated_at()"
    )

    op.create_table(
        "live_speakers",
        _uuid_pk(),
        _session_fk(),
        sa.Column("label", sa.String(16), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("color_index", sa.Integer(), nullable=False),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("suggested_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("suggestion_score", sa.Float(), nullable=True),
        sa.Column("merged_into", sa.String(16), nullable=True),
        sa.Column("name_source", sa.String(16), nullable=True),
        sa.Column("speech_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("centroid", postgresql.ARRAY(sa.Float()), nullable=True),
        sa.Column("centroid_weight", sa.Float(), nullable=False, server_default="0"),
        _now(),
        _now("updated_at"),
        sa.UniqueConstraint("session_id", "label", name="uq_live_speakers_label"),
    )
    op.execute(
        "CREATE TRIGGER trg_live_speakers_updated_at BEFORE UPDATE ON live_speakers "
        "FOR EACH ROW EXECUTE FUNCTION touch_updated_at()"
    )

    op.create_table(
        "live_segments",
        _uuid_pk(),
        _session_fk(),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("speaker_label", sa.String(16), nullable=False),
        sa.Column("start_seconds", sa.Numeric(10, 3), nullable=False),
        sa.Column("end_seconds", sa.Numeric(10, 3), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        _now(),
        sa.UniqueConstraint("session_id", "seq", name="uq_live_segments_seq"),
    )

    op.create_table(
        "live_triggers",
        sa.Column("id", UUID, primary_key=True),
        _session_fk(),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("query", sa.String(1000), nullable=False),
        sa.Column("seq_from", sa.Integer(), nullable=False),
        sa.Column("seq_to", sa.Integer(), nullable=False),
        sa.Column("probability", sa.Float(), nullable=False),
        sa.Column("jev_latency_ms", sa.Integer(), nullable=True),
        sa.Column("provider", sa.String(16), nullable=True),
        sa.Column("anchor_seq", sa.Integer(), nullable=True),
        sa.Column("topic", sa.String(500), nullable=True),
        sa.Column("context_text", sa.String(4000), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("claimed_by", sa.String(64), nullable=True),
        sa.Column("claimed_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finding_id", UUID, nullable=True),
        _now(),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'answered', 'expired')",
            name="ck_live_triggers_status",
        ),
    )
    op.create_index("ix_live_triggers_session_status", "live_triggers", ["session_id", "status"])

    op.create_table(
        "live_findings",
        _uuid_pk(),
        _session_fk(),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("title_key", sa.String(200), nullable=False),
        sa.Column("body_md", sa.String(), nullable=False),
        sa.Column(
            "links",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("related_seq", sa.Integer(), nullable=True),
        sa.Column("trigger_id", UUID, sa.ForeignKey("live_triggers.id", ondelete="SET NULL")),
        sa.Column("source_agent", sa.String(64), nullable=False),
        sa.Column("created_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        _now(),
    )
    op.create_index(
        "ix_live_findings_session_created", "live_findings", ["session_id", "created_at"]
    )
    op.create_index(
        "uq_live_findings_trigger",
        "live_findings",
        ["trigger_id"],
        unique=True,
        postgresql_where=sa.text("trigger_id IS NOT NULL"),
    )

    op.create_table(
        "live_events",
        sa.Column(
            "session_id",
            UUID,
            sa.ForeignKey("live_sessions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("seq", sa.Integer(), primary_key=True),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        _now(),
    )


def downgrade() -> None:
    op.drop_table("live_events")
    op.drop_index("uq_live_findings_trigger", table_name="live_findings")
    op.drop_index("ix_live_findings_session_created", table_name="live_findings")
    op.drop_table("live_findings")
    op.drop_index("ix_live_triggers_session_status", table_name="live_triggers")
    op.drop_table("live_triggers")
    op.drop_table("live_segments")
    op.execute("DROP TRIGGER IF EXISTS trg_live_speakers_updated_at ON live_speakers")
    op.drop_table("live_speakers")
    op.execute("DROP TRIGGER IF EXISTS trg_live_sessions_updated_at ON live_sessions")
    op.drop_index("ix_live_sessions_active_code", table_name="live_sessions")
    op.drop_index("ix_live_sessions_started_by", table_name="live_sessions")
    op.drop_table("live_sessions")
    op.execute("DROP FUNCTION IF EXISTS touch_updated_at()")

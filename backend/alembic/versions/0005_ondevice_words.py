"""Optional on-device words (Copper browser Phonon-2 only) for live sessions.

Revision ID: 0005_ondevice_words
Revises: 0004_speaker_identity
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0005_ondevice_words"
down_revision = "0004_speaker_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "live_ondevice_segments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("live_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("client_seq", sa.Integer(), nullable=False),
        sa.Column("engine", sa.String(32), nullable=False),
        sa.Column("start_seconds", sa.Numeric(10, 3), nullable=False),
        sa.Column("end_seconds", sa.Numeric(10, 3), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column(
            "words", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "session_id", "client_seq", name="uq_live_ondevice_segments_client_seq"
        ),
    )
    op.create_index(
        "ix_live_ondevice_segments_session_start",
        "live_ondevice_segments",
        ["session_id", "start_seconds"],
    )


def downgrade() -> None:
    op.drop_index("ix_live_ondevice_segments_session_start", table_name="live_ondevice_segments")
    op.drop_table("live_ondevice_segments")

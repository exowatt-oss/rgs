"""Opt-in short-retention raw audio for live sessions.

Revision ID: 0006_audio_retention
Revises: 0005_ondevice_words
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0006_audio_retention"
down_revision = "0005_ondevice_words"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "live_sessions",
        sa.Column("audio_retain_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "live_audio_chunks",
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("live_sessions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("seq", sa.Integer(), primary_key=True),
        sa.Column("start_sample", sa.BigInteger(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("pcm", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_live_sessions_audio_retain_until",
        "live_sessions",
        ["audio_retain_until"],
        postgresql_where=sa.text("audio_retain_until IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_live_sessions_audio_retain_until", table_name="live_sessions")
    op.drop_table("live_audio_chunks")
    op.drop_column("live_sessions", "audio_retain_until")

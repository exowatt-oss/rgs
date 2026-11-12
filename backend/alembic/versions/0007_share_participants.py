"""Opt-in recap sharing with meeting participants (+ their read access).

Revision ID: 0007_share_participants
Revises: 0006_audio_retention
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0007_share_participants"
down_revision = "0006_audio_retention"
branch_labels = None
depends_on = None

_INFLIGHT_OLD = "recap_status IN ('pending', 'writing', 'printing', 'delivering')"
_INFLIGHT_NEW = "recap_status IN ('pending', 'writing', 'printing', 'delivering', 'sharing')"


def _inflight_index(where: str) -> None:
    op.drop_index("ix_live_sessions_recap_inflight", table_name="live_sessions")
    op.create_index(
        "ix_live_sessions_recap_inflight",
        "live_sessions",
        ["recap_status"],
        postgresql_where=sa.text(where),
    )


def upgrade() -> None:
    op.add_column(
        "live_sessions",
        sa.Column(
            "share_with_participants", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "live_sessions",
        sa.Column(
            "shared_with",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    # `sharing` (sending to participants) is a recap step a restart must resume.
    _inflight_index(_INFLIGHT_NEW)


def downgrade() -> None:
    op.execute(
        "UPDATE live_sessions SET recap_status = CASE WHEN recap_edition->>'status' = "
        "'published' THEN 'done' ELSE 'failed' END WHERE recap_status = 'sharing'"
    )
    _inflight_index(_INFLIGHT_OLD)
    op.drop_column("live_sessions", "shared_with")
    op.drop_column("live_sessions", "share_with_participants")

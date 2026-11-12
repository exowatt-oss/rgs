"""Recap: ad-hoc session kind, delivery preference, structured notes, external publisher edition.

Revision ID: 0003_recap
Revises: 0002_live
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003_recap"
down_revision = "0002_live"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "live_sessions",
        sa.Column("kind", sa.String(16), nullable=False, server_default="call"),
    )
    op.add_column(
        "live_sessions",
        sa.Column("recap_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "live_sessions",
        sa.Column(
            "deliver_via",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[\"slack\"]'::jsonb"),
        ),
    )
    op.add_column("live_sessions", sa.Column("recap_status", sa.String(16), nullable=True))
    op.add_column("live_sessions", sa.Column("recap_notes", postgresql.JSONB(), nullable=True))
    op.add_column("live_sessions", sa.Column("recap_edition", postgresql.JSONB(), nullable=True))
    op.add_column("live_sessions", sa.Column("recap_delivery", postgresql.JSONB(), nullable=True))
    op.add_column("live_sessions", sa.Column("recap_error", sa.String(), nullable=True))
    op.add_column(
        "live_sessions",
        sa.Column("recap_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Existing sessions predate recaps: nothing to print for them.
    op.execute("UPDATE live_sessions SET kind = 'adhoc' WHERE meet_url IS NULL")
    op.create_index(
        "ix_live_sessions_recap_inflight",
        "live_sessions",
        ["recap_status"],
        postgresql_where=sa.text(
            "recap_status IN ('pending', 'writing', 'printing', 'delivering')"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_live_sessions_recap_inflight", table_name="live_sessions")
    for col in (
        "recap_updated_at",
        "recap_error",
        "recap_delivery",
        "recap_edition",
        "recap_notes",
        "recap_status",
        "deliver_via",
        "recap_enabled",
        "kind",
    ):
        op.drop_column("live_sessions", col)

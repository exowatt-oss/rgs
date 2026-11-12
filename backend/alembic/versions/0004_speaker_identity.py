"""Speaker identity: name confidence/evidence, directory person, suggestions, speaker-count hint.

Revision ID: 0004_speaker_identity
Revises: 0003_recap
"""

import sqlalchemy as sa

from alembic import op

revision = "0004_speaker_identity"
down_revision = "0003_recap"
branch_labels = None
depends_on = None

_SPEAKER_COLUMNS = (
    ("name_confidence", sa.Float()),
    ("name_evidence", sa.String()),
    ("person_email", sa.String(320)),
    ("suggested_name", sa.String()),
    ("suggested_email", sa.String(320)),
)


def upgrade() -> None:
    for name, type_ in _SPEAKER_COLUMNS:
        op.add_column("live_speakers", sa.Column(name, type_, nullable=True))
    op.add_column("live_sessions", sa.Column("expected_speakers", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("live_sessions", "expected_speakers")
    for name, _type in reversed(_SPEAKER_COLUMNS):
        op.drop_column("live_speakers", name)

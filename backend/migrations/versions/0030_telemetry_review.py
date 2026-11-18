"""Persist progress of the targeted historical OCR review.

Revision ID: 0030
Revises: 0029
"""

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("recordings", sa.Column("telemetry_review_state", sa.String(32), nullable=True))
    op.create_index(
        "ix_recordings_telemetry_review_state", "recordings", ["telemetry_review_state"]
    )


def downgrade() -> None:
    op.drop_index("ix_recordings_telemetry_review_state", table_name="recordings")
    op.drop_column("recordings", "telemetry_review_state")

"""Persist field-level telemetry extraction quality.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-11

The old row shape could only say ``has_fix = false``. It could not distinguish the
camera's explicit no-fix marker from OCR failure, parse rejection, or a sample that was
never retained. A JSON provenance column records those independent states and remains
nullable for existing rows until their recording is reprocessed.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("telemetry_points") as batch:
        batch.add_column(sa.Column("quality_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("telemetry_points") as batch:
        batch.drop_column("quality_json")

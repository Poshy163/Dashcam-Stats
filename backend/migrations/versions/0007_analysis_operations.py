"""Analysis revisions, quality rollups, review and operational controls.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("recordings") as batch:
        for name in (
            "metadata_revision",
            "telemetry_revision",
            "detection_revision",
            "plate_revision",
        ):
            batch.add_column(sa.Column(name, sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("gps_gap_count", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("gps_longest_gap_s", sa.Float(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("gps_recovered_count", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("gps_no_fix_count", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("gps_ocr_gap_count", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("gps_rejected_count", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("telemetry_problem_count", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("protected", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("event_type", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("event_notes", sa.Text(), nullable=True))
        batch.create_index("ix_recordings_protected", ["protected"], unique=False)
        batch.create_index("ix_recordings_event_type", ["event_type"], unique=False)

    with op.batch_alter_table("plates") as batch:
        batch.add_column(sa.Column("dismissed", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.create_index("ix_plates_dismissed", ["dismissed"], unique=False)

    with op.batch_alter_table("processing_jobs") as batch:
        batch.add_column(sa.Column("resource_state", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("processing_jobs") as batch:
        batch.drop_column("resource_state")
    with op.batch_alter_table("plates") as batch:
        batch.drop_index("ix_plates_dismissed")
        batch.drop_column("dismissed")
    with op.batch_alter_table("recordings") as batch:
        batch.drop_index("ix_recordings_event_type")
        batch.drop_index("ix_recordings_protected")
        for name in (
            "event_notes",
            "event_type",
            "protected",
            "telemetry_problem_count",
            "gps_recovered_count",
            "gps_rejected_count",
            "gps_ocr_gap_count",
            "gps_no_fix_count",
            "gps_longest_gap_s",
            "gps_gap_count",
            "plate_revision",
            "detection_revision",
            "telemetry_revision",
            "metadata_revision",
        ):
            batch.drop_column(name)

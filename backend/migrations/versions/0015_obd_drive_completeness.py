"""Project OBD lifecycle and tier-aware completeness without mutating source JSON.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-30

The v1 logger could emit ``completion_status=complete`` together with
``clean_end=false``. The immutable manifest remains untouched; these columns hold the
recomputable server projection, per-signal quality analysis, and its processing state.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "obd_drives",
        sa.Column(
            "lifecycle_status", sa.String(length=32), nullable=False, server_default="complete"
        ),
    )
    op.add_column(
        "obd_drives", sa.Column("interruption_reason", sa.String(length=128), nullable=True)
    )
    op.add_column(
        "obd_drives", sa.Column("first_sample_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "obd_drives", sa.Column("last_sample_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "obd_drives",
        sa.Column("last_successful_response_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "obd_drives",
        sa.Column("finalization_observed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "obd_drives",
        sa.Column("connection_loss_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "obd_drives", sa.Column("gap_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("obd_drives", sa.Column("longest_gap_s", sa.Float(), nullable=True))
    op.add_column(
        "obd_drives", sa.Column("data_completeness_percentage", sa.Float(), nullable=True)
    )
    op.add_column("obd_drives", sa.Column("gap_analysis_json", sa.JSON(), nullable=True))
    op.add_column(
        "obd_drives",
        sa.Column(
            "processing_status", sa.String(length=32), nullable=False, server_default="pending"
        ),
    )
    op.add_column("obd_drives", sa.Column("last_processing_error", sa.Text(), nullable=True))
    op.add_column(
        "obd_drives",
        sa.Column(
            "summary_source", sa.String(length=32), nullable=False, server_default="producer"
        ),
    )
    op.add_column(
        "obd_drives", sa.Column("summary_generated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_obd_drives_lifecycle_status", "obd_drives", ["lifecycle_status"])
    op.create_index("ix_obd_drives_processing_status", "obd_drives", ["processing_status"])

    # Make the affected legacy rows truthful immediately. The richer timestamps and
    # quality JSON are filled by the idempotent reconciler at startup or via the API.
    op.execute(
        """
        UPDATE obd_drives
        SET lifecycle_status = CASE
              WHEN clean_end = 1 THEN 'complete'
              WHEN stop_reason = 'device_restart' OR completion_status = 'recovered'
                THEN 'recovered'
              ELSE 'interrupted'
            END,
            interruption_reason = CASE
              WHEN clean_end = 0 THEN COALESCE(stop_reason, 'unclean_end')
              ELSE NULL
            END,
            finalization_observed_at = finished_at,
            processing_status = 'pending'
        """
    )


def downgrade() -> None:
    op.drop_index("ix_obd_drives_processing_status", table_name="obd_drives")
    op.drop_index("ix_obd_drives_lifecycle_status", table_name="obd_drives")
    for column in (
        "summary_generated_at",
        "summary_source",
        "last_processing_error",
        "processing_status",
        "gap_analysis_json",
        "data_completeness_percentage",
        "longest_gap_s",
        "gap_count",
        "connection_loss_count",
        "finalization_observed_at",
        "last_successful_response_at",
        "last_sample_at",
        "first_sample_at",
        "interruption_reason",
        "lifecycle_status",
    ):
        op.drop_column("obd_drives", column)

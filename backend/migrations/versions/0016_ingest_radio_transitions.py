"""Persist ingest radio ownership and crash-recovery state.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-30

Only transition checkpoints live here.  Bulk-transfer progress remains in memory and
normal completed-run history remains in ``ingest_runs``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ingest_radio_transitions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("transition_id", sa.String(length=36), nullable=False, unique=True),
        sa.Column("trigger", sa.String(length=32), nullable=False, server_default="auto"),
        sa.Column("phase", sa.String(length=32), nullable=False, server_default="preparing"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=64), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("device_address", sa.String(length=255), nullable=False),
        sa.Column("device_boot_id", sa.String(length=128), nullable=True),
        sa.Column("transport_host", sa.String(length=255), nullable=False),
        sa.Column("transport_interface", sa.String(length=64), nullable=True),
        sa.Column("capabilities_json", sa.JSON(), nullable=True),
        sa.Column("logger_status_path", sa.String(length=512), nullable=True),
        sa.Column("logger_request_id", sa.String(length=64), nullable=True),
        sa.Column(
            "logger_quiesce_capable", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("logger_quiesce_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("logger_quiesce_acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "logger_resume_attempted", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "logger_resume_verified", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "bluetooth_before", sa.String(length=16), nullable=False, server_default="unknown"
        ),
        sa.Column("hotspot_before", sa.String(length=16), nullable=False, server_default="unknown"),
        sa.Column("hotspot_interface", sa.String(length=64), nullable=True),
        sa.Column("hotspot_restore_ref", sa.String(length=255), nullable=True),
        sa.Column(
            "bluetooth_disable_attempted", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "bluetooth_disable_verified", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "hotspot_disable_attempted", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "hotspot_disable_verified", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "bluetooth_restore_attempted", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "bluetooth_restore_verified", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "hotspot_restore_attempted", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "hotspot_restore_verified", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("obd_transfer_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("recovery_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_error", sa.Text(), nullable=True),
    )
    op.create_index(
        "uq_ingest_radio_transition_active",
        "ingest_radio_transitions",
        ["active"],
        unique=True,
        sqlite_where=sa.text("active = 1"),
    )
    op.create_index("ix_ingest_radio_transitions_phase", "ingest_radio_transitions", ["phase"])
    op.create_index("ix_ingest_radio_transitions_active", "ingest_radio_transitions", ["active"])
    op.create_index(
        "ix_ingest_radio_transitions_created_at", "ingest_radio_transitions", ["created_at"]
    )
    op.create_index(
        "ix_ingest_radio_transitions_lease_expires_at",
        "ingest_radio_transitions",
        ["lease_expires_at"],
    )
    op.create_index(
        "ix_ingest_radio_transitions_recovery_required",
        "ingest_radio_transitions",
        ["recovery_required"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ingest_radio_transitions_recovery_required",
        table_name="ingest_radio_transitions",
    )
    op.drop_index(
        "ix_ingest_radio_transitions_lease_expires_at",
        table_name="ingest_radio_transitions",
    )
    op.drop_index("ix_ingest_radio_transitions_created_at", table_name="ingest_radio_transitions")
    op.drop_index("ix_ingest_radio_transitions_active", table_name="ingest_radio_transitions")
    op.drop_index("ix_ingest_radio_transitions_phase", table_name="ingest_radio_transitions")
    op.drop_index("uq_ingest_radio_transition_active", table_name="ingest_radio_transitions")
    op.drop_table("ingest_radio_transitions")

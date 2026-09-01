"""Persist the Android logger's bounded lifecycle event stream.

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-01

Only canonical codes and finite numeric metrics are stored.  The app's random source and
session UUIDs are hashed before insertion; hardware/network identifiers never enter this
table or its API.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "obd_logger_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id_hash", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_id_hash", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("level", sa.String(length=8), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("drive_id", sa.String(length=64), nullable=True),
        sa.Column("metrics_json", sa.JSON(), nullable=True),
        sa.Column("app_version_name", sa.String(length=64), nullable=False),
        sa.Column("app_version_code", sa.Integer(), nullable=False),
        sa.Column("build_git_sha", sa.String(length=12), nullable=False),
        sa.UniqueConstraint(
            "source_id_hash",
            "sequence",
            name="uq_obd_logger_event_source_sequence",
        ),
    )
    op.create_index("ix_obd_logger_events_occurred_at", "obd_logger_events", ["occurred_at"])
    op.create_index("ix_obd_logger_events_received_at", "obd_logger_events", ["received_at"])
    op.create_index("ix_obd_logger_events_kind", "obd_logger_events", ["kind"])
    op.create_index("ix_obd_logger_events_level", "obd_logger_events", ["level"])
    op.create_index("ix_obd_logger_events_drive_id", "obd_logger_events", ["drive_id"])
    op.create_index(
        "ix_obd_logger_events_kind_time",
        "obd_logger_events",
        ["kind", "occurred_at"],
    )
    op.create_index(
        "ix_obd_logger_events_drive_time",
        "obd_logger_events",
        ["drive_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_obd_logger_events_drive_time", table_name="obd_logger_events")
    op.drop_index("ix_obd_logger_events_kind_time", table_name="obd_logger_events")
    op.drop_index("ix_obd_logger_events_drive_id", table_name="obd_logger_events")
    op.drop_index("ix_obd_logger_events_level", table_name="obd_logger_events")
    op.drop_index("ix_obd_logger_events_kind", table_name="obd_logger_events")
    op.drop_index("ix_obd_logger_events_received_at", table_name="obd_logger_events")
    op.drop_index("ix_obd_logger_events_occurred_at", table_name="obd_logger_events")
    op.drop_table("obd_logger_events")

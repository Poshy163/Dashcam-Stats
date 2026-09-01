"""Persist a filtered slice of the head unit's own system log.

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-01

The vendor firmware ships with logging disabled, so the built-in recorder fails silently.
This table holds the filtered capture described in ``app/ingest/unit_logs.py``: error level
and above, with the measured high-rate noise tags silenced on the unit before a line is
ever written.  ``line_hash`` is unique so the non-consuming refresh can re-read the same
tail without duplicating rows.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "unit_log_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("tid", sa.Integer(), nullable=False),
        sa.Column("level", sa.String(length=1), nullable=False),
        sa.Column("tag", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("line_hash", sa.String(length=64), nullable=False),
        sa.UniqueConstraint("line_hash", name="uq_unit_log_entry_line_hash"),
    )
    op.create_index("ix_unit_log_entries_occurred_at", "unit_log_entries", ["occurred_at"])
    op.create_index("ix_unit_log_entries_received_at", "unit_log_entries", ["received_at"])
    op.create_index("ix_unit_log_entries_level", "unit_log_entries", ["level"])
    op.create_index("ix_unit_log_entries_tag", "unit_log_entries", ["tag"])
    op.create_index("ix_unit_log_entries_tag_time", "unit_log_entries", ["tag", "occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_unit_log_entries_tag_time", table_name="unit_log_entries")
    op.drop_index("ix_unit_log_entries_tag", table_name="unit_log_entries")
    op.drop_index("ix_unit_log_entries_level", table_name="unit_log_entries")
    op.drop_index("ix_unit_log_entries_received_at", table_name="unit_log_entries")
    op.drop_index("ix_unit_log_entries_occurred_at", table_name="unit_log_entries")
    op.drop_table("unit_log_entries")

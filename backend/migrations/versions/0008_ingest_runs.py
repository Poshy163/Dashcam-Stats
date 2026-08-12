"""History for footage pulled off the head unit.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ingest_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trigger", sa.String(length=32), nullable=False, server_default="auto"),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="ok"),
        sa.Column("files_transferred", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bytes_transferred", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("throughput_mbs_avg", sa.Float(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_ingest_runs_started_at", "ingest_runs", ["started_at"], unique=False)
    op.create_index("ix_ingest_runs_state", "ingest_runs", ["state"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_ingest_runs_state", table_name="ingest_runs")
    op.drop_index("ix_ingest_runs_started_at", table_name="ingest_runs")
    op.drop_table("ingest_runs")

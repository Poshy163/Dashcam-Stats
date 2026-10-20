"""Indexes for the queries that grew with the library.

Revision ID: 0025
Revises: 0024

Measured on a 12.7k-recording / 865k-point library: the whole-library map queries filter
``telemetry_points`` on ``has_fix`` and ``captured_at`` with nothing else to narrow by, the
dashboard asks for recordings processed in the last hour, the queue statistics walk the
newest completed jobs, and the map cache fingerprints the library by its newest journey
change. None of those columns had an index, so each was a scan of its whole table.
"""

from __future__ import annotations

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


INDEXES: tuple[tuple[str, str, list[str]], ...] = (
    ("ix_telemetry_fix_captured", "telemetry_points", ["has_fix", "captured_at"]),
    ("ix_recordings_processed_at", "recordings", ["processed_at"]),
    ("ix_jobs_state_finished", "processing_jobs", ["state", "finished_at"]),
    ("ix_journeys_updated_at", "journeys", ["updated_at"]),
)


def upgrade() -> None:
    for name, table, columns in INDEXES:
        op.create_index(name, table, columns, if_not_exists=True)


def downgrade() -> None:
    for name, table, _columns in reversed(INDEXES):
        op.drop_index(name, table_name=table, if_exists=True)

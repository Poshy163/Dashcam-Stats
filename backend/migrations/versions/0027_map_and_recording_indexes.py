"""Cover the map reads and the visible recording list's time order.

Revision ID: 0027
Revises: 0026
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_telemetry_map",
        "telemetry_points",
        [
            "has_fix",
            "recording_id",
            "journey_id",
            "captured_at",
            "t_offset_s",
            "lat",
            "lon",
            "gps_quality",
            "speed_kmh",
            "breaks_segment",
        ],
        sqlite_where=sa.text("has_fix IS 1 AND lat IS NOT NULL AND lon IS NOT NULL"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_recordings_ignored_started", "recordings", ["ignored", "started_at"], if_not_exists=True
    )
    op.create_index(
        "ix_recordings_journey_ignored", "recordings", ["journey_id", "ignored"], if_not_exists=True
    )
    op.drop_index("ix_recordings_ignored", table_name="recordings", if_exists=True)
    op.drop_index("ix_recordings_journey_id", table_name="recordings", if_exists=True)
    # The UNIQUE constraint already maintains the same two-column B-tree for clip seeks.
    op.drop_index("ix_telemetry_recording_offset", table_name="telemetry_points", if_exists=True)


def downgrade() -> None:
    op.create_index(
        "ix_telemetry_recording_offset",
        "telemetry_points",
        ["recording_id", "t_offset_s"],
        if_not_exists=True,
    )
    op.create_index("ix_recordings_ignored", "recordings", ["ignored"], if_not_exists=True)
    op.create_index("ix_recordings_journey_id", "recordings", ["journey_id"], if_not_exists=True)
    op.drop_index("ix_recordings_journey_ignored", table_name="recordings", if_exists=True)
    op.drop_index("ix_recordings_ignored_started", table_name="recordings", if_exists=True)
    op.drop_index("ix_telemetry_map", table_name="telemetry_points", if_exists=True)

"""Keep the default moving heatmap's fixes in grid-cell and observation-second order.

Revision ID: 0028
Revises: 0027
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.heat_index import SQLITE_VERSION_NUMBER

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_telemetry_heat_cells",
        "telemetry_points",
        [
            "has_fix",
            sa.text("round(lat, 3)"),
            sa.text("round(lon, 3)"),
            sa.text("coalesce(strftime('%s', captured_at), id)"),
            "recording_id",
            "journey_id",
            "speed_kmh",
            "gps_quality",
        ],
        sqlite_where=sa.text(
            "has_fix IS 1 AND lat IS NOT NULL AND lon IS NOT NULL "
            "AND (speed_kmh IS NULL OR speed_kmh >= 5)"
        ),
        if_not_exists=True,
    )
    op.execute(f"PRAGMA user_version = {SQLITE_VERSION_NUMBER}")


def downgrade() -> None:
    op.drop_index("ix_telemetry_heat_cells", table_name="telemetry_points", if_exists=True)
    op.execute("PRAGMA user_version = 0")

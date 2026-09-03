"""Let the head unit itself close a radio transition before it sleeps.

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-03

The vendor sleep countdown is not ours to pause, so the ordinary end of a backup window is
the unit going quiet mid-run. Everything after that point is unreachable: the server's
restore is *attempted*, never *verified*, and the row sits in ``recovery_required`` --
blocking the next backup -- until the car comes back.

These columns move the last word onto the unit. The detached watchdog is handed a
single-use ``unit_report_token`` when it is armed; seconds before sleep it restores both
radios, reads their real state back on the device, and posts that evidence in. The
readback is the same evidence class the server would have gathered over ADB, so
``restore_evidence_source`` records which side actually answered rather than pretending
they are the same thing.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ingest_radio_transitions") as batch:
        batch.add_column(sa.Column("unit_report_token", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("unit_reported_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("unit_sleep_reported_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column("restore_evidence_source", sa.String(length=16), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("ingest_radio_transitions") as batch:
        batch.drop_column("restore_evidence_source")
        batch.drop_column("unit_sleep_reported_at")
        batch.drop_column("unit_reported_at")
        batch.drop_column("unit_report_token")
